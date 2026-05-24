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
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import server.paths as _paths
from server.db import get_db, init_db
from ui.auth import (
    SESSION_COOKIE,
    check_rate_limit,
    check_session,
    clear_failed_attempts,
    create_session,
    delete_session,
    get_password_hash,
    hash_password,
    prune_old_login_attempts,
    record_login_attempt,
    set_password_hash,
    verify_password,
)

# ── App setup ───────────────────────────────────────────────────────────────

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
    return bool(get_password_hash(_db_path()))


def _is_authenticated(request: Request) -> bool:
    return check_session(request, _db_path())


def _check_raw_session(db_path, token: str) -> bool:
    """Validate a raw session token without a Request object."""
    from ui.auth import _now, _SESSION_TTL
    conn = get_db(db_path)
    try:
        row = conn.execute(
            "SELECT expires_at FROM sessions WHERE id = ?", (token,)
        ).fetchone()
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
_PREF_TO_CHANNEL = {"openclaw": "openclaw_chat", "ui": "in_ui", "macos": "macos",
                    "openclaw_chat": "openclaw_chat", "in_ui": "in_ui"}


def _get_notification_channel() -> str:
    conn = get_db(_db_path())
    try:
        try:
            row = conn.execute("SELECT notification_channel FROM app_config WHERE id=1").fetchone()
            if row and row["notification_channel"]:
                return row["notification_channel"]
        except Exception: pass
        try:
            row = conn.execute("SELECT notification_pref FROM app_config WHERE id=1").fetchone()
            if row and row["notification_pref"]:
                return _PREF_TO_CHANNEL.get(row["notification_pref"], row["notification_pref"])
        except Exception: pass
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
        except Exception: pass
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

def _get_ledgers() -> list[dict]:
    """Query ledgers + line_items from the DB and return a list of dicts."""
    conn = get_db(_db_path())
    try:
        ledger_rows = conn.execute(
            "SELECT id, name FROM ledgers ORDER BY name"
        ).fetchall()
        ledgers = []
        for lr in ledger_rows:
            items = conn.execute(
                "SELECT name, item_type FROM line_items WHERE ledger_id = ? ORDER BY name",
                (lr["id"],),
            ).fetchall()
            ledgers.append({
                "name": lr["name"],
                "line_items": [{"name": i["name"], "item_type": i["item_type"]} for i in items],
            })
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
        samesite="strict",
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
    return _redirect("/profile")


# ── /setup ───────────────────────────────────────────────────────────────────


def _openclaw_home_exists() -> bool:
    return Path(os.path.expanduser("~/.openclaw")).is_dir()


