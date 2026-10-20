"""
ui/server.py — FastAPI application for Friday Budgeting Pro.

Implements all UI routes defined in issue #14.

Auth is handled by ui.auth (PBKDF2 placeholder — argon2id lands in #37).
Templates live in ui/templates/.  Static files in ui/static/.

Design Constraint #6: this module is transport-agnostic (127.0.0.1 binding
is configured in server.daemon via uvicorn.Config, not here).

Route overview
──────────────
  GET  /              redirect hub based on setup/auth state
  GET  /healthz       liveness probe (from #52 — preserved)
  GET  /setup         first-run wizard
  POST /setup/<step>  advance wizard step (final step: complete setup)
  GET  /login         password login form
  POST /login         verify password, create session
  POST /logout        delete session, clear cookie
  GET  /forgot        password recovery placeholder (#60)
  POST /forgot        write recovery token file placeholder (#60)
  GET  /reset         password reset form placeholder (#60)
  POST /reset         password reset action placeholder (#60)
  GET  /profile       settings + linked-accounts placeholder (#47)
  POST /profile       save settings to app_config
  GET  /ledgers       read-only ledger tree
  GET  /link          Plaid Link JS embed
  GET  /static/<path> static file serving
  POST /api/classify        trigger LLM classification of pending transactions
  GET  /api/classify/status poll classification job state
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Load .env before any server imports so Plaid credentials are available
try:
    import pathlib as _pathlib

    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(_pathlib.Path(__file__).parent.parent / ".env")
except ImportError:
    pass

import server.paths as _paths
from server.db import get_db, init_db
from server.main import list_rules as _list_rules

# ── Recovery token store (in-memory, 10-min TTL) ──────────────────────────
# Shared with the MCP tools (reset_ui_password) via ui.auth so both operate
# on the same in-memory map.  Tokens do not survive daemon restarts.
from ui.auth import (
    SESSION_COOKIE,
    _recovery_tokens,
    add_recovery_token,
    check_session,
    create_session,
    create_user,
    delete_session,
    get_password_hash,
    get_session_user_id,
    get_user_by_id,
    get_user_by_username,
    has_any_user,
    hash_password,
    list_users,
    set_password_hash,
    update_user_password,
    verify_password,
)

# ── App setup ───────────────────────────────────────────────────────────────

# Run DB migrations on startup so schema is always up to date
# (safe to call repeatedly — all operations are idempotent)
init_db(_paths.DB_PATH)

# ── Background classification state ─────────────────────────────────────────
# Tracks whether an async LLM classification job is running and its last result.
import threading as _threading

_classify_lock = _threading.Lock()
_classify_state: dict = {
    "running": False,
    "result": None,  # dict with classified/skipped/uncertain or error
}

# ── Background sync progress state ───────────────────────────────────────────
# Tracks real-time progress for the sync + classify pipeline so the dashboard
# UI can poll /api/sync/progress and render a live step log.
_sync_progress_lock = _threading.Lock()
_sync_progress: dict = {"steps": [], "running": False, "phase": "idle"}


def _sync_emit(step: dict) -> None:
    """Append *step* to the shared sync-progress log (thread-safe)."""
    with _sync_progress_lock:
        _sync_progress["steps"].append(step)
        _sync_progress["phase"] = step.get("phase", "running")


app = FastAPI(title="Friday Budgeting Pro UI", version="0.1.0")

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


_SETUP_COMPLETE_COOKIE = "friday_bp_setup"

# ── Helpers ─────────────────────────────────────────────────────────────────


def _db_path() -> Path:
    """Return the active DB path (test-overridable via server.paths.DB_PATH)."""
    return _paths.DB_PATH


def _password_is_set() -> bool:
    """Legacy check: True if any user exists (or app_config has a password hash)."""
    return has_any_user(_db_path()) or bool(get_password_hash(_db_path()))


def _is_authenticated(request: Request) -> bool:
    return check_session(request, _db_path())


def _current_user_id(request: Request) -> Optional[str]:
    """Return the user_id from the current session, or None."""
    return get_session_user_id(request, _db_path())


def _check_raw_session(db_path, token: str) -> bool:
    """Validate a raw session token without a Request object."""
    from ui.auth import _SESSION_TTL, _now

    conn = get_db(db_path)
    try:
        row = conn.execute("SELECT expires_at FROM sessions WHERE id = ?", (token,)).fetchone()
        if row is None:
            return False
        now = _now()
        if now > row["expires_at"]:
            conn.execute("DELETE FROM sessions WHERE id = ?", (token,))
            conn.commit()
            return False
        conn.execute(
            "UPDATE sessions SET last_seen_at=?, expires_at=? WHERE id=?",
            (now, now + _SESSION_TTL, token),
        )
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=302)


_CHANNEL_TO_PREF = {"openclaw_chat": "openclaw", "in_ui": "ui", "macos": "macos"}
_PREF_TO_CHANNEL = {
    "openclaw": "openclaw_chat",
    "ui": "in_ui",
    "macos": "macos",
    "openclaw_chat": "openclaw_chat",
    "in_ui": "in_ui",
}


def _get_username(user_id: Optional[str] = None) -> str:
    """Return the username for *user_id* (or first user if not specified)."""
    conn = get_db(_db_path())
    try:
        if user_id:
            row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
            if row and row["username"]:
                return row["username"]
        # Fallback: first user or app_config
        row2 = conn.execute("SELECT username FROM users ORDER BY created_at LIMIT 1").fetchone()
        if row2 and row2["username"]:
            return row2["username"]
        try:
            row3 = conn.execute("SELECT username FROM app_config WHERE id=1").fetchone()
            if row3 and row3["username"]:
                return row3["username"]
        except Exception:
            pass
        return "User"
    except Exception:
        return "User"
    finally:
        conn.close()


def _get_notification_channel() -> str:
    conn = get_db(_db_path())
    try:
        try:
            row = conn.execute("SELECT notification_channel FROM app_config WHERE id=1").fetchone()
            if row and row["notification_channel"]:
                return row["notification_channel"]
        except Exception:
            pass
        try:
            row = conn.execute("SELECT notification_pref FROM app_config WHERE id=1").fetchone()
            if row and row["notification_pref"]:
                return _PREF_TO_CHANNEL.get(row["notification_pref"], row["notification_pref"])
        except Exception:
            pass
        return "openclaw_chat"
    finally:
        conn.close()


def _get_notification_pref() -> str:
    return _CHANNEL_TO_PREF.get(_get_notification_channel(), "openclaw")


def _set_notification_channel(channel: str) -> None:
    conn = get_db(_db_path())
    try:
        try:
            conn.execute("ALTER TABLE app_config ADD COLUMN notification_channel TEXT")
            conn.commit()
        except Exception:
            pass
        conn.execute(
            "INSERT INTO app_config (id, notification_channel) VALUES (1,?) "
            "ON CONFLICT(id) DO UPDATE SET notification_channel=excluded.notification_channel",
            (channel,),
        )
        conn.commit()
    finally:
        conn.close()


def _set_notification_pref(pref: str) -> None:
    _set_notification_channel(_PREF_TO_CHANNEL.get(pref, pref))


def _get_ledgers(
    user_id: Optional[str] = None,
    with_drilldown: bool = False,
    period: Optional[str] = "this_month",
) -> list[dict]:
    """Query ledgers + line_items from the DB and return a list of dicts.

    Parameters
    ----------
    user_id :
        When supplied only ledgers owned by this user (plus legacy NULL-user
        rows) are returned.
    with_drilldown :
        When ``True`` each line-item dict includes ``total`` and
        ``transactions`` keys (used by the /ledgers page drilldown UI).
    period :
        Date filter applied to transactions: ``"this_month"``, ``"last_month"``,
        ``"last_3_months"``, ``"this_year"``, ``"all"``, or ``None`` (all time).
    """
    from server.main import _build_ledger_drilldown

    # Normalise: "all" sentinel → None so drilldown skips date filter
    drilldown_period: Optional[str] = None if period == "all" else period

    conn = get_db(_db_path())
    try:
        if user_id:
            # Include rows owned by user_id AND unowned rows (NULL user_id = legacy/migration)
            ledger_rows = conn.execute(
                "SELECT id, name, type, description FROM ledgers "
                "WHERE user_id = ? OR user_id IS NULL ORDER BY name",
                (user_id,),
            ).fetchall()
        else:
            ledger_rows = conn.execute(
                "SELECT id, name, type, description FROM ledgers ORDER BY name"
            ).fetchall()
        ledgers = []
        for lr in ledger_rows:
            if with_drilldown:
                drilldown = _build_ledger_drilldown(conn, lr, period=drilldown_period)
                ledgers.append(
                    {
                        "id": lr["id"],
                        "name": lr["name"],
                        "line_items": drilldown["line_items"],
                        "totals": drilldown["totals"],
                    }
                )
            else:
                items = conn.execute(
                    "SELECT id, name, item_type FROM line_items WHERE ledger_id = ? ORDER BY name",
                    (lr["id"],),
                ).fetchall()
                ledgers.append(
                    {
                        "id": lr["id"],
                        "name": lr["name"],
                        "line_items": [
                            {"id": i["id"], "name": i["name"], "item_type": i["item_type"]}
                            for i in items
                        ],
                    }
                )
        return ledgers
    finally:
        conn.close()


# ── Wizard state helpers ─────────────────────────────────────────────────────
# Setup wizard tracks progress in app_config.setup_step (0 = not started,
# 1–3 = in-progress, 4 = waiting on bank link, 5 = complete).
# We store it inline with the password write so no extra column is needed for
# steps 1–3; we just track completion by whether the password hash is set.

# Wizard session data is stored in a small in-process dict keyed by a
# short-lived wizard token cookie.  Alternatively, steps could round-trip
# form data; here we use a plain dict for simplicity.  This is reset on
# daemon restart, which is fine for a one-time wizard.
_wizard_state: dict[str, dict] = {}


def _get_wizard_token(request: Request) -> Optional[str]:
    return request.cookies.get("friday_bp_wizard")


def _wizard_data(request: Request) -> dict:
    token = _get_wizard_token(request)
    if token and token in _wizard_state:
        return _wizard_state[token]
    return {}


def _update_wizard(response: Response, token: str, data: dict) -> None:
    _wizard_state[token] = data
    response.set_cookie(
        "friday_bp_wizard",
        token,
        httponly=True,
        samesite="lax",
        max_age=3600,
    )


def _clear_wizard(response: Response, token: Optional[str]) -> None:
    if token and token in _wizard_state:
        del _wizard_state[token]
    response.delete_cookie("friday_bp_wizard")


# ── Routes ────────────────────────────────────────────────────────────────────

# ── /healthz ─────────────────────────────────────────────────────────────────


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness probe — returns 200 OK when the daemon is running."""
    return {"status": "ok"}


# ── / ────────────────────────────────────────────────────────────────────────


@app.get("/")
def index(request: Request):
    """Redirect hub.

    - No password set  → /setup
    - Not authenticated → /login
    - Authenticated    → /profile
    """
    if not _password_is_set():
        return _redirect("/setup")
    if not _is_authenticated(request):
        return _redirect("/login")
    return _redirect("/dashboard")


# ── /setup ───────────────────────────────────────────────────────────────────


def _openclaw_home_exists() -> bool:
    return Path(os.path.expanduser("~/.openclaw")).is_dir()


@app.get("/setup", response_class=HTMLResponse)
def setup_get(request: Request):
    if _password_is_set():
        return HTMLResponse(status_code=404, content="Setup already complete.")
    tok = _get_wizard_token(request) or secrets.token_hex(16)
    dch = "openclaw_chat" if _openclaw_home_exists() else "macos"
    resp = templates.TemplateResponse(
        request, "setup.html", {"step": 1, "error": None, "default_channel": dch}
    )
    _update_wizard(resp, tok, {"step": 1, "wizard_active": False})
    return resp


