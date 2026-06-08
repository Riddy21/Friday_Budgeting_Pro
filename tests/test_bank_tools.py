"""
tests/test_bank_tools.py — Tests for the bank-connection MCP tools in server/main.py.

Uses a tmp_path SQLite DB (monkeypatching server.paths.DB_PATH) so no real
Keychain or Plaid network calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import server.paths
from server.db import get_db, init_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """Create a fresh temp DB and monkeypatch server.paths.DB_PATH to point at it."""
    path = tmp_path / "test.db"
    init_db(path)
    monkeypatch.setattr(server.paths, "DB_PATH", path)
    return path


@pytest.fixture(autouse=True)
def patch_crypto(monkeypatch):
    """Patch encrypt/decrypt with transparent passthrough fakes."""
    fake_encrypt = MagicMock(side_effect=lambda plaintext: "enc:" + plaintext)
    fake_decrypt = MagicMock(side_effect=lambda ciphertext: ciphertext[len("enc:") :])
    monkeypatch.setattr("server.crypto.encrypt", fake_encrypt)
    monkeypatch.setattr("server.crypto.decrypt", fake_decrypt)


# ---------------------------------------------------------------------------
# start_link
# ---------------------------------------------------------------------------


def test_start_link_returns_url_with_link_token(db_path):
    with patch(
        "server.providers.plaid.PlaidProvider.create_link_token", return_value="link-token-abc"
    ) as mock_create:
        from server.main import start_link

        result = start_link()

    assert "url" in result
    assert "link-token-abc" in result["url"]
    mock_create.assert_called_once()


# ---------------------------------------------------------------------------
# complete_link
# ---------------------------------------------------------------------------


def test_complete_link_inserts_row_and_returns_connection_id(db_path):
    exchange_result = {"access_token": "access-sandbox-xyz", "item_id": "item-abc"}

    with patch(
        "server.providers.plaid.PlaidProvider.exchange_public_token", return_value=exchange_result
    ) as mock_exchange:
        from server.main import complete_link

        result = complete_link("public-token-test")

    assert "connection_id" in result
    connection_id = result["connection_id"]
    assert result["institution_name"] is None
    mock_exchange.assert_called_once_with("public-token-test")

    # Verify the row was inserted and the encrypted token is stored
    conn = get_db(db_path)
    row = conn.execute("SELECT * FROM bank_connections WHERE id = ?", (connection_id,)).fetchone()
    conn.close()

    assert row is not None
    assert row["plaid_item_id"] == "item-abc"
    assert row["plaid_access_token_encrypted"] == "enc:access-sandbox-xyz"
    assert row["status"] == "active"
    # Encrypted token must NOT be the plaintext
    assert row["plaid_access_token_encrypted"] != "access-sandbox-xyz"


# ---------------------------------------------------------------------------
# list_connections
# ---------------------------------------------------------------------------


def test_list_connections_returns_all_without_encrypted_token(db_path):
    exchange_result_1 = {"access_token": "access-1", "item_id": "item-1"}
    exchange_result_2 = {"access_token": "access-2", "item_id": "item-2"}

    with patch(
        "server.providers.plaid.PlaidProvider.exchange_public_token",
        side_effect=[exchange_result_1, exchange_result_2],
    ):
        from server.main import complete_link, list_connections

        complete_link("public-token-1")
        complete_link("public-token-2")

    result = list_connections()

    assert "connections" in result
    assert len(result["connections"]) == 2

    for conn_entry in result["connections"]:
        # Required fields present
        assert "id" in conn_entry
        assert "institution_name" in conn_entry
        assert "status" in conn_entry
        assert "last_synced_at" in conn_entry
        # Encrypted token must NEVER appear
        assert "plaid_access_token_encrypted" not in conn_entry


# ---------------------------------------------------------------------------
# disconnect
# ---------------------------------------------------------------------------


def test_disconnect_removes_connection_and_sync_cursor(db_path):
    exchange_result = {"access_token": "access-del", "item_id": "item-del"}

    with patch(
        "server.providers.plaid.PlaidProvider.exchange_public_token", return_value=exchange_result
    ):
        from server.main import complete_link

        result = complete_link("public-token-del")

    connection_id = result["connection_id"]

    # Insert a sync_cursor row for this connection
    conn = get_db(db_path)
    conn.execute(
        "INSERT INTO sync_cursors (connection_id, cursor) VALUES (?, ?)",
        (connection_id, "cursor-val"),
    )
    conn.commit()
    conn.close()

    from server.main import disconnect

    with patch("server.main.PlaidProvider") as mock_provider_cls:
        mock_provider_instance = mock_provider_cls.return_value
        mock_provider_instance.remove_item.return_value = {"revoked": True, "request_id": "req-abc"}
        disconnect_result = disconnect(connection_id)
    mock_remove = mock_provider_instance.remove_item

    assert disconnect_result["ok"] is True
    assert disconnect_result["plaid_item_removed"] is True
    assert "plaid_error" not in disconnect_result
    mock_remove.assert_called_once()

    # Verify both rows are gone
    conn = get_db(db_path)
    bc_row = conn.execute(
        "SELECT id FROM bank_connections WHERE id = ?", (connection_id,)
    ).fetchone()
    sc_row = conn.execute(
        "SELECT connection_id FROM sync_cursors WHERE connection_id = ?", (connection_id,)
    ).fetchone()
    conn.close()

    assert bc_row is None
    assert sc_row is None


def test_disconnect_still_removes_local_row_when_plaid_remove_fails(db_path):
    """Plaid /item/remove failure must not prevent the local DB row from being deleted."""
    exchange_result = {"access_token": "access-fail", "item_id": "item-fail"}

    with patch(
        "server.providers.plaid.PlaidProvider.exchange_public_token", return_value=exchange_result
    ):
        from server.main import complete_link

        result = complete_link("public-token-fail")

    connection_id = result["connection_id"]

    from server.main import disconnect

    with patch(
        "server.providers.plaid.PlaidProvider.remove_item",
        side_effect=Exception("Plaid API error"),
    ):
        disconnect_result = disconnect(connection_id)

    # ok=True because local cleanup succeeded
    assert disconnect_result["ok"] is True
    assert disconnect_result["plaid_item_removed"] is False
    assert "plaid_error" in disconnect_result
    assert "Plaid API error" in disconnect_result["plaid_error"]

    # Row must be gone locally despite Plaid failure
    conn = get_db(db_path)
    bc_row = conn.execute(
        "SELECT id FROM bank_connections WHERE id = ?", (connection_id,)
    ).fetchone()
    conn.close()
    assert bc_row is None


def test_disconnect_nonexistent_id_returns_ok(db_path):
    """Disconnecting an ID that doesn't exist should be a silent no-op."""
    from server.main import disconnect

    # remove_item should never be called because there's no row to load
    with patch("server.providers.plaid.PlaidProvider.remove_item") as mock_remove:
        result = disconnect("nonexistent-id-xyz")

    assert result["ok"] is True
    assert result["plaid_item_removed"] is False
    mock_remove.assert_not_called()