@app.get("/setup", response_class=HTMLResponse)
def setup_get(request: Request):
    if _password_is_set():
        return HTMLResponse(status_code=404, content="Setup already complete.")
    tok = _get_wizard_token(request) or secrets.token_hex(16)
    dch = "openclaw_chat" if _openclaw_home_exists() else "macos"
    resp = templates.TemplateResponse(request, "setup.html",
        {"step": 1, "error": None, "default_channel": dch})
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
        pw = (form.get("password") or "").strip()
        cf = (form.get("password_confirm") or "").strip()
        dch = "openclaw_chat" if _openclaw_home_exists() else "macos"
        if len(pw) < 8:
            err = "Password must be at least 8 characters."
            resp = templates.TemplateResponse(request, "setup.html",
                {"step": 1, "error": err, "default_channel": dch})
            _update_wizard(resp, tok, {"step": 1, "wizard_active": False, "error": err})
            return resp
        if pw != cf:
            err = "Passwords do not match."
            resp = templates.TemplateResponse(request, "setup.html",
                {"step": 1, "error": err, "default_channel": dch})
            _update_wizard(resp, tok, {"step": 1, "wizard_active": False, "error": err})
            return resp
        set_password_hash(_db_path(), hash_password(pw))
        ua = request.headers.get("user-agent")
        stoken = create_session(_db_path(), user_agent=ua)
        ns = {"step": 2, "wizard_active": True, "session_token": stoken, "error": None}
        resp = templates.TemplateResponse(request, "setup.html",
            {"step": 2, "error": None, "default_channel": dch})
        _update_wizard(resp, tok, ns)
        # Set the real session cookie at step 1 so the user is logged in for the
        # rest of the wizard and lands authenticated on /profile at the end.
        # (Matches the spec in tests/test_setup_wizard.py which asserts
        # friday_bp_session is set after POST /setup/1.)
        resp.set_cookie(SESSION_COOKIE, stoken, httponly=True, samesite="strict")
        return resp
    elif step == 2:
        raw = form.get("notification_channel") or form.get("notification_pref") or "openclaw_chat"
        ch = _PREF_TO_CHANNEL.get(raw, raw)
        _set_notification_channel(ch)
        ns = {**state, "step": 3, "notification_channel": ch, "error": None}
        resp = templates.TemplateResponse(request, "setup.html", {"step": 3, "error": None})
        _update_wizard(resp, tok, ns)
        return resp
    elif step == 3:
        bl = (form.get("action") or "").strip() == "done"
        ch = state.get("notification_channel", _get_notification_channel())
        ns = {**state, "step": 4, "bank_linked": bl, "error": None}
        resp = templates.TemplateResponse(request, "setup.html",
            {"step": 4, "error": None, "notification_channel": ch, "bank_linked": bl})
        _update_wizard(resp, tok, ns)
        return resp
    elif step == 4:
        import server.main as _sm
        _sm.apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])
        redir = _redirect("/profile")
        st = state.get("session_token")
        if st:
            redir.set_cookie(_SETUP_COMPLETE_COOKIE, st, httponly=True, samesite="strict", max_age=300)
        _clear_wizard(redir, tok)
        return redir
    return HTMLResponse(status_code=404, content="Unknown step.")

def _ensure_ledger(name: str) -> None:
    """Create a ledger row if one with this name doesn't already exist."""
    import uuid as _uuid
    conn = get_db(_db_path())
    try:
        existing = conn.execute(
            "SELECT id FROM ledgers WHERE name = ?", (name,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO ledgers (id, name) VALUES (?, ?)",
                (str(_uuid.uuid4()), name),
            )
            conn.commit()
    finally:
        conn.close()


# ── /login ───────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    """Render login form.  If no password is set, redirect to /setup."""
    if not _password_is_set():
        return _redirect("/setup")
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None},
    )


@app.post("/login")
async def login_post(request: Request):
    """Verify password; on success create session and redirect to /profile.

    On failure: re-render login.html with an error.

    Rate limiting (#37): rejects with 429 after 5 failed attempts in 5 min.
    Opportunistically prunes login_attempts rows older than 30 days.
    """
    if not _password_is_set():
        return _redirect("/setup")

    # Opportunistic cleanup of old attempts (30-day horizon).
    prune_old_login_attempts(_db_path())

    # Enforce rate limit before touching the password.
    blocked, retry_after = check_rate_limit(_db_path())
    if blocked:
        return JSONResponse(
            status_code=429,
            content={"error": "too_many_attempts", "retry_after_seconds": retry_after},
        )

    form = await request.form()
    password = (form.get("password") or "")

    stored_hash = get_password_hash(_db_path())
    success = stored_hash is not None and verify_password(password, stored_hash)

    # Record this attempt.
    record_login_attempt(_db_path(), success)

    if not success:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect password."},
            status_code=200,
        )

    # Successful login — clear the recent failure counter.
    clear_failed_attempts(_db_path())
    # Clear any pending setup session.
    st = request.cookies.get(_SETUP_COMPLETE_COOKIE)
    if st:
        delete_session(_db_path(), st)

    # Create session.
    ua = request.headers.get("user-agent")
    token = create_session(_db_path(), user_agent=ua)

    response = _redirect("/profile")
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict")
    response.delete_cookie(_SETUP_COMPLETE_COOKIE)
    return response


# ── /logout ──────────────────────────────────────────────────────────────────

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
    """Placeholder recovery page (full flow in #60)."""
    return templates.TemplateResponse(
        request,
        "forgot.html",
        {"sent": False},
    )


