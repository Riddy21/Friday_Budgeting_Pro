"""
tests/test_configure_flinks.py — Tests for the Flinks MCP tools.

Covers:
  * configure_flinks — .env merge (does NOT clobber Plaid keys) + validation
  * complete_flinks_link — creates a bank_connections row with
    connection_type='flinks' and flinks_login_id set.
"""

from __future__ import annotations

import os
import stat
import threading

import pytest

import server.main as main_module
import server.paths
from server.db import get_db, init_db
from server.main import complete_flinks_link, configure_flinks

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patch_project_root(tmp_path, monkeypatch):
    """Redirect all .env writes to tmp_path so tests never touch the real repo."""
    monkeypatch.setattr(main_module, "project_root", tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def no_real_background_sync(monkeypatch):
    """Prevent complete_flinks_link's background sync thread from running.

    complete_flinks_link spawns a daemon thread that calls the real sync().
    In the test suite that lingering thread can run after per-test monkeypatch
    teardown and pollute other tests, so we stub Thread.start() to a no-op.
    """

    class _NoRunThread(threading.Thread):
        def start(self):  # noqa: D401 - deliberately does nothing
            return None

    monkeypatch.setattr(threading, "Thread", _NoRunThread)
    yield


@pytest.fixture(autouse=True)
def clean_environ():
    keys = (
        "FLINKS_CUSTOMER_ID",
        "FLINKS_SECRET_KEY",
        "FLINKS_X_API_KEY",
        "FLINKS_INSTANCE",
    )
    saved = {k: os.environ.pop(k, None) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# configure_flinks
# ---------------------------------------------------------------------------


class TestConfigureFlinks:
    def test_writes_env_and_returns_ok(self, tmp_path):
        result = configure_flinks("cust", "sekret", "xapi", "toolbox")
        assert result == {"ok": True, "instance": "toolbox"}
        content = (tmp_path / ".env").read_text()
        assert "FLINKS_CUSTOMER_ID=cust" in content
        assert "FLINKS_SECRET_KEY=sekret" in content
        assert "FLINKS_X_API_KEY=xapi" in content
        assert "FLINKS_INSTANCE=toolbox" in content

    def test_env_file_mode_is_0o600(self, tmp_path):
        configure_flinks("cust", "sekret", "xapi")
        mode = stat.S_IMODE((tmp_path / ".env").stat().st_mode)
        assert mode == 0o600

    def test_merge_preserves_existing_plaid_keys(self, tmp_path):
        # Pre-seed .env with Plaid creds + a comment.
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# my env\nPLAID_CLIENT_ID=pid\nPLAID_SECRET=psecret\nPLAID_ENV=production\n"
        )
        configure_flinks("cust", "sekret", "xapi", "toolbox")
        content = env_file.read_text()
        # Plaid keys survive the merge …
        assert "PLAID_CLIENT_ID=pid" in content
        assert "PLAID_SECRET=psecret" in content
        assert "# my env" in content
        # … and Flinks keys are added.
        assert "FLINKS_CUSTOMER_ID=cust" in content
        assert "FLINKS_X_API_KEY=xapi" in content

    def test_calling_twice_replaces_flinks_values_only(self, tmp_path):
        configure_flinks("c1", "s1", "x1", "toolbox")
        configure_flinks("c2", "s2", "x2", "prod-instance")
        content = (tmp_path / ".env").read_text()
        assert "c2" in content and "x2" in content
        assert "FLINKS_INSTANCE=prod-instance" in content
        assert "c1" not in content
        assert content.count("FLINKS_CUSTOMER_ID") == 1

    def test_os_environ_updated(self):
        configure_flinks("cust", "sekret", "xapi", "toolbox")
        assert os.environ["FLINKS_CUSTOMER_ID"] == "cust"
        assert os.environ["FLINKS_X_API_KEY"] == "xapi"
        assert os.environ["FLINKS_INSTANCE"] == "toolbox"

    def test_default_instance_is_toolbox(self, tmp_path):
        result = configure_flinks("cust", "sekret", "xapi")
        assert result["instance"] == "toolbox"

    def test_empty_customer_id_raises(self):
        with pytest.raises(ValueError, match="customer_id must be non-empty"):
            configure_flinks("", "sekret", "xapi")

    def test_empty_secret_key_raises(self):
        with pytest.raises(ValueError, match="secret_key must be non-empty"):
            configure_flinks("cust", "", "xapi")

    def test_empty_x_api_key_raises(self):
        with pytest.raises(ValueError, match="x_api_key must be non-empty"):
            configure_flinks("cust", "sekret", "")

    def test_secret_not_in_logs(self, tmp_path, caplog):
        import logging

        secret = "super-secret-flinks-key"
        with caplog.at_level(logging.DEBUG):
            configure_flinks("cust", secret, "xapi")
        assert secret not in caplog.text


# ---------------------------------------------------------------------------
# complete_flinks_link
# ---------------------------------------------------------------------------


class TestCompleteFlinksLink:
    def test_creates_flinks_connection_row(self, tmp_path, monkeypatch):
        # Fresh DB.
        init_db(server.paths.DB_PATH)
        # Avoid the background sync doing anything real.
        monkeypatch.setattr(main_module, "sync", lambda *a, **k: {"ok": True})

        result = complete_flinks_link(
            login_id="login-xyz", institution_name="RBC Royal Bank"
        )
        assert result["status"] == "ok"
        conn = get_db(server.paths.DB_PATH)
        try:
            row = conn.execute(
                "SELECT connection_type, flinks_login_id, institution_name, status "
                "FROM bank_connections WHERE id = ?",
                (result["connection_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert row["connection_type"] == "flinks"
        assert row["flinks_login_id"] == "login-xyz"
        assert row["institution_name"] == "RBC Royal Bank"
        assert row["status"] == "active"

    def test_reconnect_same_login_is_idempotent(self, tmp_path, monkeypatch):
        init_db(server.paths.DB_PATH)
        monkeypatch.setattr(main_module, "sync", lambda *a, **k: {"ok": True})

        r1 = complete_flinks_link(login_id="dup-login")
        r2 = complete_flinks_link(login_id="dup-login")
        assert r1["connection_id"] == r2["connection_id"]

        conn = get_db(server.paths.DB_PATH)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM bank_connections WHERE flinks_login_id = ?",
                ("dup-login",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_empty_login_id_returns_error(self, monkeypatch):
        monkeypatch.setattr(main_module, "sync", lambda *a, **k: {"ok": True})
        result = complete_flinks_link(login_id="")
        assert result["status"] == "error"
