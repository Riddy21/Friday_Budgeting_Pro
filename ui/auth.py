"""
ui/auth.py — Auth helpers for Friday Budgeting Pro UI.

Implements:
  - argon2id password hashing via argon2-cffi
  - Permanent sessions (persist until explicit logout — no idle expiry)
  - No login rate limiting (single-user local app per d4403c0)
"""

from __future__ import annotations

import secrets
import time
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from fastapi import Request

from server.db import get_db

# ── Session cookie name ────────────────────────────────────────────────────
SESSION_COOKIE = "friday_bp_session"

# ── argon2id hasher (sensible defaults from argon2-cffi) ──────────────────
_ph = PasswordHasher()


# ── Time helper (used by set_password_hash / create_session for timestamps) ──

def _now() -> int:
    """Return the current Unix timestamp as an integer."""
    return int(time.time())


# ── Password helpers ──────────────────────────────────────────────────────

def hash_password(plaintext: str) -> str:
    """Hash *plaintext* with argon2id and return the encoded hash string."""
    return _ph.hash(plaintext)


def verify_password(plaintext: str, stored_hash: str) -> bool:
    """Return True if *plaintext* matches *stored_hash*."""
    try:
        return _ph.verify(stored_hash, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ── Session helpers ───────────────────────────────────────────────────────

def create_session(db_path, user_agent: Optional[str] = None) -> str:
    """Insert a new session row and return the session token.

    Sessions are permanent — they persist until explicitly deleted via
    delete_session (logout). No expiry is enforced.
    """
    token = secrets.token_hex(32)
    now = _now()
    conn = get_db(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (id, created_at, last_seen_at, expires_at, user_agent) "
            "VALUES (?, ?, ?, ?, ?)",
            # expires_at column kept for backward compat with existing DBs; not used for auth.
            (token, now, now, 0, user_agent),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def delete_session(db_path, token: str) -> None:
    """Remove a session row (logout)."""
    conn = get_db(db_path)
    try:
        conn.execute("DELETE FROM sessions WHERE id = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def check_session(request: Request, db_path) -> bool:
    """Return True if the request carries a valid session cookie.

    Sessions are permanent — no expiry check. Updates last_seen_at as a
    record-keeping touch only.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    conn = get_db(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM sessions WHERE id = ?", (token,)
        ).fetchone()
        if row is None:
            return False
        now = _now()
        conn.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
            (now, token),
        )
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


# ── App-config helpers ────────────────────────────────────────────────────

def get_password_hash(db_path) -> Optional[str]:
    """Return the stored UI password hash, or None if not yet set."""
    conn = get_db(db_path)
    try:
        row = conn.execute(
            "SELECT ui_password_hash FROM app_config WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return row["ui_password_hash"]
    finally:
        conn.close()


def set_password_hash(db_path, hashed: str) -> None:
    """Upsert the UI password hash into app_config (single-row, id=1)."""
    now = _now()
    conn = get_db(db_path)
    try:
        conn.execute(
            "INSERT INTO app_config (id, ui_password_hash, ui_password_set_at) "
            "VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET ui_password_hash=excluded.ui_password_hash, "
            "ui_password_set_at=excluded.ui_password_set_at",
            (hashed, now),
        )
        conn.commit()
    finally:
        conn.close()