@app.post("/forgot", response_class=HTMLResponse)
def forgot_post(request: Request):
    """Write a recovery token file placeholder (#60).

    Writes ~/.friday-bp/recovery.txt with a random token.  The full recovery
    flow (token validation, new password entry) lands with issue #60.
    """
    token = secrets.token_hex(32)
    recovery_path = _paths.APP_DIR / "recovery.txt"
    try:
        _paths.APP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        recovery_path.write_text(
            f"Friday Budgeting Pro — password recovery token\n"
            f"Token: {token}\n"
            f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Visit http://127.0.0.1:6789/reset and enter this token.\n"
            f"(Full recovery flow ships with issue #60.)\n"
        )
        os.chmod(recovery_path, 0o600)
    except Exception:
        pass  # Non-fatal in this PR; #60 adds proper error handling.

    return templates.TemplateResponse(
        request,
        "forgot.html",
        {"sent": True},
    )


# ── /reset ───────────────────────────────────────────────────────────────────

@app.get("/reset", response_class=HTMLResponse)
def reset_get(request: Request):
    """Placeholder reset page (#60)."""
    return templates.TemplateResponse(
        request,
        "reset.html",
        {"error": None},
    )


@app.post("/reset", response_class=HTMLResponse)
async def reset_post(request: Request):
    """Placeholder reset handler (#60).

    TODO (#60): Validate token from recovery.txt, update password hash,
    delete recovery.txt, redirect to /login.
    """
    return templates.TemplateResponse(
        request,
        "reset.html",
        {"error": "Password reset is not yet implemented. See issue #60."},
    )


# ── /profile ─────────────────────────────────────────────────────────────────

@app.get("/profile", response_class=HTMLResponse)
def profile_get(request: Request):
    """Settings page.  Requires authentication."""
    if _is_authenticated(request):
        pref = _get_notification_pref()
        return templates.TemplateResponse(request, "profile.html",
            {"notification_pref": pref, "saved": False})
    st = request.cookies.get(_SETUP_COMPLETE_COOKIE)
    if st and _check_raw_session(_db_path(), st):
        pref = _get_notification_pref()
        resp = templates.TemplateResponse(request, "profile.html",
            {"notification_pref": pref, "saved": False})
        resp.set_cookie(SESSION_COOKIE, st, httponly=True, samesite="strict")
        resp.delete_cookie(_SETUP_COMPLETE_COOKIE)
        return resp
    resp2 = _redirect("/login")
    if request.cookies.get(_SETUP_COMPLETE_COOKIE):
        resp2.delete_cookie(_SETUP_COMPLETE_COOKIE)
    return resp2


@app.post("/profile", response_class=HTMLResponse)
async def profile_post(request: Request):
    """Save settings.  Requires authentication.

    Persists what's already in app_config (notification_pref).
    New columns (linked-account settings, etc.) wait for #47.
    """
    if not _is_authenticated(request):
        return _redirect("/login")
    form = await request.form()
    pref = form.get("notification_pref") or "openclaw"
    _set_notification_pref(pref)
    return templates.TemplateResponse(
        request,
        "profile.html",
        {"notification_pref": pref, "saved": True},
    )


# ── /ledgers ─────────────────────────────────────────────────────────────────

@app.get("/ledgers", response_class=HTMLResponse)
def ledgers_get(request: Request):
    """Read-only ledger tree.  Requires authentication.

    Queries the DB directly because server.main.list_ledgers() is still a
    stub returning {'status': 'not_implemented'}.  A minimal editor is #48.
    """
    if not _is_authenticated(request):
        return _redirect("/login")
    ledgers = _get_ledgers()
    return templates.TemplateResponse(
        request,
        "ledgers.html",
        {"ledgers": ledgers},
    )


# ── /link ─────────────────────────────────────────────────────────────────────

@app.get("/link", response_class=HTMLResponse)
def link_get(request: Request, token: Optional[str] = None):
    """Plaid Link JS embed.

    Accepts ?token=<link_token> from whoever generates the link token
    (setup wizard, profile page, or an MCP-issued URL).

    Loopback-only binding is enforced by daemon.py; this route just renders
    the embed.
    """
    if not _is_authenticated(request):
        return _redirect("/login")
    return templates.TemplateResponse(
        request,
        "link.html",
        {"link_token": token},
    )
