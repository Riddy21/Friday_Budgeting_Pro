"""
tests/test_plaid_revocation_log.py — Tests for issue #265: plaid_revocation_log.

Verifies that:
  1. complete_link() creates a matching log row
  2. disconnect() marks the log row revoked=1 on Plaid success
  3. disconnect() leaves the log row revoked=0 when Plaid fails
  4. retry_pending_revocations() marks rows revoked and returns correct counts
  5. wipe.py _load_connections() uses plaid_revocation_log (revoked=0) as its
     token source
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import server.paths
from server.db import get_db, init_db

# ---------------------------------------------------------------------------
# Fixtures (same pattern as test_bank_tools.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    init_db(path)
    monkeypatch.setattr(server.paths, "DB_PATH", path)
    return path


@pytest.fixture(autouse=True)
def patch_crypto(monkeypatch):
    monkeypatch.setattr("server.crypto.encrypt", MagicMock(side_effect=lambda t: "enc:" + t))
    monkeypatch.setattr("server.crypto.decrypt", MagicMock(side_effect=lambda t: t[len("enc:") :]))


# ---------------------------------------------------------------------------
# Helper: call complete_link with mocked Plaid
# ---------------------------------------------------------------------------


def _do_complete_link(token="public-tok", access="access-sandbox-abc", item_id="item-123"):
    with patch("server.main.PlaidProvider") as mock_cls:
        mock_cls.return_value.exchange_public_token.return_value = {
            "access_token": access,
            "item_id": item_id,
        }
        mock_cls.return_value.get_institution_name.return_value = None
        mock_cls.return_value.env = "sandbox"
        from server.main import complete_link  # noqa: PLC0415

        return complete_link(token)


# ---------------------------------------------------------------------------
# 1. complete_link() creates a log row
# ---------------------------------------------------------------------------


def test_complete_link_creates_revocation_log_row(db_path):
    result = _do_complete_link(access="access-tok-1", item_id="item-1")
    assert "connection_id" in result

    conn = get_db(db_path)
    row = conn.execute(
        "SELECT * FROM plaid_revocation_log WHERE plaid_item_id = ?", ("item-1",)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["access_token_encrypted"] == "enc:access-tok-1"
    assert row["revoked"] == 0
    assert row["revoked_at"] is None


# ---------------------------------------------------------------------------
# 2. disconnect() marks log row revoked=1 on Plaid success
# ---------------------------------------------------------------------------


def test_disconnect_marks_log_row_revoked_on_success(db_path):
    result = _do_complete_link(access="access-tok-2", item_id="item-2")
    connection_id = result["connection_id"]

    from server.main import disconnect  # noqa: PLC0415

    with patch("server.main.PlaidProvider") as mock_cls:
        mock_cls.return_value.remove_item.return_value = {"revoked": True}
        disconnect(connection_id)

    conn = get_db(db_path)
    row = conn.execute(
        "SELECT * FROM plaid_revocation_log WHERE plaid_item_id = ?", ("item-2",)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["revoked"] == 1
    assert row["revoked_at"] is not None


# ---------------------------------------------------------------------------
# 3. disconnect() leaves log row revoked=0 when Plaid fails
# ---------------------------------------------------------------------------


def test_disconnect_leaves_log_row_unrevoked_on_plaid_failure(db_path):
    result = _do_complete_link(access="access-tok-3", item_id="item-3")
    connection_id = result["connection_id"]

    from server.main import disconnect  # noqa: PLC0415

    with patch(
        "server.providers.plaid.PlaidProvider.remove_item",
        side_effect=Exception("Plaid API error"),
    ):
        disc_result = disconnect(connection_id)

    # Local cleanup still worked
    assert disc_result["ok"] is True
    assert disc_result["plaid_item_removed"] is False
    assert "plaid_error" in disc_result

    # Log row must still exist with revoked=0 (retryable)
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT * FROM plaid_revocation_log WHERE plaid_item_id = ?", ("item-3",)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["revoked"] == 0
    assert row["revoked_at"] is None


# ---------------------------------------------------------------------------
# 4a. retry_pending_revocations() — all succeed
# ---------------------------------------------------------------------------


def test_retry_pending_revocations_succeeds(db_path):
    # Insert two connections
    _do_complete_link(access="access-retry-1", item_id="item-retry-1", token="pub-1")
    _do_complete_link(access="access-retry-2", item_id="item-retry-2", token="pub-2")

    from server.main import retry_pending_revocations  # noqa: PLC0415

    with patch("server.main.PlaidProvider") as mock_cls:
        mock_cls.return_value.remove_item.return_value = {"revoked": True}
        result = retry_pending_revocations()

    assert result["attempted"] == 2
    assert result["succeeded"] == 2
    assert result["failed"] == 0

    conn = get_db(db_path)
    unrevoked = conn.execute(
        "SELECT COUNT(*) FROM plaid_revocation_log WHERE revoked=0"
    ).fetchone()[0]
    conn.close()
    assert unrevoked == 0


# ---------------------------------------------------------------------------
# 4b. retry_pending_revocations() — partial failure
# ---------------------------------------------------------------------------


def test_retry_pending_revocations_partial_failure(db_path):
    _do_complete_link(access="access-ok", item_id="item-ok", token="pub-ok")
    _do_complete_link(access="access-bad", item_id="item-bad", token="pub-bad")

    from server.main import retry_pending_revocations  # noqa: PLC0415

    call_count = 0

    def _remove_item_side_effect(token):
        nonlocal call_count
        call_count += 1
        if "bad" in token:
            raise Exception("Plaid error")
        return {"revoked": True}

    with patch("server.main.PlaidProvider") as mock_cls:
        mock_cls.return_value.remove_item.side_effect = _remove_item_side_effect
        result = retry_pending_revocations()

    assert result["attempted"] == 2
    assert result["succeeded"] == 1
    assert result["failed"] == 1

    conn = get_db(db_path)
    revoked = conn.execute("SELECT COUNT(*) FROM plaid_revocation_log WHERE revoked=1").fetchone()[
        0
    ]
    still_pending = conn.execute(
        "SELECT COUNT(*) FROM plaid_revocation_log WHERE revoked=0"
    ).fetchone()[0]
    conn.close()
    assert revoked == 1
    assert still_pending == 1


# ---------------------------------------------------------------------------
# 4c. retry_pending_revocations() — empty (no pending rows)
# ---------------------------------------------------------------------------


def test_retry_pending_revocations_empty(db_path):
    from server.main import retry_pending_revocations  # noqa: PLC0415

    result = retry_pending_revocations()
    assert result == {"attempted": 0, "succeeded": 0, "failed": 0}


# ---------------------------------------------------------------------------
# 5. wipe.py _load_connections() uses plaid_revocation_log
# ---------------------------------------------------------------------------


def test_wipe_load_connections_uses_revocation_log(db_path, monkeypatch):
    """_load_connections() should read from plaid_revocation_log, not bank_connections."""
    import sys

    # Ensure fresh import
    for key in list(sys.modules.keys()):
        if "wipe" in key:
            del sys.modules[key]

    monkeypatch.setattr(server.paths, "DB_PATH", db_path)

    # Insert a log row directly (simulating a connection whose bank_connections
    # row was already deleted but the token is still pending revocation)
    conn = get_db(db_path)
    conn.execute(
        "INSERT INTO plaid_revocation_log "
        "(id, plaid_item_id, access_token_encrypted, plaid_env, revoked) "
        "VALUES ('log-id-1', 'item-wipe-1', 'enc:tok-wipe-1', 'sandbox', 0)"
    )
    conn.execute(
        "INSERT INTO plaid_revocation_log "
        "(id, plaid_item_id, access_token_encrypted, plaid_env, revoked) "
        "VALUES ('log-id-2', 'item-wipe-2', 'enc:tok-wipe-2', 'sandbox', 1)"
    )
    conn.commit()
    conn.close()

    # Import and call _load_connections from wipe.py
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "wipe",
        pathlib.Path(__file__).parent.parent / "scripts" / "wipe.py",
    )
    wipe_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wipe_mod)  # type: ignore[union-attr]

    conn2 = get_db(db_path)
    connections = wipe_mod._load_connections(conn2)
    conn2.close()

    # Only the unrevoked row should be returned
    assert len(connections) == 1
    assert connections[0]["id"] == "log-id-1"
    assert connections[0]["plaid_access_token_encrypted"] == "enc:tok-wipe-1"