# ---------------------------------------------------------------------------
# refresh_connection
# ---------------------------------------------------------------------------


def test_refresh_connection_returns_url_with_link_token(db_path):
    """refresh_connection must fetch the stored access token and pass it to
    create_link_token for true Plaid Update Mode."""
    exchange_result = {"access_token": "access-refresh", "item_id": "item-refresh"}
    with patch(
        "server.providers.plaid.PlaidProvider.exchange_public_token",
        return_value=exchange_result,
    ):
        from server.main import complete_link

        link_result = complete_link("public-token-refresh")

    connection_id = link_result["connection_id"]

    with patch(
        "server.providers.plaid.PlaidProvider.create_link_token", return_value="link-update-token"
    ) as mock_create:
        from server.main import refresh_connection

        result = refresh_connection(connection_id)

    assert "url" in result
    assert "link-update-token" in result["url"]
    # connection_id must be embedded in the URL so /link/complete can use Update Mode
    assert connection_id in result["url"]
    assert result.get("connection_id") == connection_id
    # Must have been called with the decrypted access_token for Update Mode
    mock_create.assert_called_once_with(access_token="access-refresh")


def test_refresh_connection_unknown_id_returns_error(db_path):
    """refresh_connection on a non-existent connection_id must return an error dict."""
    from server.main import refresh_connection

    result = refresh_connection("no-such-id")
    assert "error" in result