@app.post("/setup/{step}", response_class=HTMLResponse)
async def setup_post(request: Request, step: int):
    tok = _get_wizard_token(request) or secrets.token_hex(16)
    state = _wizard_state.get(tok, {})
    wip = bool(state) and state.get("wizard_active", False)
    if _password_is_set() and not wip:
        return HTMLResponse(status_code=404, content="Setup already complete.")
    form = await request.form()
    if step == 1:
        username = (form.get("username") or "").strip() or "default"
        pw = (form.get("password") or "").strip()
        cf = (form.get("password_confirm") or "").strip()
        dch = "openclaw_chat" if _openclaw_home_exists() else "macos"
        if len(pw) < 8:
            err = "Password must be at least 8 characters."
            resp = templates.TemplateResponse(
                request, "setup.html", {"step": 1, "error": err, "default_channel": dch}
            )
            _update_wizard(resp, tok, {"step": 1, "wizard_active": False, "error": err})
            return resp
        if pw != cf:
            err = "Passwords do not match."
            resp = templates.TemplateResponse(
                request, "setup.html", {"step": 1, "error": err, "default_channel": dch}
            )
            _update_wizard(resp, tok, {"step": 1, "wizard_active": False, "error": err})
            return resp

        # Create user in users table (or update existing default user's password).
        password_hash = hash_password(pw)
        existing_user = get_user_by_username(_db_path(), username)
        if existing_user:
            # Update existing user's password
            from ui.auth import update_user_password

            update_user_password(_db_path(), existing_user["id"], pw)
            new_user_id = existing_user["id"]
        else:
            # Check if there's a default user to update (migration scenario)
            _conn = get_db(_db_path())
            try:
                default_row = _conn.execute(
                    "SELECT id FROM users WHERE id = 'default-user-id'"
                ).fetchone()
            finally:
                _conn.close()
            if default_row:
                # Update the migrated default user
                _conn2 = get_db(_db_path())
                try:
                    _conn2.execute(
                        "UPDATE users SET username = ?, password_hash = ? WHERE id = 'default-user-id'",
                        (username, password_hash),
                    )
                    _conn2.commit()
                finally:
                    _conn2.close()
                new_user_id = "default-user-id"
            else:
                new_user_id = create_user(_db_path(), username, pw)

        # Also save to app_config for legacy compat.
        set_password_hash(_db_path(), password_hash)
        _conn3 = get_db(_db_path())
        try:
            try:
                _conn3.execute("ALTER TABLE app_config ADD COLUMN username TEXT")
            except:
                pass
            _conn3.execute(
                "INSERT INTO app_config (id, username) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET username=excluded.username",
                (username,),
            )
            _conn3.commit()
        finally:
            _conn3.close()

        ua = request.headers.get("user-agent")
        stoken = create_session(_db_path(), user_agent=ua, user_id=new_user_id)
        ns = {
            "step": 2,
            "wizard_active": True,
            "session_token": stoken,
            "user_id": new_user_id,
            "error": None,
        }
        resp = templates.TemplateResponse(
            request, "setup.html", {"step": 2, "error": None, "default_channel": dch}
        )
        _update_wizard(resp, tok, ns)
        # Set the real session cookie at step 1 so the user is logged in for the
        # rest of the wizard and lands authenticated on /profile at the end.
        # (Matches the spec in tests/test_setup_wizard.py which asserts
        # friday_bp_session is set after POST /setup/1.)
        resp.set_cookie(SESSION_COOKIE, stoken, httponly=True, samesite="lax", secure=True)
        return resp
    elif step == 2:
        raw = form.get("notification_channel") or form.get("notification_pref") or "openclaw_chat"
        ch = _PREF_TO_CHANNEL.get(raw, raw)
        _set_notification_channel(ch)
        # Try to generate a Plaid link token for step 3.
        link_token = None
        try:
            import server.main as _sm_link

            result = _sm_link.start_link()
            url = result.get("url", "")
            if "token=" in url:
                link_token = url.split("token=", 1)[1]
        except Exception:
            pass
        ns = {**state, "step": 3, "notification_channel": ch, "error": None}
        resp = templates.TemplateResponse(
            request, "setup.html", {"step": 3, "error": None, "link_token": link_token}
        )
        _update_wizard(resp, tok, ns)
        return resp
    elif step == 3:
        # Final wizard step — bank linked (via Plaid) or skipped.
        # Either way: run apply_initial_setup and go to the dashboard.
        action = (form.get("action") or "").strip()
        if action == "bank_linked":
            public_token = (form.get("public_token") or "").strip()
            if public_token:
                try:
                    import server.main as _sm_cl

                    _sm_cl.complete_link(public_token=public_token)
                except Exception:
                    pass
                # Trigger an initial sync to populate bank_accounts immediately.
                try:
                    import server.main as _sm_sync_setup

                    _sm_sync_setup.sync()
                except Exception:
                    pass
        import server.main as _sm

        _sm.apply_initial_setup(
            banks_to_link=[],
            extra_ledgers=[],
            hints=[],
            rental_properties=[],
            investment_account_ids=[],
        )
        redir = _redirect("/dashboard")
        st = state.get("session_token")
        if st:
            redir.set_cookie(_SETUP_COMPLETE_COOKIE, st, httponly=True, samesite="lax", secure=True, max_age=300)
        _clear_wizard(redir, tok)
        return redir
    return HTMLResponse(status_code=404, content="Unknown step.")


def _ensure_ledger(name: str, user_id: Optional[str] = None) -> None:
    """Create a ledger row if one with this name doesn't already exist."""
    import uuid as _uuid

    conn = get_db(_db_path())
    try:
        existing = conn.execute("SELECT id FROM ledgers WHERE name = ?", (name,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO ledgers (id, name, user_id) VALUES (?, ?, ?)",
                (str(_uuid.uuid4()), name, user_id),
            )
            conn.commit()
    finally:
        conn.close()


# ── /login ───────────────────────────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    """Render login form.  If no users exist, redirect to /setup."""
    if not _password_is_set():
        return _redirect("/setup")
    profiles = list_users(_db_path())
    prefill_username = request.query_params.get("username", "")
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None, "profiles": profiles, "prefill_username": prefill_username},
    )


@app.post("/login")
async def login_post(request: Request):
    """Verify username+password; on success create session and redirect to /profile.

    Username is optional when there is only one user (backward compat).
    On failure: re-render login.html with an error.
    No rate limiting — single-user local app (per d4403c0).
    """
    if not _password_is_set():
        return _redirect("/setup")

    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""

    profiles = list_users(_db_path())

    # If username not supplied, try to resolve it from the single-user case.
    user = None
    if username:
        user = get_user_by_username(_db_path(), username)
    elif len(profiles) == 1:
        # Backward compat: single user, no username required
        user = get_user_by_id(_db_path(), profiles[0]["id"])
    elif len(profiles) == 0:
        # Should never happen (guarded above), but be safe
        return _redirect("/setup")
    # else: multiple users, username required — user stays None → error below

    success = user is not None and verify_password(password, user["password_hash"])

    if not success:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Incorrect username or password.",
                "profiles": profiles,
                "prefill_username": username,
            },
            status_code=200,
        )
    # Clear any pending setup session.
    st = request.cookies.get(_SETUP_COMPLETE_COOKIE)
    if st:
        delete_session(_db_path(), st)

    # Create session with user_id.
    ua = request.headers.get("user-agent")
    token = create_session(_db_path(), user_agent=ua, user_id=user["id"])

    response = _redirect("/dashboard")
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=True)
    response.delete_cookie(_SETUP_COMPLETE_COOKIE)
    return response


# ── /logout ──────────────────────────────────────────────────────────────────


