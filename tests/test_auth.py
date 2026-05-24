"""
tests/test_auth.py — Dedicated tests for ui.auth (issue #37).

Covers:
  - argon2id hash/verify round trip
  - Wrong password → verify_password returns False, no exception
  - Rate limiting: 5 failures → 6th attempt returns 429
  - Rate limit resets after 5-minute window (monkeypatched _now)
  - Successful login clears failed attempt counter
  - Session lifetime: expired session rejected (monkeypatched _now)
  - Restart-survival: session persisted in SQLite, no in-memory state needed
  - prune_old_login_attempts removes rows older than 30 days
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch) -> Path:
    """Initialise a fresh SQLite DB and monkeypatch server.paths.DB_PATH."""
    from server.db import init_db
    import server.paths as paths

    db = tmp_path / "test_auth.db"
    init_db(db)

    monkeypatch.setattr(paths, "DB_PATH", db)
    monkeypatch.setattr(paths, "APP_DIR", tmp_path)
    return db


@pytest.fixture()
def client(db_path: Path) -> TestClient:
    from ui.server import app
    return TestClient(app, follow_redirects=False)


@pytest.fixture()
def setup_client(client: TestClient) -> TestClient:
    """Drive through setup wizard so a password is set."""
    _complete_setup(client)
    return client


def _complete_setup(client: TestClient, password: str = "correcthorsebattery") -> None:
    r = client.post("/setup/1", data={"password": password, "password_confirm": password})
    assert r.status_code == 200
    r = client.post("/setup/2", data={"notification_pref": "openclaw"})
    assert r.status_code == 200
    r = client.post("/setup/3", data={"ledger_name": "Personal"})
    assert r.status_code == 200
    r = client.post("/setup/4", data={})
    assert r.status_code == 302


# ---------------------------------------------------------------------------
# Unit tests — password hashing
# ---------------------------------------------------------------------------

class TestPasswordHashing:
    def test_hash_verify_roundtrip(self):
        from ui.auth import hash_password, verify_password
        h = hash_password("mysecretpassword")
        assert verify_password("mysecretpassword", h) is True

    def test_wrong_password_returns_false(self):
        from ui.auth import hash_password, verify_password
        h = hash_password("correct")
        result = verify_password("wrong", h)
        assert result is False

    def test_wrong_password_no_exception(self):
        from ui.auth import hash_password, verify_password
        h = hash_password("correct")
        # Must not raise — just return False.
        try:
            result = verify_password("definitely wrong", h)
            assert result is False
        except Exception as exc:
            pytest.fail(f"verify_password raised unexpectedly: {exc}")

    def test_empty_password_returns_false(self):
        from ui.auth import hash_password, verify_password
        h = hash_password("nonempty")
        assert verify_password("", h) is False

    def test_hash_uses_argon2id_format(self):
        from ui.auth import hash_password
        h = hash_password("test")
        # argon2-cffi produces $argon2id$ prefix
        assert h.startswith("$argon2id$"), f"Unexpected hash prefix: {h[:20]}"

    def test_two_hashes_differ(self):
        """argon2 uses a random salt — two hashes of the same password differ."""
        from ui.auth import hash_password
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2


# ---------------------------------------------------------------------------
# Unit tests — rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    """Tests for check_rate_limit / record_login_attempt / clear_failed_attempts."""

    def test_five_failures_block_sixth(self, setup_client, db_path):
        """5 bad POST /login → 6th returns 429."""
        for _ in range(5):
            r = setup_client.post("/login", data={"password": "wrong"})
            assert r.status_code == 200  # still getting login form back

        r = setup_client.post("/login", data={"password": "wrong"})
        assert r.status_code == 429
        body = r.json()
        assert body["error"] == "too_many_attempts"
        assert isinstance(body["retry_after_seconds"], int)
        assert body["retry_after_seconds"] > 0

    def test_rate_limit_respects_window(self, setup_client, db_path, monkeypatch):
        """After the 5-min window elapses, login is allowed again."""
        import ui.auth as auth_module

        base_time = 1_700_000_000

        # Simulate 5 failures at base_time.
        monkeypatch.setattr(auth_module, "_now", lambda: base_time)
        for _ in range(5):
            r = setup_client.post("/login", data={"password": "wrong"})
            assert r.status_code == 200

        # 6th attempt at base_time → blocked.
        r = setup_client.post("/login", data={"password": "wrong"})
        assert r.status_code == 429

        # Advance time by 6 minutes (past the 5-min window).
        monkeypatch.setattr(auth_module, "_now", lambda: base_time + 6 * 60)

        # Now login should be allowed again (correct password).
        r = setup_client.post("/login", data={"password": "correcthorsebattery"})
        assert r.status_code == 302, f"Expected 302, got {r.status_code}: {r.text}"

    def test_successful_login_clears_failed_counter(self, setup_client, db_path):
        """4 failures + 1 success → counter resets → next attempt is not blocked."""
        for _ in range(4):
            r = setup_client.post("/login", data={"password": "wrong"})
            assert r.status_code == 200

        # Successful login — clears the failed counter.
        r = setup_client.post("/login", data={"password": "correcthorsebattery"})
        assert r.status_code == 302

        # 5 more failures should be fine (counter was cleared).
        for _ in range(5):
            r = setup_client.post("/login", data={"password": "wrong"})
            assert r.status_code == 200  # not yet blocked

    def test_429_retry_after_is_sensible(self, setup_client, db_path):
        """retry_after_seconds should be <= 300 (the window size)."""
        for _ in range(5):
            setup_client.post("/login", data={"password": "wrong"})

        r = setup_client.post("/login", data={"password": "wrong"})
        assert r.status_code == 429
        assert r.json()["retry_after_seconds"] <= 300


# ---------------------------------------------------------------------------
# Unit tests — session lifetime
# ---------------------------------------------------------------------------

class TestSessionLifetime:
    def test_expired_session_rejected(self, setup_client, db_path, monkeypatch):
        """Session 8 days old (no activity) must be rejected."""
        import ui.auth as auth_module

        base_time = 1_700_000_000
        monkeypatch.setattr(auth_module, "_now", lambda: base_time)

        # Log in at base_time — creates session with expires_at = base + 7d.
        r = setup_client.post("/login", data={"password": "correcthorsebattery"})
        assert r.status_code == 302

        # Jump 8 days into the future — session should be expired.
        monkeypatch.setattr(auth_module, "_now", lambda: base_time + 8 * 24 * 3600)

        # Profile should redirect to /login (session rejected).
        r = setup_client.get("/profile")
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_active_session_refreshes(self, setup_client, db_path, monkeypatch):
        """Session touched at day 6 should still be valid at day 12."""
        import ui.auth as auth_module

        base_time = 1_700_000_000
        monkeypatch.setattr(auth_module, "_now", lambda: base_time)

        r = setup_client.post("/login", data={"password": "correcthorsebattery"})
        assert r.status_code == 302

        # At day 6, access profile → session refreshed (expires_at = day 6 + 7d = day 13).
        monkeypatch.setattr(auth_module, "_now", lambda: base_time + 6 * 24 * 3600)
        r = setup_client.get("/profile")
        assert r.status_code == 200

        # At day 12 (< day 13 new expiry), session should still be valid.
        monkeypatch.setattr(auth_module, "_now", lambda: base_time + 12 * 24 * 3600)
        r = setup_client.get("/profile")
        assert r.status_code == 200

    def test_session_restart_survival(self, db_path):
        """Sessions are persisted in SQLite — reloading the DB makes them survive."""
        import ui.auth as auth_module

        # Create a session with a direct call (not via HTTP).
        token = auth_module.create_session(db_path, user_agent="test-agent")
        assert len(token) == 64  # 32 bytes → 64 hex chars

        # Simulate "restart" by opening a fresh connection and querying.
        from server.db import get_db
        conn = get_db(db_path)
        try:
            row = conn.execute(
                "SELECT id, expires_at FROM sessions WHERE id = ?", (token,)
            ).fetchone()
            assert row is not None, "Session not found after DB close/reopen"
            assert row["id"] == token
            assert row["expires_at"] > auth_module._now()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Unit tests — prune_old_login_attempts
# ---------------------------------------------------------------------------

class TestPruneOldLoginAttempts:
    def test_prune_removes_old_rows(self, db_path):
        """Rows older than 30 days are deleted; recent rows are kept."""
        from server.db import get_db
        import ui.auth as auth_module

        now = auth_module._now()
        old_ts = now - 31 * 24 * 3600   # 31 days ago → should be pruned
        recent_ts = now - 1 * 24 * 3600  # 1 day ago → should survive

        conn = get_db(db_path)
        try:
            conn.execute(
                "INSERT INTO login_attempts (attempted_at, success) VALUES (?, 0)",
                (old_ts,),
            )
            conn.execute(
                "INSERT INTO login_attempts (attempted_at, success) VALUES (?, 0)",
                (recent_ts,),
            )
            conn.commit()
        finally:
            conn.close()

        auth_module.prune_old_login_attempts(db_path)

        conn = get_db(db_path)
        try:
            rows = conn.execute("SELECT attempted_at FROM login_attempts").fetchall()
            timestamps = [r["attempted_at"] for r in rows]
        finally:
            conn.close()

        assert old_ts not in timestamps, "Old row should have been pruned"
        assert recent_ts in timestamps, "Recent row should be kept"

    def test_prune_empty_table_is_safe(self, db_path):
        """prune_old_login_attempts on an empty table must not raise."""
        from ui.auth import prune_old_login_attempts
        prune_old_login_attempts(db_path)  # should not raise
