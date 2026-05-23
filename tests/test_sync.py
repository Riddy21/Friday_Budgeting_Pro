"""
tests/test_sync.py — Tests for server.main.sync() (issue #16).

Covers:
  - Normal sync: 3 added transactions, 1 matched by routing rule
  - ITEM_LOGIN_REQUIRED: connection flagged as needs_reauth, no crash
  - Idempotent re-sync: no duplicate transactions on second call
  - sync_lock contention: returns {"status": "already_running"}
"""

from __future__ import annotations

import json
import uuid

import pytest

from server.db import get_db, init_db
from server.main import sync


# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

class _FakePlaidError(Exception):
    """Minimal stand-in for plaid.ApiException with a JSON body."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.body = json.dumps({"error_code": error_code})


_ADDED_TXNS = [
    {
        "transaction_id": "plaid-txn-1",
        "account_id": "plaid-acct-1",
        "date": "2024-01-01",
        "name": "Starbucks Coffee",
        "merchant_name": "Starbucks",
        "amount": 5.50,
        "pending": False,
    },
    {
        "transaction_id": "plaid-txn-2",
        "account_id": "plaid-acct-1",
        "date": "2024-01-02",
        "name": "Uber",
        "merchant_name": "Uber",
        "amount": 12.00,
        "pending": False,
    },
    {
        "transaction_id": "plaid-txn-3",
        "account_id": "plaid-acct-1",
        "date": "2024-01-03",
        "name": "Amazon",
        "merchant_name": None,
        "amount": 35.00,
        "pending": True,
    },
]


def _mock_sync_ok(access_token, cursor=None):
    return {
        "added": _ADDED_TXNS,
        "modified": [],
        "removed": [],
        "next_cursor": "cursor-2",
    }


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Patch paths, init a tmp DB, and seed the minimum required rows."""
    db = tmp_path / "data.db"
    lock = tmp_path / "sync.lock"
    monkeypatch.setattr("server.paths.DB_PATH", db)
    monkeypatch.setattr("server.paths.SYNC_LOCK_PATH", lock)

    init_db(db)

    ledger_id = str(uuid.uuid4())
    line_item_id = str(uuid.uuid4())
    connection_id = str(uuid.uuid4())
    rule_id = str(uuid.uuid4())

    conn = get_db(db)
    conn.execute(
        "INSERT INTO ledgers (id, name) VALUES (?, ?)",
        (ledger_id, "Personal"),
    )
    conn.execute(
        "INSERT INTO line_items (id, ledger_id, name, item_type) VALUES (?, ?, ?, 'expense')",
        (line_item_id, ledger_id, "Dining"),
    )
    conn.execute(
        "INSERT INTO bank_connections "
        "(id, plaid_item_id, plaid_access_token_encrypted, status) "
        "VALUES (?, 'item-1', 'test-token', 'active')",
        (connection_id,),
    )
    conn.execute(
        "INSERT INTO routing_rules (id, merchant_pattern, line_item_id) VALUES (?, ?, ?)",
        (rule_id, "starbucks", line_item_id),
    )
    conn.commit()
    conn.close()

    return {
        "db": db,
        "lock": lock,
        "connection_id": connection_id,
        "ledger_id": ledger_id,
        "line_item_id": line_item_id,
    }


# ---------------------------------------------------------------------------
# Test 1: normal sync
# ---------------------------------------------------------------------------

def test_sync_normal(env, monkeypatch):
    monkeypatch.setattr("server.plaid_client.sync_transactions", _mock_sync_ok)
    monkeypatch.setattr("server.crypto.decrypt", lambda x: x)

    result = sync()

    # Return-value assertions
    assert result["status"] == "ok"
    assert result["connections_synced"] == 1
    assert result["added"] == 3
    assert result["classified_by_rule"] == 1

    conn = get_db(env["db"])

    # 3 rows in transactions
    txns = conn.execute("SELECT * FROM transactions").fetchall()
    assert len(txns) == 3

    # 1 transaction_entry with source='rule' (only Starbucks matched)
    entries = conn.execute("SELECT * FROM transaction_entries").fetchall()
    assert len(entries) == 1
    assert entries[0]["source"] == "rule"

    # sync_cursors persisted
    cursor_row = conn.execute("SELECT cursor FROM sync_cursors").fetchone()
    assert cursor_row is not None
    assert cursor_row["cursor"] == "cursor-2"

    # bank_connections.last_synced_at set
    bc = conn.execute(
        "SELECT last_synced_at FROM bank_connections WHERE id = ?",
        (env["connection_id"],),
    ).fetchone()
    assert bc["last_synced_at"] is not None

    conn.close()


# ---------------------------------------------------------------------------
# Test 2: ITEM_LOGIN_REQUIRED → connection status = needs_reauth
# ---------------------------------------------------------------------------

def test_sync_item_login_required(env, monkeypatch):
    def _raise_reauth(access_token, cursor=None):
        raise _FakePlaidError("ITEM_LOGIN_REQUIRED")

    monkeypatch.setattr("server.plaid_client.sync_transactions", _raise_reauth)
    monkeypatch.setattr("server.crypto.decrypt", lambda x: x)

    result = sync()

    # sync() should complete without crashing; 0 connections successfully synced
    assert result["status"] == "ok"
    assert result["connections_synced"] == 0

    conn = get_db(env["db"])
    bc = conn.execute(
        "SELECT status FROM bank_connections WHERE id = ?",
        (env["connection_id"],),
    ).fetchone()
    assert bc["status"] == "needs_reauth"
    conn.close()


# ---------------------------------------------------------------------------
# Test 3: idempotent re-sync — no duplicate transactions
# ---------------------------------------------------------------------------

def test_sync_idempotent(env, monkeypatch):
    monkeypatch.setattr("server.plaid_client.sync_transactions", _mock_sync_ok)
    monkeypatch.setattr("server.crypto.decrypt", lambda x: x)

    sync()
    sync()  # second call with identical batch — INSERT OR IGNORE absorbs it

    conn = get_db(env["db"])
    txns = conn.execute("SELECT * FROM transactions").fetchall()
    assert len(txns) == 3  # still exactly 3, not 6
    conn.close()


# ---------------------------------------------------------------------------
# Test 4: sync_lock contention → {"status": "already_running"}
# ---------------------------------------------------------------------------

def test_sync_lock_contention(env, monkeypatch):
    from server.sync_lock import acquire_sync_lock

    monkeypatch.setattr("server.plaid_client.sync_transactions", _mock_sync_ok)
    monkeypatch.setattr("server.crypto.decrypt", lambda x: x)

    # Hold the lock ourselves so sync() cannot acquire it
    held = acquire_sync_lock(timeout=0.0)
    assert held is not None, "could not acquire lock in test setup"
    try:
        result = sync()
    finally:
        held.close()

    assert result == {"status": "already_running"}
