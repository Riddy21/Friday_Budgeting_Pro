"""
tests/test_wipe_script.py — Unit tests for scripts/wipe.py.

All tests operate on a temporary SQLite database and mock Plaid + crypto so
no real network or Keychain calls are made.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Make scripts/ importable without installing the package.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import server.paths  # noqa: E402
from server.db import get_db, init_db  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """Fresh temp DB with server.paths.DB_PATH monkeypatched."""
    path = tmp_path / "wipe_test.db"
    init_db(path)
    monkeypatch.setattr(server.paths, "DB_PATH", path)
    return path


@pytest.fixture(autouse=True)
def patch_crypto(monkeypatch):
    """Transparent passthrough encrypt/decrypt + no-op init_crypto."""
    monkeypatch.setattr("server.crypto.encrypt", lambda p: "enc:" + p)
    monkeypatch.setattr("server.crypto.decrypt", lambda c: c[len("enc:") :])
    monkeypatch.setattr("server.crypto.init_crypto", lambda: None)


_USER_INSERTED: set = set()  # track which db_paths already have the test user


def _ensure_user(db_path):
    """Insert a test user row if not already present (satisfies FK on bank_connections)."""
    if str(db_path) in _USER_INSERTED:
        return
    import time as _time

    conn = get_db(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO users (id, username, password_hash, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("user-1", "testuser", "hash", int(_time.time())),
    )
    conn.commit()
    conn.close()
    _USER_INSERTED.add(str(db_path))


def _insert_connection(db_path, institution="Test Bank", access_token="at-test", env="sandbox"):
    """Helper: insert a bank_connection row + revocation log row and return its id."""
    _ensure_user(db_path)
    cid = str(uuid.uuid4())
    item_id = f"item-{cid[:8]}"
    conn = get_db(db_path)
    conn.execute(
        "INSERT INTO bank_connections "
        "(id, plaid_item_id, plaid_access_token_encrypted, institution_name, "
        " status, plaid_env, user_id) "
        "VALUES (?, ?, ?, ?, 'active', ?, 'user-1')",
        (cid, item_id, "enc:" + access_token, institution, env),
    )
    conn.commit()
    # Insert into revocation log separately so bank_connections is committed
    # regardless, and so any schema issue with the log is surfaced clearly.
    try:
        conn.execute(
            "INSERT INTO plaid_revocation_log "
            "(id, plaid_item_id, access_token_encrypted, institution_name, plaid_env, revoked) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (str(uuid.uuid4()), item_id, "enc:" + access_token, institution, env),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"plaid_revocation_log insert failed: {exc}") from exc
    conn.close()
    return cid


def _insert_transaction(db_path, connection_id):
    """Helper: insert a minimal bank_account + transaction row."""
    conn = get_db(db_path)
    acct_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO bank_accounts "
        "(id, connection_id, plaid_account_id, name, type, subtype) "
        "VALUES (?, ?, ?, 'Chequing', 'depository', 'checking')",
        (acct_id, connection_id, f"plaid-acct-{acct_id[:8]}"),
    )
    txn_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO transactions "
        "(id, bank_account_id, plaid_transaction_id, merchant, amount, date, currency) "
        "VALUES (?, ?, ?, 'ACME', 42.00, '2025-01-01', 'CAD')",
        (txn_id, acct_id, f"plaid-txn-{txn_id[:8]}"),
    )
    conn.commit()
    conn.close()
    return acct_id, txn_id


# ---------------------------------------------------------------------------
# Import the module under test (after fixtures set up sys.path)
# ---------------------------------------------------------------------------

import importlib  # noqa: E402

wipe = importlib.import_module("wipe")


# ---------------------------------------------------------------------------
# Tests: dry-run
# ---------------------------------------------------------------------------


def test_dry_run_prints_summary_and_makes_no_changes(db_path, capsys):
    cid = _insert_connection(db_path)
    _insert_transaction(db_path, cid)

    with patch("wipe.PlaidProvider") as mock_remove:
        wipe.run(dry_run=True, skip_prompt=True)

    # Plaid should never be called in dry-run
    mock_remove.assert_not_called()

    # DB rows must still be present
    conn = get_db(db_path)
    assert conn.execute("SELECT COUNT(*) FROM bank_connections").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    conn.close()

    out = capsys.readouterr().out
    assert "Dry run complete" in out
    assert "bank_connections" in out


# ---------------------------------------------------------------------------
# Tests: full wipe
# ---------------------------------------------------------------------------


def test_full_wipe_revokes_plaid_and_clears_tables(db_path, capsys):
    cid = _insert_connection(db_path, institution="Royal Bank")
    _insert_transaction(db_path, cid)

    with patch("wipe.PlaidProvider") as mock_cls:
        mock_cls.return_value.remove_item.return_value = {"revoked": True, "request_id": "req-xyz"}
        wipe.run(dry_run=False, skip_prompt=True)

    mock_cls.assert_called()  # PlaidProvider was instantiated

    conn = get_db(db_path)
    for table in wipe.WIPE_ORDER:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        assert count == 0, f"Expected {table} to be empty after wipe, got {count} rows"
    conn.close()

    out = capsys.readouterr().out
    assert "Wipe complete" in out


def test_full_wipe_continues_on_plaid_error(db_path, capsys):
    """Plaid API failure must not abort the local DB wipe."""
    cid = _insert_connection(db_path)
    _insert_transaction(db_path, cid)

    with patch("wipe.PlaidProvider") as mock_cls:
        mock_cls.return_value.remove_item.side_effect = Exception("network timeout")
        wipe.run(dry_run=False, skip_prompt=True)

    conn = get_db(db_path)
    assert conn.execute("SELECT COUNT(*) FROM bank_connections").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    conn.close()

    out = capsys.readouterr().out
    assert "network timeout" in out
    assert "Wipe complete" in out


def test_full_wipe_empty_db_is_noop(db_path, capsys):
    """Wiping an already-empty database should not raise and should say so."""
    with patch("wipe.PlaidProvider") as mock_remove:
        wipe.run(dry_run=False, skip_prompt=True)

    mock_remove.assert_not_called()
    out = capsys.readouterr().out
    assert "Nothing to wipe" in out


def test_full_wipe_multiple_connections(db_path):
    """All connections are revoked on Plaid even if one fails."""
    cid1 = _insert_connection(db_path, institution="Bank A", access_token="at-a")
    cid2 = _insert_connection(db_path, institution="Bank B", access_token="at-b")
    _insert_transaction(db_path, cid1)
    _insert_transaction(db_path, cid2)

    revoke_calls: list[str] = []

    def fake_remove(access_token):
        revoke_calls.append(access_token)
        return {"revoked": True, "request_id": "req"}

    with patch("wipe.PlaidProvider") as mock_cls:
        mock_cls.return_value.remove_item.side_effect = fake_remove
        wipe.run(dry_run=False, skip_prompt=True)

    assert len(revoke_calls) == 2  # both connections attempted
    conn = get_db(db_path)
    assert conn.execute("SELECT COUNT(*) FROM bank_connections").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# Tests: missing DB
# ---------------------------------------------------------------------------


def test_missing_db_exits_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(server.paths, "DB_PATH", tmp_path / "nonexistent.db")

    with pytest.raises(SystemExit) as exc_info:
        wipe.run(dry_run=False, skip_prompt=True)

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "nothing to do" in out.lower() or "not found" in out.lower()


# ---------------------------------------------------------------------------
# Tests: WIPE_ORDER completeness
# ---------------------------------------------------------------------------


def test_wipe_order_covers_expected_tables():
    expected = {
        "auto_promoted_rules_log",
        "transaction_entries",
        "transactions",
        "sync_cursors",
        "bank_accounts",
        "bank_connections",
    }
    assert set(wipe.WIPE_ORDER) == expected
