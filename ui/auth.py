"""
ui/auth.py — Auth helpers for Friday Budgeting Pro UI.

PLACEHOLDER IMPLEMENTATION — Issue #37 will replace this with:
  - argon2id password hashing (instead of PBKDF2-HMAC-SHA256 used here)
  - 7-day idle session expiry
  - Login rate limiting (login_attempts table)

Current scope (issue #14):
  - PBKDF2-HMAC-SHA256 for password hashing (stdlib only, intentionally)
  - Server-side session cookies ("friday_bp_session")
  - check_session(request) — simple lookup, no expiry in this PR
  - hash_password / verify_password — clearly marked for #37 replacement
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from typing import Optional

from fastapi import Request

from server.db import get_db

# ── Session cookie name ────────────────────────────────────────────────────
SESSION_COOKIE = "friday_bp_session"

# ── PBKDF2 parameters ─────────────────────────────────────────────────────
# TODO (#37): Replace with argon2id via argon2-cffi.  PBKDF2 is used here
# only to avoid adding a non-stdlib dependency before #37 lands.
_PBKDF2_HASH = "sha256"
_PBKDF2_ITERATIONS = 260_000  # OWASP 2023 minimum for SHA-256


# ── Password helpers ──────────────────────────────────────────────────────

def hash_password(plaintext: str) -> str:
    """Hash *plaintext* with PBKDF2-HMAC-SHA256 and a random salt.

    Returns a ``"pbkdf2$<hex-salt>$<hex-digest>"`` string.

    NOTE: This is a PLACEHOLDER.  Issue #37 will replace this function body
    with argon2id (argon2-cffi).  The stored format will change at that point
    and existing hashes will be invalidated — acceptable for a fresh install.
    """
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac(
        _PBKDF2_HASH,
        plaintext.encode(),
        salt,
        _PBKDF2_ITERATIONS,
    )
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(plaintext: str, stored_hash: str) -> bool:
    """Return True if *plaintext* matches *stored_hash*.

    Accepts hashes produced by hash_password().

    NOTE: This is a PLACEHOLDER — see hash_password() docstring.
    """
    try:
        scheme, salt_hex, dk_hex = stored_hash.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2":
        return False
    salt = bytes.fromhex(salt_hex)
    expected_dk = bytes.fromhex(dk_hex)
    dk = hashlib.pbkdf2_hmac(
        _PBKDF2_HASH,
        plaintext.encode(),
        salt,
        _PBKDF2_ITERATIONS,
    )
    # Constant-time comparison to prevent timing attacks.
    return secrets.compare_digest(dk, expected_dk)


# ── Session helpers ───────────────────────────────────────────────────────

def create_session(db_path, user_agent: Optional[str] = None) -> str:
    """Insert a new session row and return the session token.

    TODO (#37): Add 7-day idle expiry enforcement here.
    """
    token = secrets.token_hex(32)
    now = int(time.time())
    # expires_at: 7 days from now — #37 will enforce this; for now we store
    # it correctly so the schema constraint is satisfied.
    expires_at = now + 7 * 24 * 3600
    conn = get_db(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (id, created_at, last_seen_at, expires_at, user_agent) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, now, now, expires_at, user_agent),
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

    Reads the ``friday_bp_session`` cookie, looks it up in the sessions table.
    Returns False if the cookie is absent, the session doesn't exist, or DB
    lookup fails for any reason.

    TODO (#37): Enforce 7-day idle expiry (expires_at / last_seen_at check).
    TODO (#37): Refresh last_seen_at on valid requests.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    conn = get_db(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM sessions WHERE id = ?", (token,)
        ).fetchone()
        return row is not None
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
    now = int(time.time())
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
