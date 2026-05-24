"""
ui/auth.py — Auth helpers for Friday Budgeting Pro UI.

Implements issue #37:
  - argon2id password hashing via argon2-cffi
  - 7-day idle session expiry (sliding window)
  - Login rate limiting (5 failed attempts per 5 minutes → 429)
  - Opportunistic 30-day cleanup of login_attempts
"""

from __future__ import annotations

import secrets
import time
from typing import Optional, Tuple

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from fastapi import Request

from server.db import get_db

# ── Session cookie name ────────────────────────────────────────────────────
SESSION_COOKIE = "friday_bp_session"

# ── Session expiry ─────────────────────────────────────────────────────────
_SESSION_TTL = 7 * 24 * 3600  # 7 days in seconds

# ── Rate limit parameters ──────────────────────────────────────────────────
_RATE_LIMIT_WINDOW = 5 * 60       # 5-minute window
_RATE_LIMIT_MAX_FAILURES = 5      # max failed attempts before lockout
_CLEANUP_HORIZON = 30 * 24 * 3600  # prune rows older than 30 days

# ── argon2id hasher (sensible defaults from argon2-cffi) ──────────────────
_ph = PasswordHasher()


# ── Time helper (monkeypatchable for tests) ───────────────────────────────

def _now() -> int:
    """Return the current Unix timestamp as an integer.

    Extracted as a module-level function so tests can monkeypatch it.
    """
    return int(time.time())


# ── Password helpers ──────────────────────────────────────────────────────

def hash_password(plaintext: str) -> str:
    """Hash *plaintext* with argon2id and return the encoded hash string.

    Uses argon2-cffi's PasswordHasher with default parameters (argon2id,
    time_cost=3, memory_cost=65536, parallelism=4 as of argon2-cffi 21+).
    """
    return _ph.hash(plaintext)


def verify_password(plaintext: str, stored_hash: str) -> bool:
    """Return True if *plaintext* matches *stored_hash*.

    Returns False on any mismatch without raising exceptions in normal flow.
    Works with argon2id hashes produced by hash_password().
    """
    try:
        return _ph.verify(stored_hash, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ── Session helpers ───────────────────────────────────────────────────────

def create_session(db_path, user_agent: Optional[str] = None) -> str:
    """Insert a new session row and return the session token.

    Session has a 7-day sliding idle expiry (expires_at is refreshed on
    every authenticated request via check_session).
    """
    token = secrets.token_hex(32)
    now = _now()
    expires_at = now + _SESSION_TTL
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
    """Return True if the request carries a valid, non-expired session cookie.

    On a valid session:
      - Checks that now <= expires_at (rejects expired sessions).
      - Updates last_seen_at = now and extends expires_at by another 7 days
        (sliding idle expiry window).

    Returns False if the cookie is absent, the session is expired, the row
    doesn't exist, or the DB lookup fails for any reason.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    conn = get_db(db_path)
    try:
        row = conn.execute(
            "SELECT expires_at FROM sessions WHERE id = ?", (token,)
        ).fetchone()
        if row is None:
            return False
        now = _now()
        if now > row["expires_at"]:
            # Session has expired — delete it to keep the table tidy.
            conn.execute("DELETE FROM sessions WHERE id = ?", (token,))
            conn.commit()
            return False
        # Refresh sliding window.
        new_expires = now + _SESSION_TTL
        conn.execute(
            "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
            (now, new_expires, token),
        )
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


# ── Rate limiting helpers ─────────────────────────────────────────────────

def check_rate_limit(db_path) -> Tuple[bool, int]:
    """Check whether the login endpoint should be rate-limited.

    Counts failed login_attempts rows within the last 5 minutes.
    Returns (blocked, retry_after_seconds):
      - blocked=True  → caller should return 429
      - retry_after_seconds = seconds until the oldest failure leaves the window
    """
    now = _now()
    window_start = now - _RATE_LIMIT_WINDOW
    conn = get_db(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM login_attempts "
            "WHERE attempted_at >= ? AND success = 0",
            (window_start,),
        ).fetchone()
        count = row["cnt"]
        if count >= _RATE_LIMIT_MAX_FAILURES:
            oldest_row = conn.execute(
                "SELECT MIN(attempted_at) as oldest FROM login_attempts "
                "WHERE attempted_at >= ? AND success = 0",
                (window_start,),
            ).fetchone()
            oldest = oldest_row["oldest"] or now
            retry_after = max(int(oldest + _RATE_LIMIT_WINDOW - now), 1)
            return True, retry_after
        return False, 0
    finally:
        conn.close()


def record_login_attempt(db_path, success: bool) -> None:
    """Insert a row into login_attempts for the current attempt."""
    conn = get_db(db_path)
    try:
        conn.execute(
            "INSERT INTO login_attempts (attempted_at, success) VALUES (?, ?)",
            (_now(), 1 if success else 0),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def clear_failed_attempts(db_path) -> None:
    """Delete recent failed login attempts (called on successful login).

    Clears all failed rows within the current rate-limit window so the
    counter resets after a correct password is entered.
    """
    now = _now()
    window_start = now - _RATE_LIMIT_WINDOW
    conn = get_db(db_path)
    try:
        conn.execute(
            "DELETE FROM login_attempts WHERE attempted_at >= ? AND success = 0",
            (window_start,),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def prune_old_login_attempts(db_path) -> None:
    """Delete login_attempts rows older than 30 days.

    Called opportunistically on every login to keep the table from growing
    unboundedly without requiring a scheduled job.
    """
    cutoff = _now() - _CLEANUP_HORIZON
    conn = get_db(db_path)
    try:
        conn.execute(
            "DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,)
        )
        conn.commit()
    except Exception:
        pass
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