@app.get("/logout")
@app.post("/logout")
def logout(request: Request):
    """Delete session row(s) and clear cookies."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        delete_session(_db_path(), token)
    st = request.cookies.get(_SETUP_COMPLETE_COOKIE)
    if st:
        delete_session(_db_path(), st)
    response = _redirect("/login")
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(_SETUP_COMPLETE_COOKIE)
    return response


# ── /forgot ──────────────────────────────────────────────────────────────────


@app.get("/forgot", response_class=HTMLResponse)
def forgot_get(request: Request):
    """Render the forgot-password form (username input)."""
    return templates.TemplateResponse(
        request,
        "forgot.html",
        {"sent": False, "error": None},
    )


@app.post("/forgot", response_class=HTMLResponse)
async def forgot_post(request: Request):
    """Generate a recovery token and write it to ~/.friday-bp/recovery.txt.

    Accepts: username (form field)
    Behaviour:
      - Looks up user by username in the users table
      - Generates a 32-byte random hex token with 10-minute TTL
      - Stores token in the in-memory _recovery_tokens dict
      - Writes token to ~/.friday-bp/recovery.txt with mode 0600
      - Returns the same success page whether or not the username exists
        (prevents username enumeration)
    """
    form = await request.form()
    username = (form.get("username") or "").strip()

    user = get_user_by_username(_db_path(), username) if username else None

    if user:
        token = secrets.token_hex(32)
        add_recovery_token(token, user["id"])

        recovery_path = _paths.APP_DIR / "recovery.txt"
        try:
            _paths.APP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Write with O_CREAT|O_WRONLY|O_TRUNC so mode is applied atomically
            fd = os.open(
                str(recovery_path),
                os.O_CREAT | os.O_WRONLY | os.O_TRUNC,
                0o600,
            )
            try:
                os.write(fd, token.encode())
            finally:
                os.close(fd)
            # Ensure mode is correct even if the file already existed
            os.chmod(recovery_path, 0o600)
        except Exception:
            pass

    # Always return success page to avoid leaking which usernames exist.
    return templates.TemplateResponse(
        request,
        "forgot.html",
        {"sent": True, "error": None},
    )


# ── /reset ───────────────────────────────────────────────────────────────────


@app.get("/reset", response_class=HTMLResponse)
def reset_get(request: Request):
    """Render the password-reset form (token + new password)."""
    return templates.TemplateResponse(
        request,
        "reset.html",
        {"error": None, "success": False},
    )


@app.post("/reset", response_class=HTMLResponse)
async def reset_post(request: Request):
    """Validate recovery token and update the user's password.

    Accepts: token, new_password (form fields)
    On success:
      - Hashes new_password with argon2id via update_user_password
      - Deletes recovery.txt
      - Invalidates all sessions for that user (forces re-login)
      - Redirects to /login?reset=1
    On failure:
      - Re-renders reset.html with an error message
    """
    form = await request.form()
    token = (form.get("token") or "").strip()
    new_password = form.get("new_password") or ""

    if len(new_password) < 8:
        return templates.TemplateResponse(
            request,
            "reset.html",
            {"error": "Password must be at least 8 characters.", "success": False},
        )

    entry = _recovery_tokens.get(token)
    if entry is None:
        return templates.TemplateResponse(
            request,
            "reset.html",
            {"error": "Invalid or expired recovery token.", "success": False},
        )

    user_id, expiry = entry
    if time.time() > expiry:
        # Clean up the expired token
        _recovery_tokens.pop(token, None)
        return templates.TemplateResponse(
            request,
            "reset.html",
            {"error": "Recovery token has expired. Please request a new one.", "success": False},
        )

    # Update the password
    update_user_password(_db_path(), user_id, new_password)

    # Delete recovery.txt
    recovery_path = _paths.APP_DIR / "recovery.txt"
    try:
        recovery_path.unlink(missing_ok=True)
    except Exception:
        pass

    # Invalidate all sessions for this user (force re-login after reset)
    conn_db = get_db(_db_path())
    try:
        conn_db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn_db.commit()
    finally:
        conn_db.close()

    # Remove token from in-memory store
    _recovery_tokens.pop(token, None)

    # Redirect to login with a success flash
    return _redirect("/login?reset=1")


# ── /dashboard ──────────────────────────────────────────────────────────────


def _get_last_synced_at() -> Optional[str]:
    """Return MAX(last_synced_at) from sync_cursors as a human-readable string,
    or None if the table is empty or the DB has no sync history."""
    import datetime

    conn = get_db(_db_path())
    try:
        row = conn.execute("SELECT MAX(last_synced_at) FROM sync_cursors").fetchone()
        ts = row[0] if row else None
    except Exception:
        ts = None
    finally:
        conn.close()
    if not ts:
        return None
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _run_classification_background(user_id: str) -> None:
    """Classify all pending transactions for *user_id* in a background thread.

    Updates ``_classify_state`` so ``/api/classify/status`` can report progress.
    Also emits progress steps to ``_sync_progress`` so the dashboard live log
    reflects manual classification as well as the post-sync classify phase.
    Safe to call when a classification job is already running — the second call
    will block on ``_classify_lock`` until the first finishes, then see there
    are no remaining unclassified transactions (idempotent).
    """
    import logging as _logging

    import server.main as _sm

    with _classify_lock:
        _classify_state["running"] = True
        _classify_state["result"] = None
        try:
            _sync_emit({"phase": "classifying", "msg": "Classifying transactions\u2026"})
            result = _sm.classify_pending_transactions(user_id)
            _classify_state["result"] = {"status": "ok", **result}
            n = result.get("classified", 0)
            _sync_emit(
                {
                    "phase": "done",
                    "msg": f"Classification done \u2014 {n} transaction{'s' if n != 1 else ''} auto-classified",
                }
            )
        except Exception as exc:
            _logging.getLogger(__name__).error("Background classification failed: %s", exc)
            _classify_state["result"] = {"status": "error", "error": str(exc)}
            _sync_emit({"phase": "error", "msg": f"Classification error: {exc}"})
        finally:
            _classify_state["running"] = False


@app.post("/api/sync")
def api_sync(request: Request):
    """AJAX sync — returns JSON immediately; sync runs in a background thread.

    After the sync completes, LLM classification is automatically run inline
    within the same background thread so the live progress log reflects both
    phases.  Sync runs with ``classify=False`` so classification does not run
    twice.  The background thread is daemonised so it survives page navigation.
    """
    if not _is_authenticated(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    from server.sync_lock import acquire_sync_lock

    # Check if already running without blocking
    lock = acquire_sync_lock(timeout=0)
    if lock is None:
        return JSONResponse({"status": "already_running"})
    lock.close()  # Release — sync() will re-acquire internally

    def _run():
        global _sync_progress
        with _sync_progress_lock:
            _sync_progress["steps"] = []
            _sync_progress["running"] = True
            _sync_progress["phase"] = "starting"
        try:
            import server.main as _sm
            from ui.auth import get_active_user_id

            # Sync without inline classification; use _sync_emit for live progress.
            sync_result = _sm.sync(classify=False, progress_callback=_sync_emit)
            total_added = sync_result.get("added", 0) if isinstance(sync_result, dict) else 0

            # Run classification inline in this thread so progress log covers
            # both phases and running stays True until both are done.
            uid = get_active_user_id(_paths.DB_PATH)
            # Big-pull warning surfaced from the sync summary (#270).
            if isinstance(sync_result, dict) and sync_result.get("big_pull"):
                _sync_emit(
                    {
                        "phase": "warning",
                        "msg": sync_result.get("warning")
                        or f"Large pull: {total_added} new transactions.",
                    }
                )
            if uid and total_added > 0:
                _sync_emit({"phase": "classifying", "msg": "Classifying transactions\u2026"})
                with _classify_lock:
                    _classify_state["running"] = True
                    _classify_state["result"] = None
                try:
                    # Classify in batches until nothing is pending or a safety
                    # ceiling of passes is hit (#270).  Each pass is bounded by
                    # the per-user classify_batch_size, so we never fire an
                    # unbounded number of serial LLM calls in one go, and the
                    # live progress log updates between batches.
                    total_classified = 0
                    classify_result = {
                        "classified": 0,
                        "skipped": 0,
                        "uncertain": 0,
                        "pending_remaining": 0,
                    }
                    _MAX_PASSES = 40  # hard safety ceiling
                    for _pass in range(_MAX_PASSES):
                        classify_result = _sm.classify_pending_transactions(uid)
                        batch_n = classify_result.get("classified", 0)
                        total_classified += batch_n
                        remaining = classify_result.get("pending_remaining", 0)
                        if batch_n or remaining:
                            _sync_emit(
                                {
                                    "phase": "classifying",
                                    "msg": f"Classified {total_classified} so far… {remaining} remaining",
                                }
                            )
                        # Stop when nothing left, or a pass made no progress
                        # (avoids an infinite loop on persistently-uncertain rows).
                        if remaining <= 0 or batch_n == 0:
                            break
                    classify_result = {**classify_result, "classified": total_classified}
                    _classify_state["result"] = {"status": "ok", **classify_result}
                    n = total_classified
                    remaining = classify_result.get("pending_remaining", 0)
                    done_msg = f"Done! {n} transaction{'s' if n != 1 else ''} classified"
                    if remaining > 0:
                        done_msg += f" — {remaining} still pending (click Sync again to continue)"
                    _sync_emit(
                        {
                            "phase": "done",
                            "msg": done_msg,
                            "added": total_added,
                            "classified": n,
                            "pending_remaining": remaining,
                        }
                    )
                except Exception as exc:
                    import logging as _cl_logging

                    _cl_logging.getLogger(__name__).error(
                        "Classification in sync thread failed: %s", exc
                    )
                    _classify_state["result"] = {"status": "error", "error": str(exc)}
                    _sync_emit({"phase": "error", "msg": f"Classification error: {exc}"})
                finally:
                    with _classify_lock:
                        _classify_state["running"] = False
            elif uid:
                _sync_emit(
                    {
                        "phase": "done",
                        "msg": "Done! No new transactions to classify.",
                        "added": 0,
                        "classified": 0,
                    }
                )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).error("api_sync background: %s", exc)
            _sync_emit({"phase": "error", "msg": f"Sync error: {exc}"})
        finally:
            with _sync_progress_lock:
                _sync_progress["running"] = False
                # Persist the final steps list to DB so a page refresh can restore it.
                _steps_snapshot = list(_sync_progress["steps"])
            import json as _json_lib

            try:
                _log_conn = get_db(_db_path())
                try:
                    _log_conn.execute(
                        "INSERT OR REPLACE INTO sync_log (id, steps_json, finished_at) VALUES (1, ?, ?)",
                        (_json_lib.dumps(_steps_snapshot), int(time.time())),
                    )
                    _log_conn.commit()
                finally:
                    _log_conn.close()
            except Exception as _log_exc:
                import logging as _ll

                _ll.getLogger(__name__).warning("Failed to persist sync_log: %s", _log_exc)

    _threading.Thread(target=_run, daemon=True).start()
    return JSONResponse(
        {
            "status": "ok",
            "added": 0,
            "connections_synced": 0,
            "message": "Sync started in background",
        }
    )


@app.get("/api/sync/progress")
def api_sync_progress(request: Request):
    """Return the current sync progress log for the live dashboard step feed."""
    if not _is_authenticated(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    with _sync_progress_lock:
        data = dict(_sync_progress)
    # If no in-memory state and not running, try loading from DB as fallback.
    if not data["running"] and not data["steps"]:
        try:
            import json as _json_lib

            _log_conn = get_db(_db_path())
            try:
                row = _log_conn.execute("SELECT steps_json FROM sync_log WHERE id = 1").fetchone()
                if row and row["steps_json"]:
                    data["steps"] = _json_lib.loads(row["steps_json"])
                    data["restored"] = True
            finally:
                _log_conn.close()
        except Exception:
            pass
    return JSONResponse(data)


@app.post("/api/classify")
def api_classify(request: Request):
    """Manually trigger LLM classification of all pending transactions."""
    if not _is_authenticated(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    from ui.auth import get_active_user_id

    with _classify_lock:
        if _classify_state["running"]:
            return JSONResponse({"status": "already_running"})

    uid = get_active_user_id(_paths.DB_PATH)
    if not uid:
        return JSONResponse({"error": "No active user"}, status_code=400)

    _threading.Thread(target=_run_classification_background, args=(uid,), daemon=True).start()
    return JSONResponse({"status": "ok", "message": "Classification started"})


@app.get("/api/classify/status")
def api_classify_status(request: Request):
    """Return current classification job state."""
    if not _is_authenticated(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    return JSONResponse(
        {
            "running": _classify_state["running"],
            "result": _classify_state["result"],
        }
    )


@app.get("/api/sync/result")
def api_sync_result(request: Request):
    """Return the result of the last completed sync."""
    if not _is_authenticated(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    import json as _json

    from server.sync_lock import acquire_sync_lock

    conn = get_db(_db_path())
    try:
        row = conn.execute(
            "SELECT value FROM app_config WHERE key = ?", ("last_sync_result",)
        ).fetchone()
        # Check if sync is still running
        lock = acquire_sync_lock(timeout=0)
        running = lock is None
        if lock:
            lock.close()
        result = _json.loads(row["value"]) if row else {}
        result["running"] = running
        return JSONResponse(result)
    finally:
        conn.close()


@app.get("/api/summary")
def api_summary(request: Request, period: str = "this_month"):
    """Return spending/savings summary for the given period.

    Valid period values: ``this_month``, ``last_month``, ``this_year``,
    ``ytd``, or a specific ``YYYY-MM`` / ``YYYY`` string.

    Auto-fallback: when ``period=this_month`` and the result has no data
    (income, expenses, and savings_contributions all zero), the endpoint
    automatically returns last month's data instead and includes a
    ``period_label`` field (e.g. ``"Last Month"``) so the caller knows
    which period was actually used.
    """
    if not _is_authenticated(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    from datetime import date as _date

    from server.main import summary as _summary

    # Translate UI-friendly period names to the values summary() expects.
    # summary() accepts: "month", "year", "ytd", "YYYY-MM", "YYYY".
    # The UI (and this endpoint's query param) uses: "this_month", "last_month",
    # "this_year", "ytd", "YYYY-MM", "YYYY".
    _today = _date.today()
    original_period = period
    if period == "this_month":
        period = "month"
    elif period == "this_year":
        period = "year"
    elif period == "last_month":
        if _today.month == 1:
            period = f"{_today.year - 1}-12"
        else:
            period = f"{_today.year}-{_today.month - 1:02d}"

    try:
        result = _summary(period)
        # Auto-fallback: if this_month has no data, try last_month instead.
        if (
            original_period == "this_month"
            and result.get("income", 0) == 0
            and result.get("expenses", 0) == 0
            and result.get("savings_contributions", 0) == 0
        ):
            if _today.month == 1:
                last_month_period = f"{_today.year - 1}-12"
            else:
                last_month_period = f"{_today.year}-{_today.month - 1:02d}"
            last_result = _summary(last_month_period)
            if (
                last_result.get("income", 0) != 0
                or last_result.get("expenses", 0) != 0
                or last_result.get("savings_contributions", 0) != 0
            ):
                last_result["period_label"] = "Last Month"
                return JSONResponse(last_result)
        result["period_label"] = "This Month"
        return JSONResponse(result)
    except Exception as exc:  # pragma: no cover
        return JSONResponse({"error": str(exc)}, status_code=500)


def _get_dashboard_widgets() -> dict:
    """Server-side data for the dashboard widgets (#297).

    Returns:
        recent_transactions: last 8 classified transactions (most recent first)
        needs_review_count:  number of transactions awaiting manual review
        top_categories:      top 5 line items by spending this month
    """
    from datetime import date as _date

    today = _date.today()
    # Month range
    month_start = today.replace(day=1).isoformat()
    month_end = today.isoformat()

    conn = get_db(_db_path())
    try:
        from ui.auth import get_active_user_id

        uid = get_active_user_id(_db_path())

        # ── Recent transactions ──────────────────────────────────────────
        # Pull 8 most-recent transactions whose entry was classified for
        # this user, joined with the line-item name and amount sign.
        if uid:
            recent_rows = conn.execute(
                """
                SELECT t.id, t.merchant, t.amount, t.date, t.currency,
                       t.pending, ba.name AS account_name,
                       li.name AS line_item_name,
                       te.entry_type
                  FROM transactions t
                  JOIN bank_accounts ba ON ba.id = t.bank_account_id
                  JOIN bank_connections bc ON bc.id = ba.connection_id
                  LEFT JOIN transaction_entries te ON te.transaction_id = t.id
                  LEFT JOIN line_items li ON li.id = te.line_item_id
                 WHERE bc.user_id = ?
                 ORDER BY t.date DESC, t.id DESC
                 LIMIT 8
                """,
                (uid,),
            ).fetchall()
        else:
            recent_rows = []
        recent_transactions = [
            {
                "id": r["id"],
                "merchant": r["merchant"] or "(unknown)",
                "amount": r["amount"],
                "date": r["date"],
                "currency": r["currency"] or "CAD",
                "pending": bool(r["pending"]),
                "account_name": r["account_name"],
                "line_item_name": r["line_item_name"],
                "entry_type": r["entry_type"],
            }
            for r in recent_rows
        ]

        # ── Needs-review count ───────────────────────────────────────────
        if uid:
            cnt = conn.execute(
                """
                SELECT COUNT(*) AS c
                  FROM transaction_entries te
                  JOIN transactions t ON t.id = te.transaction_id
                  JOIN bank_accounts ba ON ba.id = t.bank_account_id
                  JOIN bank_connections bc ON bc.id = ba.connection_id
                 WHERE bc.user_id = ?
                   AND (te.uncertain = 1 OR te.line_item_id IS NULL)
                """,
                (uid,),
            ).fetchone()
            needs_review_count = int(cnt["c"] or 0) if cnt else 0
        else:
            needs_review_count = 0

        # ── Top categories (this month, by absolute spending) ───────────
        if uid:
            top_rows = conn.execute(
                """
                SELECT li.name AS line_item_name, l.name AS ledger_name,
                       SUM(ABS(te.amount)) AS total,
                       COUNT(*) AS tx_count
                  FROM transaction_entries te
                  JOIN transactions t ON t.id = te.transaction_id
                  JOIN bank_accounts ba ON ba.id = t.bank_account_id
                  JOIN bank_connections bc ON bc.id = ba.connection_id
                  JOIN line_items li ON li.id = te.line_item_id
                  JOIN ledgers l ON l.id = li.ledger_id
                 WHERE bc.user_id = ?
                   AND te.entry_type = 'spending'
                   AND t.date >= ? AND t.date <= ?
                 GROUP BY te.line_item_id
                 ORDER BY total DESC
                 LIMIT 5
                """,
                (uid, month_start, month_end),
            ).fetchall()
        else:
            top_rows = []
        top_categories = [
            {
                "line_item_name": r["line_item_name"],
                "ledger_name": r["ledger_name"],
                "total": float(r["total"] or 0),
                "tx_count": int(r["tx_count"] or 0),
            }
            for r in top_rows
        ]
        max_total = max((c["total"] for c in top_categories), default=0) or 1.0
        for c in top_categories:
            c["percent"] = round(100 * c["total"] / max_total, 1)

        return {
            "recent_transactions": recent_transactions,
            "needs_review_count": needs_review_count,
            "top_categories": top_categories,
        }
    finally:
        conn.close()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_get(request: Request):
    """Main dashboard page.  Requires authentication."""
    if not _is_authenticated(request):
        return _redirect("/login")
    uid = _current_user_id(request)
    last_synced = _get_last_synced_at()
    try:
        widgets = _get_dashboard_widgets()
    except Exception:
        # Widgets are non-critical — if the query fails for any reason,
        # render the page without them rather than crashing.
        widgets = {
            "recent_transactions": [],
            "needs_review_count": 0,
            "top_categories": [],
        }
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "current_page": "dashboard",
            "last_synced_at": last_synced,
            "action_result": None,
            "reauth_connections": _get_reauth_connections(uid),
            **widgets,
        },
    )


# ── /accounts (#158) ────────────────────────────────────────────────────────


def _get_accounts_grouped(user_id: Optional[str] = None) -> dict:
    """Return bank accounts grouped by institution, with balances and owner info.

    Returns a dict of {institution_name: {connection_id, accounts, hidden_count,
    syncing, owners}}.  Connections with no bank_accounts yet are included with
    syncing=True so the UI can show a placeholder immediately after linking.

    Each account row now includes ``owner_label`` (SimpleFIN org name) and
    ``inferred_type`` (type inferred from account name, e.g. "credit" for Visas)
    so the UI can separate accounts by owner and display correct liability signs.
    (See RIDVAN_REQUESTS.md items 2, 3, 5 and 6.)
    """
    conn = get_db(_db_path())
    try:
        # --- Connections (always present after link) ---
        if user_id:
            bc_rows = conn.execute(
                "SELECT id, institution_name, last_synced_at"
                "  FROM bank_connections WHERE user_id = ?"
                " ORDER BY institution_name",
                (user_id,),
            ).fetchall()
        else:
            bc_rows = conn.execute(
                "SELECT id, institution_name, last_synced_at"
                "  FROM bank_connections ORDER BY institution_name"
            ).fetchall()

        # Seed grouped with every connection so newly-linked banks appear immediately
        grouped: dict = {}
        for bc in bc_rows:
            grouped[bc["institution_name"] or "Unknown Institution"] = {
                "connection_id": bc["id"],
                "accounts": [],
                "hidden_count": 0,
                "syncing": bc["last_synced_at"] is None,
            }

        # --- Accounts (populated after first sync) ---
        # Pull owner_label and inferred_type so the UI can display accurate
        # account types and separate by owner (RIDVAN_REQUESTS.md items 2, 3, 5).
        if user_id:
            rows = conn.execute(
                "SELECT ba.id, ba.name, ba.mask, ba.type, ba.subtype,"
                "       ba.balance_current, ba.balance_available, ba.description,"
                "       COALESCE(ba.currency, 'CAD') AS currency,"
                "       COALESCE(ba.is_duplicate, 0) AS is_duplicate,"
                "       ba.owner_label, ba.inferred_type,"
                "       bc.id AS connection_id, bc.institution_name"
                "  FROM bank_accounts ba"
                "  JOIN bank_connections bc ON bc.id = ba.connection_id"
                " WHERE bc.user_id = ?"
                "   AND ba.removed_at IS NULL"
                " ORDER BY bc.institution_name, ba.owner_label, ba.name",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ba.id, ba.name, ba.mask, ba.type, ba.subtype,"
                "       ba.balance_current, ba.balance_available, ba.description,"
                "       COALESCE(ba.currency, 'CAD') AS currency,"
                "       COALESCE(ba.is_duplicate, 0) AS is_duplicate,"
                "       ba.owner_label, ba.inferred_type,"
                "       bc.id AS connection_id, bc.institution_name"
                "  FROM bank_accounts ba"
                "  JOIN bank_connections bc ON bc.id = ba.connection_id"
                " WHERE ba.removed_at IS NULL"
                " ORDER BY bc.institution_name, ba.owner_label, ba.name"
            ).fetchall()

        # Track seen account IDs per institution to prevent double-render.
        # An account can only appear in one institution group; if it somehow
        # ends up in two query rows (schema bug), we suppress the duplicate here.
        seen_account_ids: set[str] = set()

        for r in rows:
            inst = r["institution_name"] or "Unknown Institution"
            if inst not in grouped:
                grouped[inst] = {
                    "connection_id": r["connection_id"],
                    "accounts": [],
                    "hidden_count": 0,
                    "syncing": False,
                    "owners": [],  # distinct owner_labels seen in this institution group
                }
            grouped[inst]["syncing"] = False  # has accounts → not syncing
            if r["is_duplicate"]:
                grouped[inst]["hidden_count"] += 1
            else:
                acct_id = r["id"]
                # Guard: skip if this account was already added (prevents double-render
                # if a schema/join quirk returns the same account_id twice).
                if acct_id in seen_account_ids:
                    continue
                seen_account_ids.add(acct_id)
                acct_dict = dict(r)
                # Centralized FX conversion: compute balance_home for all
                # non-home-currency accounts so templates always have a
                # CAD-equivalent balance without doing any FX logic themselves.
                _acct_currency = (acct_dict.get("currency") or "CAD").upper()
                _home_cur = "CAD"  # default home currency
                _bal = acct_dict.get("balance_current")
                if _bal is not None and _acct_currency != _home_cur:
                    # Query FX rate directly from the already-open connection
                    _fx = conn.execute(
                        "SELECT rate FROM fx_rates WHERE base=? AND quote=?",
                        (_acct_currency, _home_cur),
                    ).fetchone()
                    acct_dict["balance_home"] = round(float(_bal) * _fx["rate"], 2) if _fx else None
                    acct_dict["balance_home_currency"] = _home_cur if _fx else _acct_currency
                else:
                    acct_dict["balance_home"] = _bal
                    acct_dict["balance_home_currency"] = _acct_currency
                grouped[inst]["accounts"].append(acct_dict)
                # Collect distinct owner_labels for the institution group so the
                # template can render a per-owner sub-heading when multiple owners
                # share the same bank connection.
                owner = r["owner_label"]
                owners_list = grouped[inst].setdefault("owners", [])
                if owner and owner not in owners_list:
                    owners_list.append(owner)
        return grouped
    except Exception:
        return {}
    finally:
        conn.close()


# Category ordering for the account list (RIDVAN_REQUESTS.md items 2, 3, 5, 6).
# Assets first (Cash → Investments), then liabilities (Credit Cards → Debts).
# Cash now absorbs chequing/checking AND savings (Ridvan's request 2026-10-18):
# one liquid "Cash" tier, Investments separate, then Credit Cards and Debts
# (loans/mortgages) as two DISTINCT liability buckets.
_CATEGORY_ORDER = ["Cash", "Investments", "Credit Cards", "Debts"]


def _account_category(acct: dict, txn_count: int = 0) -> str:
    """Map a single account row to a top-level display category.

    Categories (updated 2026-10-18 per Ridvan's request):
      * "Credit Cards" — credit-card liabilities (shown negative)
      * "Debts"        — loans / mortgages / credit lines (shown negative)
      * "Investments"  — investment accounts, incl. the balance-driven heuristic:
                         an account whose balance is non-trivial but which has NO
                         transactions behaves like an investment/holding account
                         (balance drifts without transactions).
      * "Cash"         — chequing/checking + savings + everyday/day-to-day cash
                         (savings is folded into Cash as one liquid tier).

    ``txn_count`` enables the balance-driven investment heuristic; pass the
    number of transactions recorded for this account over the sync window.
    """
    inferred = (acct.get("inferred_type") or "").lower()
    provider = (acct.get("type") or "").lower()
    effective = inferred if inferred and inferred != "depository" else (provider or "depository")
    subtype = (acct.get("subtype") or "").lower()

    # Liabilities: credit cards and loans/mortgages are now SEPARATE buckets.
    if effective == "credit":
        return "Credit Cards"
    if effective == "loan":
        return "Debts"

    # Explicit investment type/subtype.
    if effective == "investment" or subtype in (
        "401k",
        "403b",
        "457b",
        "ira",
        "roth",
        "sep ira",
        "simple ira",
        "tfsa",
        "rrsp",
        "fhsa",
        "rrif",
        "resp",
        "lira",
        "rdsp",
        "hsa",
        "529",
        "brokerage",
        "mutual fund",
        "etf",
    ):
        return "Investments"

    # Balance-driven investment heuristic (RIDVAN_REQUESTS.md item 6):
    # a funded account with zero transactions is treated as an investment/holding
    # account (its balance changes without transactions).  We require a
    # meaningful balance so that empty/dormant chequing accounts aren't swept in.
    balance = acct.get("balance_current")
    if balance is None:
        balance = acct.get("balance_available")
    try:
        balance_val = abs(float(balance)) if balance is not None else 0.0
    except (TypeError, ValueError):
        balance_val = 0.0
    if txn_count == 0 and balance_val >= 1000.0:
        return "Investments"

    # Savings is folded into Cash (Ridvan's request): chequing/checking, savings,
    # CDs, money-market and everyday cash all live in the single "Cash" tier.
    return "Cash"


def _get_accounts_by_category(user_id: Optional[str] = None) -> dict:
    """Return non-duplicate bank accounts grouped by CATEGORY then OWNER.

    Shape::

        {
          "Cash": {
             "accounts": [...],            # flat list (all owners)
             "owners": {"Ridvan": [...], "Dollar": [...]},  # owner → accounts
             "count": int,
          },
          "Debts": {...},
          ...
        }

    Categories are ordered Cash → Savings → Investments → Debts and only
    non-empty categories are returned.  Owner sub-grouping uses ``owner_label``
    (SimpleFIN org name); accounts with no owner label are grouped under the
    connection's institution name as a sensible fallback.
    (Implements RIDVAN_REQUESTS.md items 2 — group by account/category — and
    3 — owner separation.)
    """
    grouped = _get_accounts_grouped(user_id)

    # Pre-compute per-account transaction counts for the investment heuristic.
    txn_counts: dict = {}
    conn = get_db(_db_path())
    try:
        for row in conn.execute(
            "SELECT bank_account_id AS id, COUNT(*) AS n"
            "  FROM transactions GROUP BY bank_account_id"
        ).fetchall():
            txn_counts[row["id"]] = row["n"]
    except Exception:
        txn_counts = {}
    finally:
        conn.close()

    categories: dict = {}
    for inst_name, data in (grouped or {}).items():
        for acct in data.get("accounts", []):
            txn_count = txn_counts.get(acct.get("id"), 0)
            cat = _account_category(acct, txn_count=txn_count)
            # Owner fallback: SimpleFIN org label → else institution name.
            owner = acct.get("owner_label") or inst_name or "Other"

            bucket = categories.setdefault(cat, {"accounts": [], "owners": {}, "count": 0})
            bucket["accounts"].append(acct)
            bucket["owners"].setdefault(owner, []).append(acct)
            bucket["count"] += 1

    # Return categories in the canonical order, dropping empty ones.
    ordered: dict = {}
    for label in _CATEGORY_ORDER:
        if label in categories:
            ordered[label] = categories.pop(label)
    # Any unexpected categories trail at the end (defensive).
    for label, bucket in categories.items():
        ordered[label] = bucket
    return ordered


def _compute_net_worth(
    grouped: dict, home_currency: str = "CAD", txn_counts: Optional[dict] = None
) -> dict:
    """Compute a net-worth summary across the supplied grouped accounts (#322).

    Liability detection (RIDVAN_REQUESTS.md items 5 & 6):
      We check ``inferred_type`` first (populated by infer_account_type() in
      server.main from account names), then fall back to the provider ``type``.
      This fixes SimpleFIN accounts which are all typed ``"depository"`` by the
      provider even when they are credit cards or mortgages.

    Balance sign conventions:
      * depository → positive (asset)
      * credit / loan → NEGATIVE liability (balance represents amount owed)
      * investment → positive (asset, but in its own category)

    Investment detection (RIDVAN_REQUESTS.md item 6):
      Accounts with ``inferred_type == "investment"`` or provider
      ``type == "investment"`` go into the Investments bucket.

    Returns::

        {
          "home_currency": "CAD",
          "assets": float,        # sum of positive (asset) balances in home_currency
          "liabilities": float,   # sum of liability balances (positive number)
          "net_worth": float,     # assets - liabilities, in home_currency
          "breakdown": [{"label", "total", "count"}, …],
          "by_currency": {"CAD": {assets, liabilities, net}, "USD": …},
          "mixed_currency": bool,
          "has_data": bool,       # False when there are no real balances yet
        }

    When accounts hold currencies other than ``home_currency`` we surface
    a per-currency breakdown rather than silently mis-summing them.
    """
    # Per-account transaction counts drive the balance-driven investment
    # heuristic inside _account_category (a funded account with zero txns is
    # treated as an investment/holding).  Passed in by the caller so the Net
    # Worth summary and the Accounts listing below it use identical inputs.
    _txn_counts = txn_counts or {}

    def _effective_type(acct: dict) -> str:
        """Return the effective account type, preferring inferred_type over type.

        SimpleFIN hardcodes type=\"depository\" for all accounts.  inferred_type
        is populated by infer_account_type() in server.main from the account name
        (e.g. \"RBC Rewards+ Visa\" → \"credit\").  We prefer inferred_type when it
        exists and differs from the generic \"depository\" fallback.
        (See RIDVAN_REQUESTS.md items 5 & 6.)
        """
        inferred = (acct.get("inferred_type") or "").lower()
        provider = (acct.get("type") or "").lower()
        # Use inferred only when it resolved to something more specific.
        if inferred and inferred != "depository":
            return inferred
        return provider or "depository"

    def categorise(acct: dict) -> str:
        """Return a coarse bucket label for a single bank_account row.

        Delegates to the SAME categorizer used by the Accounts listing section
        (``_account_category``) so the Net Worth summary and the account list
        below it always agree on which bucket an account belongs to
        (Cash / Savings / Investments / Debts).  Previously this used a
        divergent rule set (subtype-only savings, no name-keyword or
        balance-driven heuristics, and split Debts into Credit Cards/Loans),
        which made the two sections look different for the same accounts.
        """
        tc = int((_txn_counts or {}).get(acct.get("id"), 0))
        return _account_category(acct, txn_count=tc)

    buckets: dict = {}  # label -> {total, count, currencies}
    by_currency: dict = {}  # currency -> {assets, liabilities, net}
    has_data = False

    for inst_name, data in (grouped or {}).items():
        for acct in data.get("accounts", []):
            balance = acct.get("balance_current")
            if balance is None:
                balance = acct.get("balance_available")
            if balance is None:
                continue  # never synced — skip
            has_data = True
            currency = (acct.get("currency") or home_currency).upper()
            # Liability detection: credit cards and loans have NEGATIVE net worth.
            # We use _effective_type() which respects inferred_type from account
            # names — this fixes SimpleFIN accounts typed as generic "depository".
            effective_t = _effective_type(acct)
            is_liability = effective_t in ("credit", "loan")
            # Net-worth sign: liabilities reduce net worth.
            value = -float(balance) if is_liability else float(balance)
            # Use balance_home (FX-converted) for breakdown totals if available.
            # This ensures the Accounts page category totals match the Dashboard.
            balance_home = acct.get("balance_home")
            if balance_home is not None:
                value_home = -float(balance_home) if is_liability else float(balance_home)
            else:
                value_home = value  # fallback to raw balance
            label = categorise(acct)

            b = buckets.setdefault(label, {"total": 0.0, "count": 0, "currencies": set()})
            b["total"] += value_home
            b["count"] += 1
            b["currencies"].add(currency)

            c = by_currency.setdefault(currency, {"assets": 0.0, "liabilities": 0.0, "net": 0.0})
            if is_liability:
                c["liabilities"] += float(balance)
            else:
                c["assets"] += float(balance)
            c["net"] = c["assets"] - c["liabilities"]

    # Stable ordering matches the Accounts listing section: Cash, Investments,
    # then the two separate liability buckets Credit Cards and Debts.
    order = ["Cash", "Investments", "Credit Cards", "Debts"]
    breakdown = []
    for label in order:
        if label in buckets:
            b = buckets.pop(label)
            breakdown.append(
                {
                    "label": label,
                    "total": b["total"],
                    "count": b["count"],
                    "is_liability": label in ("Credit Cards", "Debts"),
                }
            )
    # Any unknown buckets (e.g. a new Plaid subtype) trail at the end.
    for label, b in buckets.items():
        breakdown.append(
            {
                "label": label,
                "total": b["total"],
                "count": b["count"],
                "is_liability": False,
            }
        )

    currencies = set(by_currency.keys())
    mixed = len(currencies) > 1
    home_currency_upper = home_currency.upper()

    if not mixed and (home_currency_upper in currencies or not currencies):
        # All balances are in the user's home currency — safe to sum directly.
        c = by_currency.get(home_currency_upper, {"assets": 0.0, "liabilities": 0.0})
        assets = c["assets"]
        liabilities = c["liabilities"]
        net = assets - liabilities
    else:
        # Mixed currencies: convert every foreign currency to home currency
        # using live FX rates (cached <24h in fx_rates table).  Fall back to
        # home-currency-only slice if a rate cannot be fetched.
        assets = 0.0
        liabilities = 0.0
        fx_ok = True
        for cur, c in by_currency.items():
            if cur == home_currency_upper:
                assets += c["assets"]
                liabilities += c["liabilities"]
            else:
                rate = _get_or_fetch_fx_rate(cur, home_currency_upper)
                if rate is None:
                    fx_ok = False  # couldn't convert — degrade gracefully
                    logging.getLogger(__name__).warning(
                        "_compute_net_worth: no FX rate %s→%s, skipping %s",
                        cur,
                        home_currency_upper,
                        cur,
                    )
                else:
                    assets += c["assets"] * rate
                    liabilities += c["liabilities"] * rate
        net = assets - liabilities
        mixed = mixed and not fx_ok  # hide the fx hint once we've converted

    return {
        "home_currency": home_currency_upper,
        "assets": assets,
        "liabilities": liabilities,
        "net_worth": net,
        "breakdown": breakdown,
        "by_currency": by_currency,
        "mixed_currency": mixed,
        "has_data": has_data,
    }


def _get_or_fetch_fx_rate(base: str, quote: str) -> float | None:
    """Return a live (or cached <24h) FX rate: 1 base = X quote.

    Checks the fx_rates DB table first; if stale or missing, fetches from
    the Frankfurter API (free, no key needed: api.frankfurter.app).
    Returns None on any failure so callers can fall back gracefully.
    """
    if base == quote:
        return 1.0
    try:
        db = get_db(_db_path())
        try:
            now = int(time.time())
            row = db.execute(
                "SELECT rate, fetched_at FROM fx_rates WHERE base=? AND quote=?",
                (base, quote),
            ).fetchone()
            if row and (now - row["fetched_at"]) < 86400:  # <24h — reuse
                return float(row["rate"])
        finally:
            db.close()
    except Exception:
        pass
    # Fetch fresh rate from Frankfurter (ECB-sourced, free, no key)
    try:
        url = f"https://api.frankfurter.app/latest?from={base}&to={quote}"
        req = urllib.request.Request(url, headers={"User-Agent": "friday-budgeting-pro/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        rate = float(data["rates"][quote])
        # Persist in fx_rates table
        try:
            db2 = get_db(_db_path())
            try:
                db2.execute(
                    "INSERT OR REPLACE INTO fx_rates (base, quote, rate, fetched_at) VALUES (?,?,?,?)",
                    (base, quote, rate, int(time.time())),
                )
                db2.commit()
            finally:
                db2.close()
        except Exception:
            pass
        return rate
    except Exception as exc:
        logging.getLogger(__name__).warning("FX rate fetch failed %s→%s: %s", base, quote, exc)
        return None


def _get_home_currency(uid: Optional[str]) -> str:
    """Look up the active user's home currency, falling back to CAD."""
    if not uid:
        return "CAD"
    try:
        conn = get_db(_db_path())
        try:
            row = conn.execute("SELECT home_currency FROM users WHERE id = ?", (uid,)).fetchone()
            if row and row["home_currency"]:
                return str(row["home_currency"]).upper()
        finally:
            conn.close()
    except Exception:
        pass
    return "CAD"


@app.get("/accounts", response_class=HTMLResponse)
def accounts_get(request: Request):
    """Accounts page — bank accounts grouped by CATEGORY then OWNER.

    Primary layout (RIDVAN_REQUESTS.md items 2 & 3): accounts are grouped into
    top-level categories (Cash / Savings / Investments / Debts) and, within each
    category, sub-grouped by owner (SimpleFIN org label, falling back to the
    institution name).  Liabilities (credit cards + mortgages/loans) land in the
    Debts category and are shown with negative balances.

    ``grouped_accounts`` (by institution) is still passed for the per-connection
    actions (Copy token / Disconnect) and the syncing placeholders.
    """
    if not _is_authenticated(request):
        return _redirect("/login")
    uid = _current_user_id(request)
    grouped = _get_accounts_grouped(uid)
    categorized = _get_accounts_by_category(uid)
    home_currency = _get_home_currency(uid)
    # Per-account txn counts so the Net Worth summary uses the same
    # balance-driven investment heuristic as the Accounts listing below it.
    _nw_txn_counts: dict = {}
    try:
        _nwc = get_db(_db_path())
        try:
            for _r in _nwc.execute(
                "SELECT bank_account_id AS id, COUNT(*) AS n"
                "  FROM transactions GROUP BY bank_account_id"
            ).fetchall():
                _nw_txn_counts[_r["id"]] = _r["n"]
        finally:
            _nwc.close()
    except Exception:
        _nw_txn_counts = {}
    try:
        net_worth = _compute_net_worth(
            grouped, home_currency=home_currency, txn_counts=_nw_txn_counts
        )
    except Exception:
        net_worth = None
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "current_page": "accounts",
            "grouped_accounts": grouped,
            "categorized_accounts": categorized,
            "net_worth": net_worth,
            "reauth_connections": _get_reauth_connections(uid),
        },
    )


@app.get("/accounts/{account_id}/transactions")
def account_transactions_get(request: Request, account_id: str, limit: int = 50):
    """Return recent transactions for an account with classification info.

    Requires authentication.  Returns JSON:
    {"transactions": [{id, date, merchant, amount, currency, entry_type,
                        source, uncertain, line_item_name, ledger_name}, ...]}
    """
    if not _is_authenticated(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    conn = get_db(_db_path())
    try:
        rows = conn.execute(
            """
            SELECT t.id, t.date, t.authorized_datetime, t.merchant, t.amount,
                   t.pending, t.notes,
                   COALESCE(t.currency, 'CAD') AS currency,
                   te.entry_type, te.source, te.uncertain,
                   li.name AS line_item_name,
                   l.name  AS ledger_name
              FROM transactions t
              LEFT JOIN transaction_entries te ON te.transaction_id = t.id
              LEFT JOIN line_items li ON li.id = te.line_item_id
              LEFT JOIN ledgers    l  ON l.id  = te.ledger_id
             WHERE t.bank_account_id = ?
             ORDER BY
               t.pending DESC,
               COALESCE(t.authorized_datetime, t.date) DESC,
               t.rowid DESC
             LIMIT ?
            """,
            (account_id, limit),
        ).fetchall()
        return JSONResponse({"transactions": [dict(r) for r in rows]})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        conn.close()


@app.get("/accounts/{connection_id}/access-token")
def accounts_access_token_get(request: Request, connection_id: str):
    """Return the decrypted Plaid access token for a bank connection.

    Requires authentication.  Intended for personal use only — this is a
    local single-user app and the token is needed for direct Plaid API calls.

    Returns JSON: {"access_token": "access-production-..."}
    """
    if not _is_authenticated(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    conn = get_db(_db_path())
    try:
        row = conn.execute(
            "SELECT plaid_access_token_encrypted FROM bank_connections WHERE id = ?",
            (connection_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return JSONResponse({"error": "connection not found"}, status_code=404)
    try:
        import server.crypto

        access_token = server.crypto.decrypt(row["plaid_access_token_encrypted"])
    except Exception as exc:
        return JSONResponse({"error": f"decrypt failed: {exc}"}, status_code=500)
    return JSONResponse({"access_token": access_token})


@app.patch("/accounts/{account_id}/name")
async def accounts_name_patch(request: Request, account_id: str):
    """Update the display name for a bank account (inline rename).  Requires auth.

    Reads JSON body: {"name": "<new name>"}.
    Returns {"status": "ok", "account_id": ..., "name": ...} or 404.
    """
    if not _is_authenticated(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    conn = get_db(_db_path())
    try:
        result = conn.execute(
            "UPDATE bank_accounts SET name = ? WHERE id = ?",
            (name, account_id),
        )
        conn.commit()
        if result.rowcount == 0:
            return JSONResponse({"error": f"account {account_id!r} not found"}, status_code=404)
    finally:
        conn.close()
    return JSONResponse({"status": "ok", "account_id": account_id, "name": name})


# ── Transaction notes ────────────────────────────────────────────────────────

@app.patch("/api/transactions/{transaction_id}/notes")
async def transaction_notes_patch(request: Request, transaction_id: str):
    """Update or clear the notes on a transaction.

    Reads JSON body: {"notes": "<text or null>"}
    Returns {"status": "ok", "transaction_id": ..., "notes": ...} or 404.
    """
    if not _is_authenticated(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    body = await request.json()
    notes = body.get("notes")
    if notes is not None:
        notes = notes.strip() or None  # empty string -> NULL
    conn = get_db(_db_path())
    try:
        result = conn.execute(
            "UPDATE transactions SET notes = ? WHERE id = ?",
            (notes, transaction_id),
        )
        conn.commit()
        if result.rowcount == 0:
            return JSONResponse({"error": f"transaction {transaction_id!r} not found"}, status_code=404)
    finally:
        conn.close()
    return JSONResponse({"status": "ok", "transaction_id": transaction_id, "notes": notes})


@app.get("/api/transactions/{transaction_id}")
def transaction_get(request: Request, transaction_id: str):
    """Get a single transaction with full details including notes."""
    if not _is_authenticated(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    conn = get_db(_db_path())
    try:
        row = conn.execute(
            """
            SELECT t.id, t.date, t.merchant, t.amount, t.currency, t.notes,
                   t.plaid_category, t.plaid_category_detailed,
                   ba.name AS account_name, ba.mask AS account_mask,
                   bc.institution_name,
                   te.entry_type, te.source, te.reviewed, te.uncertain,
                   li.name AS line_item_name,
                   l.name AS ledger_name
              FROM transactions t
              JOIN bank_accounts ba ON ba.id = t.bank_account_id
              JOIN bank_connections bc ON bc.id = ba.connection_id
              LEFT JOIN transaction_entries te ON te.transaction_id = t.id
              LEFT JOIN line_items li ON li.id = te.line_item_id
              LEFT JOIN ledgers l ON l.id = te.ledger_id
             WHERE t.id = ?
            """,
            (transaction_id,),
        ).fetchone()
        if row is None:
            return JSONResponse({"error": "transaction not found"}, status_code=404)
        return JSONResponse(dict(row))
    finally:
        conn.close()


# ── /settings (#159, #161) ───────────────────────────────────────────────────

_VALID_CURRENCIES = ["CAD", "USD", "EUR", "GBP"]
_VALID_TIMEZONES = [
    "America/Toronto",
    "America/New_York",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Berlin",
    "Asia/Tokyo",
    "UTC",
]


@app.get("/settings", response_class=HTMLResponse)
def settings_get(request: Request):
    """Settings page.  Requires authentication."""
    if not _is_authenticated(request):
        return _redirect("/login")
    uid = _current_user_id(request)
    conn = get_db(_db_path())
    try:
        row = conn.execute(
            "SELECT home_currency, timezone, sync_window_days, classify_batch_size, "
            "classifier_model FROM users WHERE id = ?",
            (uid,),
        ).fetchone()
        home_currency = row["home_currency"] if row and row["home_currency"] else "CAD"
        timezone = row["timezone"] if row and row["timezone"] else "America/Toronto"
        sync_window_days = (
            row["sync_window_days"] if row and row["sync_window_days"] is not None else 90
        )
        classify_batch_size = (
            row["classify_batch_size"] if row and row["classify_batch_size"] is not None else 50
        )
        classifier_model = (
            row["classifier_model"]
            if row and row["classifier_model"]
            else "claude-3-5-haiku-20241022"
        )
    finally:
        conn.close()
    saved = request.query_params.get("saved") == "1"
    # Load classification rules (gracefully degrade if DB not ready)
    try:
        rules = _list_rules().get("rules", [])
    except Exception:
        rules = []
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "current_page": "settings",
            "home_currency": home_currency,
            "currencies": _VALID_CURRENCIES,
            "timezone": timezone,
            "timezones": _VALID_TIMEZONES,
            "sync_window_days": sync_window_days,
            "classify_batch_size": classify_batch_size,
            "classifier_model": classifier_model,
            "saved": saved,
            "rules": rules,
            "reauth_connections": _get_reauth_connections(uid),
        },
    )


@app.post("/settings", response_class=HTMLResponse)
async def settings_post(request: Request):
    """Save settings.  Requires authentication."""
    if not _is_authenticated(request):
        return _redirect("/login")
    uid = _current_user_id(request)
    form = await request.form()
    home_currency = (form.get("home_currency") or "").strip().upper()
    # timezone is optional in the form: if not submitted keep the existing value
    submitted_tz = (form.get("timezone") or "").strip()
    # Sync-safety settings (#270) — optional in the form.
    submitted_window = (form.get("sync_window_days") or "").strip()
    submitted_batch = (form.get("classify_batch_size") or "").strip()
    submitted_model = (form.get("classifier_model") or "").strip()

    # Load current values for fallback + error re-render
    conn = get_db(_db_path())
    try:
        row = conn.execute(
            "SELECT home_currency, timezone FROM users WHERE id = ?", (uid,)
        ).fetchone()
        current_currency = row["home_currency"] if row and row["home_currency"] else "CAD"
        current_tz = row["timezone"] if row and row["timezone"] else "America/Toronto"
    finally:
        conn.close()

    # Use submitted timezone if provided, otherwise keep existing value
    timezone = submitted_tz if submitted_tz else current_tz

    # Validate
    errors = []
    if home_currency not in _VALID_CURRENCIES:
        errors.append(f"Invalid currency: {home_currency!r}. Choose one of {_VALID_CURRENCIES}.")
    if not timezone:
        errors.append("Timezone must not be empty.")

    # Sync-safety validation: keep existing value when field not submitted.
    sync_window_days = 90
    classify_batch_size = 50
    classifier_model = "claude-3-5-haiku-20241022"
    _conn_cur = get_db(_db_path())
    try:
        _cur_row = _conn_cur.execute(
            "SELECT sync_window_days, classify_batch_size, classifier_model "
            "FROM users WHERE id = ?",
            (uid,),
        ).fetchone()
        if _cur_row:
            if _cur_row["sync_window_days"] is not None:
                sync_window_days = int(_cur_row["sync_window_days"])
            if _cur_row["classify_batch_size"] is not None:
                classify_batch_size = int(_cur_row["classify_batch_size"])
            if _cur_row["classifier_model"]:
                classifier_model = _cur_row["classifier_model"]
    finally:
        _conn_cur.close()

    if submitted_window:
        try:
            _w = int(submitted_window)
            if _w < 1 or _w > 3650:
                errors.append("Sync window (days) must be between 1 and 3650.")
            else:
                sync_window_days = _w
        except ValueError:
            errors.append("Sync window (days) must be a whole number.")
    if submitted_batch:
        try:
            _b = int(submitted_batch)
            if _b < 1 or _b > 1000:
                errors.append("Classify batch size must be between 1 and 1000.")
            else:
                classify_batch_size = _b
        except ValueError:
            errors.append("Classify batch size must be a whole number.")
    if submitted_model:
        classifier_model = submitted_model

    if errors:
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "current_page": "settings",
                "home_currency": current_currency,
                "currencies": _VALID_CURRENCIES,
                "timezone": current_tz,
                "timezones": _VALID_TIMEZONES,
                "sync_window_days": sync_window_days,
                "classify_batch_size": classify_batch_size,
                "classifier_model": classifier_model,
                "saved": False,
                "error": " ".join(errors),
                "reauth_connections": _get_reauth_connections(uid),
            },
        )
    conn = get_db(_db_path())
    try:
        conn.execute(
            "UPDATE users SET home_currency = ?, timezone = ?, sync_window_days = ?, "
            "classify_batch_size = ?, classifier_model = ? WHERE id = ?",
            (
                home_currency,
                timezone,
                sync_window_days,
                classify_batch_size,
                classifier_model,
                uid,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return _redirect("/settings?saved=1")


# ── /profile ─────────────────────────────────────────────────────────────────


def _fmt_last_synced(ts) -> str:
    """Convert a Unix timestamp integer to a human-readable string, or 'Never'.

    Kept as a server-side fallback (e.g. for tests / non-JS contexts).
    The UI renders timestamps client-side via data-utc spans instead.
    """
    from datetime import datetime

    if not ts:
        return "Never"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%b %-d, %Y %-I:%M %p")
    except Exception:
        return "Never"


def _get_connections(user_id: Optional[str] = None) -> list[dict]:
    """Query bank_connections for *user_id* and return a list of dicts."""
    conn = get_db(_db_path())
    try:
        if user_id:
            rows = conn.execute(
                "SELECT id, institution_name, status, last_synced_at "
                "FROM bank_connections WHERE user_id = ? ORDER BY rowid",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, institution_name, status, last_synced_at "
                "FROM bank_connections ORDER BY rowid"
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # Keep last_synced_at as raw UTC integer; profile template renders
            # it client-side via .datetime-local[data-utc] JS.  Zero / None → 0.
            raw = d.get("last_synced_at")
            d["last_synced_at"] = int(raw) if raw else 0
            result.append(d)
        return result
    finally:
        conn.close()


def _has_reauth_needed(user_id: Optional[str] = None) -> bool:
    """Return True if any bank connection for user_id has needs_reauth status."""
    return bool(_get_reauth_connections(user_id))


def _get_reauth_connections(user_id: Optional[str] = None) -> list[dict]:
    """Return list of bank connections with needs_reauth status for the user."""
    db = get_db(_db_path())
    try:
        if user_id:
            rows = db.execute(
                "SELECT id, institution_name FROM bank_connections"
                " WHERE user_id = ? AND status = 'needs_reauth'",
                (user_id,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, institution_name FROM bank_connections" " WHERE status = 'needs_reauth'"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def _get_accounts(user_id: Optional[str] = None) -> list[dict]:
    """Query bank_accounts joined with their connection and return list of dicts."""
    conn = get_db(_db_path())
    try:
        if user_id:
            rows = conn.execute(
                "SELECT ba.id, ba.name, ba.mask, ba.type, ba.subtype, ba.description,"
                "       bc.institution_name"
                "  FROM bank_accounts ba"
                "  JOIN bank_connections bc ON bc.id = ba.connection_id"
                " WHERE bc.user_id = ?"
                " ORDER BY bc.institution_name, ba.name",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ba.id, ba.name, ba.mask, ba.type, ba.subtype, ba.description,"
                "       bc.institution_name"
                "  FROM bank_accounts ba"
                "  JOIN bank_connections bc ON bc.id = ba.connection_id"
                " ORDER BY bc.institution_name, ba.name"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


@app.get("/profile", response_class=HTMLResponse)
def profile_get(request: Request):
    """Settings page.  Requires authentication."""
    if _is_authenticated(request):
        uid = _current_user_id(request)
        pref = _get_notification_pref()
        connections = _get_connections(uid)
        accounts = _get_accounts(uid)
        return templates.TemplateResponse(
            request,
            "profile.html",
            {
                "current_page": "profile",
                "notification_pref": pref,
                "saved": False,
                "connections": connections,
                "accounts": accounts,
                "action_result": None,
                "reauth_connections": [c for c in connections if c.get("status") == "needs_reauth"],
            },
        )
    st = request.cookies.get(_SETUP_COMPLETE_COOKIE)
    if st and _check_raw_session(_db_path(), st):
        pref = _get_notification_pref()
        uid = None
        conn_db = get_db(_db_path())
        try:
            row = conn_db.execute("SELECT user_id FROM sessions WHERE id = ?", (st,)).fetchone()
            if row:
                uid = row["user_id"]
        except Exception:
            pass
        finally:
            conn_db.close()
        connections = _get_connections(uid)
        accounts = _get_accounts(uid)
        resp = templates.TemplateResponse(
            request,
            "profile.html",
            {
                "current_page": "profile",
                "notification_pref": pref,
                "saved": False,
                "connections": connections,
                "accounts": accounts,
                "action_result": None,
                "reauth_connections": [c for c in connections if c.get("status") == "needs_reauth"],
            },
        )
        resp.set_cookie(SESSION_COOKIE, st, httponly=True, samesite="lax", secure=True)
        resp.delete_cookie(_SETUP_COMPLETE_COOKIE)
        return resp
    resp2 = _redirect("/login")
    if request.cookies.get(_SETUP_COMPLETE_COOKIE):
        resp2.delete_cookie(_SETUP_COMPLETE_COOKIE)
    return resp2


@app.post("/profile", response_class=HTMLResponse)
async def profile_post(request: Request):
    """Save settings and handle linked-account actions.  Requires authentication.

    Actions (via hidden 'action' field):
      - (none / save_settings): save notification_pref
      - sync_now: trigger server.main.sync()
      - export_now: trigger server.main.export_excel()
      - disconnect_bank: delete bank_connection by bank_id
      - reconnect_bank: call server.main.refresh_connection(id=bank_id)
    """
    if not _is_authenticated(request):
        return _redirect("/login")
    uid = _current_user_id(request)
    form = await request.form()
    action = (form.get("action") or "").strip()
    pref = _get_notification_pref()
    connections = _get_connections(uid)
    accounts = _get_accounts(uid)
    action_result: dict | None = None

    if action == "sync_now":
        import server.main as _sm

        try:
            result = _sm.sync()
            action_result = {"ok": True, "message": "Sync complete.", "detail": result}
        except Exception as exc:
            action_result = {"ok": False, "message": f"Sync failed: {exc}"}

    elif action == "export_now":
        import server.main as _sm

        try:
            result = _sm.export_excel()
            path = result.get("path", "")
            action_result = {"ok": True, "message": f"Export saved to: {path}"}
        except Exception as exc:
            action_result = {"ok": False, "message": f"Export failed: {exc}"}

    elif action == "disconnect_bank":
        bank_id = (form.get("bank_id") or "").strip()
        if not bank_id:
            action_result = {"ok": False, "message": "No bank_id provided."}
        else:
            import server.main as _sm

            try:
                _sm.disconnect(id=bank_id)
                # Redirect to /accounts, or /setup if no connections remain
                remaining = _get_connections(uid)
                if remaining:
                    return _redirect("/accounts")
                else:
                    return _redirect("/setup")
            except Exception as exc:
                action_result = {"ok": False, "message": f"Disconnect failed: {exc}"}

    elif action == "reconnect_bank":
        bank_id = (form.get("bank_id") or "").strip()
        if not bank_id:
            action_result = {"ok": False, "message": "No bank_id provided."}
        else:
            import server.main as _sm

            try:
                result = _sm.refresh_connection(id=bank_id)
                url = result.get("url", "")
                action_result = {"ok": True, "message": f"Reconnect here: {url}", "url": url}
            except Exception as exc:
                action_result = {"ok": False, "message": f"Reconnect failed: {exc}"}

    else:
        # Default: save notification_pref
        pref = form.get("notification_pref") or "openclaw"
        _set_notification_pref(pref)
        return templates.TemplateResponse(
            request,
            "profile.html",
            {
                "current_page": "profile",
                "notification_pref": pref,
                "saved": True,
                "connections": connections,
                "accounts": accounts,
                "action_result": None,
                "reauth_connections": [c for c in connections if c.get("status") == "needs_reauth"],
            },
        )

    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "current_page": "profile",
            "notification_pref": pref,
            "saved": False,
            "connections": connections,
            "accounts": accounts,
            "action_result": action_result,
            "reauth_connections": [c for c in connections if c.get("status") == "needs_reauth"],
        },
    )


# ── /profile/accounts/{account_id}/description ──────────────────────────────────────────────────


@app.patch("/profile/accounts/{account_id}/description")
async def account_description_patch(request: Request, account_id: str):
    """Save a user-supplied description for a bank account.

    Requires authentication.  Reads JSON body: {"description": "<text>"}.
    Returns {"status": "ok", "account_id": ...} or 404 if not found.
    """
    from fastapi.responses import JSONResponse

    if not _is_authenticated(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)

    body = await request.json()
    description = (body.get("description") or "").strip() or None

    conn = get_db(_db_path())
    try:
        result = conn.execute(
            "UPDATE bank_accounts SET description = ? WHERE id = ?",
            (description, account_id),
        )
        conn.commit()
        if result.rowcount == 0:
            return JSONResponse({"error": f"account_id {account_id!r} not found"}, status_code=404)
    finally:
        conn.close()

    return JSONResponse({"status": "ok", "account_id": account_id})


# ── /reconnect/{connection_id} ────────────────────────────────────────────────


@app.get("/reconnect/{connection_id}")
def reconnect_bank(request: Request, connection_id: str):
    """Direct reauth route for a specific bank connection.

    Requires authentication.  Generates a Plaid Link token in Update Mode
    for the given connection and redirects straight to the Plaid Link page.
    The user sees their bank pre-loaded — no "choose your bank" screen, no
    extra navigation, no re-login.

    Used by the global reauth banner in base.html so reconnecting from any
    page goes directly to Plaid without touching the profile form flow.
    """
    if not _is_authenticated(request):
        return _redirect("/login")

    import server.main as _sm

    try:
        result = _sm.refresh_connection(id=connection_id)
        url = result.get("url", "")
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).error("reconnect_bank failed: %s", exc)
        return _redirect("/accounts?error=reconnect_failed")

    if not url:
        return _redirect("/accounts?error=reconnect_no_url")

    # The URL points to /link?token=... on this same server; redirect there.
    return _redirect(url)


# ── /ledgers ─────────────────────────────────────────────────────────────────


@app.get("/export/excel")
def export_excel_download(request: Request):
    """Stream an Excel export as a browser download."""
    if not _is_authenticated(request):
        return _redirect("/login")

    import datetime

    from fastapi.responses import Response as _Response

    from server.excel_export import export_excel_bytes

    with get_db(_db_path()) as conn:
        data = export_excel_bytes(conn)

    filename = f"friday-budget-{datetime.date.today().strftime('%Y-%m')}.xlsx"
    return _Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


_VALID_PERIODS = {"this_month", "last_month", "last_3_months", "this_year", "all"}


@app.get("/ledgers", response_class=HTMLResponse)
def ledgers_get(request: Request, period: str = "this_month"):
    """Read-only ledger tree with optional date-range filter.  Requires authentication.

    Queries the DB directly because server.main.list_ledgers() is still a
    stub returning {'status': 'not_implemented'}.  A minimal editor is #48.

    Query params
    ------------
    period : str
        One of ``this_month`` (default), ``last_month``, ``last_3_months``,
        ``this_year``, ``all``.
    """
    if not _is_authenticated(request):
        return _redirect("/login")
    # Reject unknown period values and fall back to default
    if period not in _VALID_PERIODS:
        period = "this_month"
    uid = _current_user_id(request)
    ledgers = _get_ledgers(uid, with_drilldown=True, period=period)
    # Auto-fallback: if this_month has no data, silently show last_month instead.
    if period == "this_month" and all(
        sum(li["total"] for li in ldr["line_items"]) == 0 for ldr in ledgers
    ):
        from datetime import date as _lm_date

        _lm_today = _lm_date.today()
        fallback_ledgers = _get_ledgers(uid, with_drilldown=True, period="last_month")
        if any(sum(li["total"] for li in ldr["line_items"]) != 0 for ldr in fallback_ledgers):
            ledgers = fallback_ledgers
            period = "last_month"
    return templates.TemplateResponse(
        request,
        "ledgers.html",
        {
            "current_page": "ledgers",
            "ledgers": ledgers,
            "period": period,
            "reauth_connections": _get_reauth_connections(uid),
        },
    )


# -- /ledgers CRUD (issue #48) -------------------------------------------------

import uuid as _uuid


@app.post("/ledgers")
async def ledgers_create(request: Request):
    """Create a new ledger. JSON body: {name: str}."""
    if not _is_authenticated(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    uid = _current_user_id(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    ledger_id = str(_uuid.uuid4())
    conn = get_db(_db_path())
    try:
        conn.execute(
            "INSERT INTO ledgers (id, name, user_id) VALUES (?, ?, ?)",
            (ledger_id, name, uid),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"id": ledger_id, "name": name}, status_code=201)


@app.delete("/ledgers/{ledger_id}")
def ledgers_delete(request: Request, ledger_id: str):
    """Delete a ledger. Returns 409 if it is the only ledger."""
    if not _is_authenticated(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    conn = get_db(_db_path())
    try:
        count = conn.execute("SELECT COUNT(*) FROM ledgers").fetchone()[0]
        if count <= 1:
            return JSONResponse({"error": "Cannot delete the only ledger"}, status_code=409)
        conn.execute("DELETE FROM line_items WHERE ledger_id = ?", (ledger_id,))
        conn.execute("DELETE FROM ledgers WHERE id = ?", (ledger_id,))
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"deleted": ledger_id})


@app.patch("/ledgers/{ledger_id}")
async def ledgers_rename(request: Request, ledger_id: str):
    """Rename a ledger. JSON body: {name: str}."""
    if not _is_authenticated(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    conn = get_db(_db_path())
    try:
        row = conn.execute("SELECT id FROM ledgers WHERE id = ?", (ledger_id,)).fetchone()
        if row is None:
            return JSONResponse({"error": "Ledger not found"}, status_code=404)
        conn.execute("UPDATE ledgers SET name = ? WHERE id = ?", (name, ledger_id))
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"id": ledger_id, "name": name})


@app.post("/ledgers/{ledger_id}/items")
async def ledger_items_create(request: Request, ledger_id: str):
    """Add a line item. JSON body: {name: str, section: 'income'|'expenses'|'savings'}."""
    if not _is_authenticated(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.json()
    name = (body.get("name") or "").strip()
    section = (body.get("section") or "expenses").strip().lower()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    # Map section name to item_type
    if section == "income":
        item_type = "income"
    elif section == "savings":
        item_type = "savings"
    else:
        item_type = "expense"
    conn = get_db(_db_path())
    try:
        row = conn.execute("SELECT id FROM ledgers WHERE id = ?", (ledger_id,)).fetchone()
        if row is None:
            return JSONResponse({"error": "Ledger not found"}, status_code=404)
        item_id = str(_uuid.uuid4())
        conn.execute(
            "INSERT INTO line_items (id, ledger_id, name, item_type) VALUES (?, ?, ?, ?)",
            (item_id, ledger_id, name, item_type),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse(
        {"id": item_id, "ledger_id": ledger_id, "name": name, "item_type": item_type},
        status_code=201,
    )


@app.patch("/ledgers/{ledger_id}/items/{item_id}")
async def ledger_items_rename(request: Request, ledger_id: str, item_id: str):
    """Rename a line item. JSON body: {name: str}."""
    if not _is_authenticated(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    conn = get_db(_db_path())
    try:
        row = conn.execute(
            "SELECT id FROM line_items WHERE id = ? AND ledger_id = ?",
            (item_id, ledger_id),
        ).fetchone()
        if row is None:
            return JSONResponse({"error": "Item not found"}, status_code=404)
        conn.execute("UPDATE line_items SET name = ? WHERE id = ?", (name, item_id))
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"id": item_id, "name": name})


@app.delete("/ledgers/{ledger_id}/items/{item_id}")
def ledger_items_delete(request: Request, ledger_id: str, item_id: str):
    """Delete a line item.  Returns 409 if transaction_entries are attached
    unless ?confirm=true is supplied."""
    if not _is_authenticated(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    confirm = request.query_params.get("confirm", "").lower() == "true"
    conn = get_db(_db_path())
    try:
        row = conn.execute(
            "SELECT id FROM line_items WHERE id = ? AND ledger_id = ?",
            (item_id, ledger_id),
        ).fetchone()
        if row is None:
            return JSONResponse({"error": "Item not found"}, status_code=404)
        # Check for attached entries
        entry_count = conn.execute(
            "SELECT COUNT(*) FROM transaction_entries WHERE line_item_id = ?",
            (item_id,),
        ).fetchone()[0]
        if entry_count > 0 and not confirm:
            return JSONResponse(
                {
                    "error": "Item has attached transaction entries",
                    "entry_count": entry_count,
                    "hint": "Add ?confirm=true to force delete",
                },
                status_code=409,
            )
        if entry_count > 0 and confirm:
            conn.execute("DELETE FROM transaction_entries WHERE line_item_id = ?", (item_id,))
        conn.execute("DELETE FROM line_items WHERE id = ?", (item_id,))
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"deleted": item_id})


# ── /link/start ──────────────────────────────────────────────────────────────


@app.get("/link/start", response_class=HTMLResponse)
def link_start(request: Request, back: Optional[str] = None):
    """Show the connector chooser: Plaid (hosted Link) or SimpleFIN (open banking).

    Previously this route went straight to Plaid; it now renders a chooser so
    users can pick their connector.  The Plaid path lives at /link/plaid/start.
    """
    if not _is_authenticated(request):
        return _redirect("/login")
    return templates.TemplateResponse(
        request,
        "link_choose.html",
        {"current_page": "accounts", "back_url": back or "/accounts"},
    )


@app.get("/link/plaid/start")
def link_plaid_start(request: Request, back: Optional[str] = None):
    """Generate a Plaid link token and redirect to /link?token=..."""
    if not _is_authenticated(request):
        return _redirect("/login")
    import server.main as _sm

    result = _sm.start_link()
    # start_link returns {"url": "http://127.0.0.1:6789/link?token=<token>"}
    url = result.get("url", "")
    if not url or "token=" not in url:
        return _redirect("/profile?error=link_token_failed")
    token = url.split("token=", 1)[1]
    back_param = f"&back={back}" if back else ""
    return _redirect(f"/link?token={token}{back_param}")


# ── /link/simplefin ──────────────────────────────────────────────────────────────────────────


@app.get("/link/simplefin/start", response_class=HTMLResponse)
def link_simplefin_start(request: Request, back: Optional[str] = None):
    """Render the SimpleFIN Setup Token paste form.

    Unlike Plaid (which redirects to a hosted Link flow), SimpleFIN asks the
    user to visit the SimpleFIN Bridge, copy their Setup Token, and paste it
    here.  No server-side action is needed before showing this page.
    """
    if not _is_authenticated(request):
        return _redirect("/login")
    return templates.TemplateResponse(
        request,
        "simplefin_link.html",
        {
            "current_page": "accounts",
            "complete_url": "/link/simplefin/complete",
            "back_url": back or "/accounts",
        },
    )


@app.post("/link/simplefin/complete")
async def link_simplefin_complete(request: Request):
    """Receive the SimpleFIN Setup Token, exchange it for an Access URL, and store.

    The user submits the paste form from /link/simplefin/start.  This handler
    calls ``complete_simplefin_link`` to exchange the token and create the
    ``bank_connections`` row, then redirects to /accounts.
    """
    if not _is_authenticated(request):
        return _redirect("/login")

    form = await request.form()
    setup_token = (form.get("setup_token") or "").strip()
    institution_name = (form.get("institution_name") or "").strip() or None

    if not setup_token:
        return _redirect("/accounts?error=missing_setup_token")

    try:
        import server.main as _sm_sf

        result = _sm_sf.complete_simplefin_link(
            setup_token=setup_token,
            institution_name=institution_name,
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).error("complete_simplefin_link failed: %s", exc)
        return templates.TemplateResponse(
            request,
            "simplefin_link.html",
            {
                "current_page": "accounts",
                "complete_url": "/link/simplefin/complete",
                "back_url": "/accounts",
                "error": f"Unexpected error: {exc}",
            },
            status_code=500,
        )

    if result.get("status") != "ok":
        # Re-render the form with the error so the user can retry.
        return templates.TemplateResponse(
            request,
            "simplefin_link.html",
            {
                "current_page": "accounts",
                "complete_url": "/link/simplefin/complete",
                "back_url": "/accounts",
                "error": result.get("message", "Could not connect SimpleFIN account."),
            },
            status_code=400,
        )

    return _redirect("/accounts?linked=1")


# ── /link ─────────────────────────────────────────────────────────────────────


@app.get("/link", response_class=HTMLResponse)
def link_get(request: Request, token: Optional[str] = None, back: Optional[str] = None):
    """Plaid Link JS embed.

    Accepts ?token=<link_token> from whoever generates the link token
    (setup wizard, profile page, or an MCP-issued URL).

    Loopback-only binding is enforced by daemon.py; this route just renders
    the embed.
    """
    if not _is_authenticated(request):
        return _redirect("/login")
    back_url = back or "/accounts"
    return templates.TemplateResponse(
        request,
        "link.html",
        {
            "link_token": token,
            "complete_url": "/link/complete",
            "back_url": back_url,
        },
    )


@app.post("/link/complete")
async def link_complete(request: Request):
    """Receive the Plaid public_token after a successful Link flow and exchange it.

    Called by the Plaid Link success callback via a hidden form POST.
    Stores the bank connection, triggers an initial sync to populate
    bank_accounts, then redirects to /accounts.
    """
    if not _is_authenticated(request):
        return _redirect("/login")
    form = await request.form()
    public_token = (form.get("public_token") or "").strip()
    if not public_token:
        return _redirect("/accounts?error=missing_token")
    try:
        import server.main as _sm_cl

        _sm_cl.complete_link(public_token=public_token)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).error("complete_link failed: %s", exc)
        return _redirect("/accounts?error=link_failed")
    # Trigger an initial sync in the background so bank_accounts gets populated.
    # Run in a thread so the redirect happens immediately (sync can be slow).
    import threading

    def _bg_sync():
        try:
            import server.main as _sm_sync

            _sm_sync.sync()
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).warning("post-link sync failed: %s", exc)

    threading.Thread(target=_bg_sync, daemon=True).start()
    return _redirect("/accounts?linked=1")