def test_complete_link_update_mode_updates_existing_connection(db_path):
    """complete_link with connection_id= must update the existing row, not insert a new one."""
    exchange_result = {"access_token": "access-orig", "item_id": "item-orig"}
    with patch(
        "server.providers.plaid.PlaidProvider.exchange_public_token",
        return_value=exchange_result,
    ):
        from server.main import complete_link

        create_result = complete_link("public-token-orig")

    connection_id = create_result["connection_id"]

    # Simulate needs_reauth
    conn = get_db(db_path)
    conn.execute("UPDATE bank_connections SET status='needs_reauth' WHERE id=?", (connection_id,))
    conn.commit()
    conn.close()

    # Re-auth via Update Mode
    reauth_exchange = {"access_token": "access-refreshed", "item_id": "item-orig"}
    with patch(
        "server.providers.plaid.PlaidProvider.exchange_public_token",
        return_value=reauth_exchange,
    ):
        update_result = complete_link("public-token-reauth", connection_id=connection_id)

    assert update_result["connection_id"] == connection_id
    assert update_result.get("update_mode") is True

    # Verify status is back to active and no duplicate connection was created
    conn = get_db(db_path)
    rows = conn.execute("SELECT id, status FROM bank_connections").fetchall()
    conn.close()
    assert len(rows) == 1, "Update Mode must not create a duplicate connection"
    assert rows[0]["status"] == "active"


def test_disconnect_cascade_deletes_accounts_and_transactions(db_path):
    """disconnect must cascade-delete bank_accounts and transactions so the
    FK constraint on bank_connections never fires."""
    exchange_result = {"access_token": "access-cascade", "item_id": "item-cascade"}
    with patch(
        "server.providers.plaid.PlaidProvider.exchange_public_token",
        return_value=exchange_result,
    ):
        from server.main import complete_link

        result = complete_link("public-token-cascade")

    connection_id = result["connection_id"]

    # Manually insert a bank_account and transaction to simulate a real connection
    import uuid

    acct_id = str(uuid.uuid4())
    txn_id = str(uuid.uuid4())
    conn = get_db(db_path)
    conn.execute(
        "INSERT INTO bank_accounts (id, connection_id, plaid_account_id, name) VALUES (?, ?, ?, ?)",
        (acct_id, connection_id, "plaid-acct-1", "Test Account"),
    )
    conn.execute(
        "INSERT INTO transactions (id, bank_account_id, plaid_transaction_id, date, amount)"
        " VALUES (?, ?, ?, '2024-01-01', 10.0)",
        (txn_id, acct_id, "plaid-txn-1"),
    )
    conn.commit()
    conn.close()

    from server.main import disconnect

    with patch("server.main.PlaidProvider") as mock_cls:
        mock_cls.return_value.remove_item.return_value = {"revoked": True, "request_id": "r"}
        disconnect_result = disconnect(connection_id)

    assert disconnect_result["ok"] is True

    conn = get_db(db_path)
    assert conn.execute("SELECT id FROM bank_connections WHERE id=?", (connection_id,)).fetchone() is None
    assert conn.execute("SELECT id FROM bank_accounts WHERE id=?", (acct_id,)).fetchone() is None
    assert conn.execute("SELECT id FROM transactions WHERE id=?", (txn_id,)).fetchone() is None
    conn.close()
