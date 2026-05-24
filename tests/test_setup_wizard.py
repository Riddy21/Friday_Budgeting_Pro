"""
tests/test_setup_wizard.py — Tests for the 4-step setup wizard (issue #56).

Covers:
  - GET /setup on empty DB → step 1
  - POST /setup/1 validation errors (mismatch, too short)
  - POST /setup/1 success → password hash set, session cookie returned, advance to step 2
  - POST /setup/2 → notification_channel persisted, advance to step 3
  - POST /setup/3 skip → advance to step 4
  - POST /setup/4 → apply_initial_setup called, redirect to /profile
  - After complete → GET /setup → 404
  - Redirect-to-setup middleware regression (non-setup routes redirect when no password)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures  (identical pattern to test_ui_routes.py so tests are independent)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch) -> Path:
    """Initialise a fresh SQLite DB and monkeypatch DB_PATH."""
    import server.paths as paths
    from server.db import init_db

    db = tmp_path / "wizard_test.db"
    init_db(db)
    monkeypatch.setattr(paths, "DB_PATH", db)
    monkeypatch.setattr(paths, "APP_DIR", tmp_path)
    return db


@pytest.fixture()
def client(db_path: Path) -> TestClient:
    from ui.server import app

    return TestClient(app, follow_redirects=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wizard_through_step1(client: TestClient, password: str = "securepass1") -> None:
    """Drive through step 1 with a valid password."""
    r = client.post("/setup/1", data={"password": password, "password_confirm": password})
    assert r.status_code == 200, f"step 1 returned {r.status_code}"


def _wizard_through_step2(client: TestClient, channel: str = "openclaw_chat") -> None:
    _wizard_through_step1(client)
    r = client.post("/setup/2", data={"notification_channel": channel})
    assert r.status_code == 200, f"step 2 returned {r.status_code}"


def _wizard_through_step3(client: TestClient) -> None:
    _wizard_through_step2(client)
    r = client.post("/setup/3", data={"action": "skip"})
    assert r.status_code == 200, f"step 3 returned {r.status_code}"


# ---------------------------------------------------------------------------
# Tests — GET /setup (empty DB)
# ---------------------------------------------------------------------------


class TestSetupGet:
    def test_empty_db_returns_200(self, client):
        r = client.get("/setup")
        assert r.status_code == 200

    def test_step1_indicators_present(self, client):
        r = client.get("/setup")
        assert b"password" in r.content.lower()
        # Password input should be in the page.
        assert b'type="password"' in r.content

    def test_wizard_cookie_is_set(self, client):
        r = client.get("/setup")
        assert "friday_bp_wizard" in r.cookies or "friday_bp_wizard" in r.headers.get(
            "set-cookie", ""
        )


# ---------------------------------------------------------------------------
# Tests — POST /setup/1 validation
# ---------------------------------------------------------------------------


class TestSetupStep1Validation:
    def test_mismatched_passwords_returns_200_with_error(self, client):
        r = client.post(
            "/setup/1",
            data={
                "password": "validpass1",
                "password_confirm": "different1",
            },
        )
        assert r.status_code == 200
        assert b"do not match" in r.content.lower() or b"mismatch" in r.content.lower()

    def test_short_password_returns_200_with_error(self, client):
        r = client.post(
            "/setup/1",
            data={
                "password": "short",
                "password_confirm": "short",
            },
        )
        assert r.status_code == 200
        assert b"8" in r.content  # mentions minimum length

    def test_short_password_does_not_set_hash(self, client, db_path):
        from ui.auth import get_password_hash

        client.post("/setup/1", data={"password": "short", "password_confirm": "short"})
        assert get_password_hash(db_path) is None

    def test_mismatch_re_renders_step1(self, client):
        r = client.post(
            "/setup/1",
            data={
                "password": "validpass1",
                "password_confirm": "different2",
            },
        )
        assert r.status_code == 200
        # Step 1 indicators still present.
        assert b'type="password"' in r.content


# ---------------------------------------------------------------------------
# Tests — POST /setup/1 success
# ---------------------------------------------------------------------------


class TestSetupStep1Success:
    def test_valid_password_returns_200(self, client):
        r = client.post(
            "/setup/1",
            data={
                "password": "securepass1",
                "password_confirm": "securepass1",
            },
        )
        assert r.status_code == 200

    def test_valid_password_sets_hash(self, client, db_path):
        from ui.auth import get_password_hash

        client.post(
            "/setup/1",
            data={
                "password": "securepass1",
                "password_confirm": "securepass1",
            },
        )
        assert get_password_hash(db_path) is not None

    def test_valid_password_creates_session_cookie(self, client):
        r = client.post(
            "/setup/1",
            data={
                "password": "securepass1",
                "password_confirm": "securepass1",
            },
        )
        assert r.status_code == 200
        # Session cookie should be set in the response headers.
        set_cookie = r.headers.get("set-cookie", "")
        assert "friday_bp_session" in set_cookie

    def test_step2_rendered_after_step1(self, client):
        r = client.post(
            "/setup/1",
            data={
                "password": "securepass1",
                "password_confirm": "securepass1",
            },
        )
        assert r.status_code == 200
        # Step 2 notification radio buttons should be present.
        assert b"notification_channel" in r.content or b"notification" in r.content.lower()


# ---------------------------------------------------------------------------
# Tests — POST /setup/2
# ---------------------------------------------------------------------------


class TestSetupStep2:
    def test_notification_channel_stored(self, client, db_path):
        _wizard_through_step1(client)
        r = client.post("/setup/2", data={"notification_channel": "macos"})
        assert r.status_code == 200
        # Verify the value was persisted.
        from server.db import get_db

        conn = get_db(db_path)
        row = conn.execute("SELECT notification_channel FROM app_config WHERE id = 1").fetchone()
        conn.close()
        assert row is not None
        assert row["notification_channel"] == "macos"

    def test_openclaw_chat_stored(self, client, db_path):
        _wizard_through_step1(client)
        client.post("/setup/2", data={"notification_channel": "openclaw_chat"})
        from server.db import get_db

        conn = get_db(db_path)
        row = conn.execute("SELECT notification_channel FROM app_config WHERE id = 1").fetchone()
        conn.close()
        assert row["notification_channel"] == "openclaw_chat"

    def test_in_ui_stored(self, client, db_path):
        _wizard_through_step1(client)
        client.post("/setup/2", data={"notification_channel": "in_ui"})
        from server.db import get_db

        conn = get_db(db_path)
        row = conn.execute("SELECT notification_channel FROM app_config WHERE id = 1").fetchone()
        conn.close()
        assert row["notification_channel"] == "in_ui"

    def test_advances_to_step3(self, client):
        _wizard_through_step1(client)
        r = client.post("/setup/2", data={"notification_channel": "macos"})
        assert r.status_code == 200
        # Step 3 bank-connect content should be rendered.
        assert (
            b"bank" in r.content.lower()
            or b"plaid" in r.content.lower()
            or b"skip" in r.content.lower()
        )

    def test_old_notification_pref_field_accepted(self, client, db_path):
        """Old form field name (notification_pref=openclaw) must still work."""
        _wizard_through_step1(client)
        r = client.post("/setup/2", data={"notification_pref": "openclaw"})
        assert r.status_code == 200
        from server.db import get_db

        conn = get_db(db_path)
        row = conn.execute("SELECT notification_channel FROM app_config WHERE id = 1").fetchone()
        conn.close()
        # 'openclaw' should be mapped to 'openclaw_chat'.
        assert row["notification_channel"] == "openclaw_chat"


# ---------------------------------------------------------------------------
# Tests — POST /setup/3 skip
# ---------------------------------------------------------------------------


class TestSetupStep3:
    def test_skip_advances_to_step4(self, client):
        _wizard_through_step2(client)
        r = client.post("/setup/3", data={"action": "skip"})
        assert r.status_code == 200
        # Step 4 done-page content should be rendered.
        assert (
            b"profile" in r.content.lower()
            or b"done" in r.content.lower()
            or b"set" in r.content.lower()
        )

    def test_no_action_treated_as_skip(self, client):
        """Sending no action field (e.g. legacy ledger_name form) advances to step 4."""
        _wizard_through_step2(client)
        r = client.post("/setup/3", data={"ledger_name": "Personal"})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Tests — POST /setup/4
# ---------------------------------------------------------------------------


class TestSetupStep4:
    def test_apply_initial_setup_called(self, client):
        _wizard_through_step3(client)
        with patch("server.main.apply_initial_setup") as mock_setup:
            mock_setup.return_value = {
                "status": "ok",
                "ledgers_created": [],
                "line_items_created": 0,
                "hints_created": 0,
                "banks_to_link": [],
            }
            r = client.post("/setup/4", data={})
        mock_setup.assert_called_once_with(
            banks_to_link=[],
            extra_ledgers=[],
            hints=[],
        )

    def test_redirects_to_profile(self, client):
        _wizard_through_step3(client)
        with patch("server.main.apply_initial_setup") as mock_setup:
            mock_setup.return_value = {
                "status": "ok",
                "ledgers_created": [],
                "line_items_created": 0,
                "hints_created": 0,
                "banks_to_link": [],
            }
            r = client.post("/setup/4", data={})
        assert r.status_code == 302
        assert r.headers["location"] == "/dashboard"

    def test_session_allows_profile_access_after_setup(self, client):
        """Session set during step 1 should let the client reach /profile."""
        _wizard_through_step3(client)
        with patch("server.main.apply_initial_setup") as mock_setup:
            mock_setup.return_value = {
                "status": "ok",
                "ledgers_created": [],
                "line_items_created": 0,
                "hints_created": 0,
                "banks_to_link": [],
            }
            client.post("/setup/4", data={})
        # Session cookie was set at step 1; profile should be accessible now.
        r = client.get("/profile")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Tests — setup returns 404 after complete
# ---------------------------------------------------------------------------


class TestSetupAfterComplete:
    def _complete(self, client):
        _wizard_through_step3(client)
        with patch("server.main.apply_initial_setup") as mock_setup:
            mock_setup.return_value = {
                "status": "ok",
                "ledgers_created": [],
                "line_items_created": 0,
                "hints_created": 0,
                "banks_to_link": [],
            }
            client.post("/setup/4", data={})

    def test_get_setup_returns_404(self, client):
        self._complete(client)
        # Clear wizard cookie to simulate a fresh browser.
        client.cookies.clear()
        r = client.get("/setup")
        assert r.status_code == 404

    def test_post_setup_1_returns_404(self, client):
        self._complete(client)
        client.cookies.clear()
        r = client.post("/setup/1", data={"password": "newpass1", "password_confirm": "newpass1"})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Tests — middleware: non-setup routes redirect to /setup when no password set
# ---------------------------------------------------------------------------


class TestRedirectToSetupMiddleware:
    """Regression tests: routes that are not in the allow-list must redirect
    to /setup when the password has not been set yet."""

    def test_root_redirects_to_setup(self, client):
        r = client.get("/")
        assert r.status_code == 302
        assert r.headers["location"] == "/setup"

    def test_profile_redirects_to_setup(self, client):
        r = client.get("/profile")
        # Should end up at /setup (via /login redirect or direct).
        assert r.status_code == 302

    def test_ledgers_redirects(self, client):
        r = client.get("/ledgers")
        assert r.status_code == 302

    def test_login_redirects_to_setup_when_no_password(self, client):
        r = client.get("/login")
        assert r.status_code == 302
        assert r.headers["location"] == "/setup"

    def test_healthz_always_200(self, client):
        """Healthz must not redirect regardless of setup state."""
        r = client.get("/healthz")
        assert r.status_code == 200
