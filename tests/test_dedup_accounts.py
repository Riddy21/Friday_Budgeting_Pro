"""
tests/test_dedup_accounts.py — Tests for duplicate-account detection (issue #269).

Covers:
  - _deduplicate_accounts() marks the correct rows is_duplicate=1 / 0
  - The primary account (lexicographically smallest id) is kept
  - Accounts with NULL / empty mask are never deduplicated
  - Accounts from different users are not cross-contaminated
  - sync() skips transactions for duplicate accounts
  - _get_accounts_grouped() hides duplicates and reports hidden_count
"""

from __future__ import annotations

import uuid

import pytest

from server.db import get_db, init_db
from server.main import _deduplicate_accounts  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_user(conn, user_id=None):
    uid = user_id or str(uuid.uuid4())
    import time

    conn.execute(
        "INSERT OR IGNORE INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (uid, uid[:8], "x", int(time.time())),
    )
    return uid


def _mk_connection(conn, user_id, conn_id=None):
    cid = conn_id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO bank_connections "
        "(id, plaid_item_id, plaid_access_token_encrypted, status, user_id) "
        "VALUES (?, ?, ?, 'active', ?)",
        (cid, f"item-{cid[:8]}", "tok", user_id),
    )
    return cid


def _mk_account(
    conn,
    connection_id,
    *,
    acct_id=None,
    name="Chequing",
    mask="1234",
    acct_type="depository",
    subtype="checking",
):
    aid = acct_id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO bank_accounts "
        "(id, connection_id, plaid_account_id, name, mask, type, subtype) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (aid, connection_id, f"plaid-{aid[:8]}", name, mask, acct_type, subtype),
    )
    return aid


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "data.db"
    monkeypatch.setattr("server.paths.DB_PATH", db_path)
    init_db(db_path)
    conn = get_db(db_path)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# _deduplicate_accounts unit tests
# ---------------------------------------------------------------------------


class TestDeduplicateAccounts:
    def test_two_identical_accounts_one_marked_duplicate(self, db):
        """Two connections for the same user with the same account → one duplicate."""
        uid = _mk_user(db)
        cid_a = _mk_connection(db, uid)
        cid_b = _mk_connection(db, uid)
        # Use fixed UUIDs so we know which is lexicographically smaller
        aid_small = "00000000-0000-0000-0000-000000000001"
        aid_large = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        _mk_account(
            db,
            cid_a,
            acct_id=aid_small,
            mask="9999",
            name="Savings",
            acct_type="depository",
            subtype="savings",
        )
        _mk_account(
            db,
            cid_b,
            acct_id=aid_large,
            mask="9999",
            name="Savings",
            acct_type="depository",
            subtype="savings",
        )
        db.commit()

        _deduplicate_accounts(db, uid)
        db.commit()

        rows = {
            r["id"]: r
            for r in db.execute(
                "SELECT id, is_duplicate, primary_account_id FROM bank_accounts"
            ).fetchall()
        }
        assert rows[aid_small]["is_duplicate"] == 0
        assert rows[aid_small]["primary_account_id"] is None
        assert rows[aid_large]["is_duplicate"] == 1
        assert rows[aid_large]["primary_account_id"] == aid_small

    def test_no_duplicate_different_masks(self, db):
        """Accounts with different masks are not considered duplicates."""
        uid = _mk_user(db)
        cid_a = _mk_connection(db, uid)
        cid_b = _mk_connection(db, uid)
        aid_a = _mk_account(db, cid_a, mask="1111", name="Chequing")
        aid_b = _mk_account(db, cid_b, mask="2222", name="Chequing")
        db.commit()

        _deduplicate_accounts(db, uid)
        db.commit()

        rows = {
            r["id"]: r for r in db.execute("SELECT id, is_duplicate FROM bank_accounts").fetchall()
        }
        assert rows[aid_a]["is_duplicate"] == 0
        assert rows[aid_b]["is_duplicate"] == 0

    def test_null_mask_same_connection_not_deduplicated(self, db):
        """Accounts with NULL mask in the SAME connection are never flagged.

        Two sub-accounts at the same institution (same connection) that share a
        name but have no mask could be genuinely different — we leave them alone.
        """
        uid = _mk_user(db)
        cid = _mk_connection(db, uid)  # same connection
        aid_a = _mk_account(db, cid, mask=None, name="Mystery Account")  # type: ignore[arg-type]
        aid_b = _mk_account(db, cid, mask=None, name="Mystery Account")  # type: ignore[arg-type]
        db.execute("UPDATE bank_accounts SET mask = NULL")
        db.commit()

        _deduplicate_accounts(db, uid)
        db.commit()

        rows = {
            r["id"]: r for r in db.execute("SELECT id, is_duplicate FROM bank_accounts").fetchall()
        }
        assert rows[aid_a]["is_duplicate"] == 0
        assert rows[aid_b]["is_duplicate"] == 0

    def test_null_mask_cross_connection_is_deduplicated(self, db):
        """Accounts with NULL mask from DIFFERENT connections are deduplicated.

        This is the “same bank linked twice” case (e.g. RBC returning no mask).
        """
        uid = _mk_user(db)
        cid_a = _mk_connection(db, uid)
        cid_b = _mk_connection(db, uid)
        aid_small = "10000000-0000-0000-0000-000000000010"
        aid_large = "20000000-0000-0000-0000-000000000020"
        _mk_account(
            db,
            cid_a,
            acct_id=aid_small,
            mask=None,
            name="Day to Day",  # type: ignore[arg-type]
            acct_type="depository",
            subtype="checking",
        )
        _mk_account(
            db,
            cid_b,
            acct_id=aid_large,
            mask=None,
            name="Day to Day",  # type: ignore[arg-type]
            acct_type="depository",
            subtype="checking",
        )
        db.execute("UPDATE bank_accounts SET mask = NULL")
        db.commit()

        _deduplicate_accounts(db, uid)
        db.commit()

        rows = {
            r["id"]: r
            for r in db.execute(
                "SELECT id, is_duplicate, primary_account_id FROM bank_accounts"
            ).fetchall()
        }
        assert rows[aid_small]["is_duplicate"] == 0
        assert rows[aid_large]["is_duplicate"] == 1
        assert rows[aid_large]["primary_account_id"] == aid_small

    def test_user_isolation(self, db):
        """Accounts belonging to different users are never cross-deduplicated."""
        uid_a = _mk_user(db)
        uid_b = _mk_user(db)
        cid_a = _mk_connection(db, uid_a)
        cid_b = _mk_connection(db, uid_b)
        aid_a = _mk_account(db, cid_a, mask="7777", name="Chequing")
        aid_b = _mk_account(db, cid_b, mask="7777", name="Chequing")
        db.commit()

        # Run dedup for user A only
        _deduplicate_accounts(db, uid_a)
        db.commit()

        rows = {
            r["id"]: r for r in db.execute("SELECT id, is_duplicate FROM bank_accounts").fetchall()
        }
        # user A has only one account → not a duplicate
        assert rows[aid_a]["is_duplicate"] == 0
        # user B's account was not touched
        assert rows[aid_b]["is_duplicate"] == 0

    def test_idempotent(self, db):
        """Calling _deduplicate_accounts twice gives the same result."""
        uid = _mk_user(db)
        cid_a = _mk_connection(db, uid)
        cid_b = _mk_connection(db, uid)
        aid_small = "10000000-0000-0000-0000-000000000000"
        aid_large = "20000000-0000-0000-0000-000000000000"
        _mk_account(db, cid_a, acct_id=aid_small, mask="5555", name="Card")
        _mk_account(db, cid_b, acct_id=aid_large, mask="5555", name="Card")
        db.commit()

        _deduplicate_accounts(db, uid)
        db.commit()
        _deduplicate_accounts(db, uid)
        db.commit()

        rows = {
            r["id"]: r for r in db.execute("SELECT id, is_duplicate FROM bank_accounts").fetchall()
        }
        assert rows[aid_small]["is_duplicate"] == 0
        assert rows[aid_large]["is_duplicate"] == 1

    def test_three_duplicates_only_one_primary(self, db):
        """Three identical accounts → one primary, two duplicates."""
        uid = _mk_user(db)
        cids = [_mk_connection(db, uid) for _ in range(3)]
        aid_1 = "10000000-0000-0000-0000-000000000001"
        aid_2 = "20000000-0000-0000-0000-000000000002"
        aid_3 = "30000000-0000-0000-0000-000000000003"
        for aid, cid in zip([aid_1, aid_2, aid_3], cids):
            _mk_account(
                db,
                cid,
                acct_id=aid,
                mask="3333",
                name="Joint",
                acct_type="depository",
                subtype="checking",
            )
        db.commit()

        _deduplicate_accounts(db, uid)
        db.commit()

        rows = {
            r["id"]: r
            for r in db.execute(
                "SELECT id, is_duplicate, primary_account_id FROM bank_accounts"
            ).fetchall()
        }
        assert rows[aid_1]["is_duplicate"] == 0  # smallest → primary
        assert rows[aid_2]["is_duplicate"] == 1
        assert rows[aid_2]["primary_account_id"] == aid_1
        assert rows[aid_3]["is_duplicate"] == 1
        assert rows[aid_3]["primary_account_id"] == aid_1

    def test_no_user_id_no_crash(self, db):
        """Calling with empty user_id is a safe no-op."""
        # Should not raise
        _deduplicate_accounts(db, "")
        _deduplicate_accounts(db, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# sync() integration test — duplicate account transactions are skipped
# ---------------------------------------------------------------------------

from server.db import init_db as _init_db
from server.main import sync

_HEALTH_NOOP = {"checked": 0, "active": 0, "needs_reauth": 0, "pending_expiration": 0}


def _plaid_factory(sync_fn):
    class _Mock:
        def __init__(self, env=None, client_id=None, secret=None):
            import os

            self.env = (env or os.environ.get("PLAID_ENV", "sandbox")).lower()

        def sync_transactions(self, access_token, cursor=None):
            return sync_fn(access_token, cursor)

    return _Mock


class TestSyncSkipsDuplicates:
    """sync() must not insert transactions for is_duplicate=1 accounts."""

    @pytest.fixture()
    def env(self, tmp_path, monkeypatch):
        db_path = tmp_path / "data.db"
        lock = tmp_path / "sync.lock"
        monkeypatch.setattr("server.paths.DB_PATH", db_path)
        monkeypatch.setattr("server.paths.SYNC_LOCK_PATH", lock)
        _init_db(db_path)

        import time

        conn = get_db(db_path)
        uid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, 'u', 'x', ?)",
            (uid, int(time.time())),
        )

        # Two connections for the same institution / account (e.g. joint account
        # linked by both partners).
        cid_primary = str(uuid.uuid4())
        cid_dup = str(uuid.uuid4())
        for cid in (cid_primary, cid_dup):
            conn.execute(
                "INSERT INTO bank_connections "
                "(id, plaid_item_id, plaid_access_token_encrypted, status, user_id) "
                "VALUES (?, ?, 'enc', 'active', ?)",
                (cid, f"item-{cid[:6]}", uid),
            )

        # Pre-create bank_account rows with the same mask/name/type/subtype so
        # _deduplicate_accounts will flag the one from cid_dup as a duplicate.
        aid_primary = "10000000-0000-0000-0000-aaaaaaaaaaaa"
        aid_dup = "20000000-0000-0000-0000-bbbbbbbbbbbb"
        PLAID_ACCT_PRIMARY = "plaid-acct-primary"
        PLAID_ACCT_DUP = "plaid-acct-dup"
        conn.execute(
            "INSERT INTO bank_accounts (id, connection_id, plaid_account_id, name, mask, type, subtype) "
            "VALUES (?, ?, ?, 'Savings', '8888', 'depository', 'savings')",
            (aid_primary, cid_primary, PLAID_ACCT_PRIMARY),
        )
        conn.execute(
            "INSERT INTO bank_accounts (id, connection_id, plaid_account_id, name, mask, type, subtype) "
            "VALUES (?, ?, ?, 'Savings', '8888', 'depository', 'savings')",
            (aid_dup, cid_dup, PLAID_ACCT_DUP),
        )
        conn.commit()
        conn.close()

        return {
            "db": db_path,
            "uid": uid,
            "cid_primary": cid_primary,
            "cid_dup": cid_dup,
            "aid_primary": aid_primary,
            "aid_dup": aid_dup,
            "plaid_acct_primary": PLAID_ACCT_PRIMARY,
            "plaid_acct_dup": PLAID_ACCT_DUP,
        }

    def test_duplicate_account_transactions_not_inserted(self, env, monkeypatch):
        """Transactions from the duplicate account must not appear in the DB."""
        plaid_acct_primary = env["plaid_acct_primary"]
        plaid_acct_dup = env["plaid_acct_dup"]

        def _mock_sync(access_token, cursor=None):
            return {
                "added": [
                    {
                        "transaction_id": "txn-primary-1",
                        "account_id": plaid_acct_primary,
                        "date": "2025-01-01",
                        "name": "Coffee",
                        "merchant_name": "Starbucks",
                        "amount": 5.0,
                        "pending": False,
                        "accounts": [],
                    },
                    {
                        "transaction_id": "txn-dup-1",
                        "account_id": plaid_acct_dup,
                        "date": "2025-01-01",
                        "name": "Coffee",
                        "merchant_name": "Starbucks",
                        "amount": 5.0,
                        "pending": False,
                        "accounts": [],
                    },
                ],
                "modified": [],
                "removed": [],
                "next_cursor": "c2",
                "accounts": [
                    {
                        "account_id": plaid_acct_primary,
                        "name": "Savings",
                        "official_name": None,
                        "type": "depository",
                        "balances": {
                            "iso_currency_code": "CAD",
                            "current": 100.0,
                            "available": 90.0,
                        },
                    },
                    {
                        "account_id": plaid_acct_dup,
                        "name": "Savings",
                        "official_name": None,
                        "type": "depository",
                        "balances": {
                            "iso_currency_code": "CAD",
                            "current": 100.0,
                            "available": 90.0,
                        },
                    },
                ],
            }

        monkeypatch.setattr("server.main.PlaidProvider", _plaid_factory(_mock_sync))
        monkeypatch.setattr("server.crypto.decrypt", lambda x: x)
        monkeypatch.setattr(
            "server.health_monitor.check_all_connections",
            lambda db, plaid_provider=None: _HEALTH_NOOP,
        )

        result = sync()
        assert result["status"] == "ok"

        conn = get_db(env["db"])
        txns = conn.execute("SELECT plaid_transaction_id FROM transactions").fetchall()
        plaid_ids = {r[0] for r in txns}
        conn.close()

        # Primary account's transaction must be present
        assert "txn-primary-1" in plaid_ids
        # Duplicate account's transaction must be absent
        assert "txn-dup-1" not in plaid_ids


# ---------------------------------------------------------------------------
# _get_accounts_grouped UI helper test
# ---------------------------------------------------------------------------


class TestGetAccountsGrouped:
    """_get_accounts_grouped filters duplicates and reports hidden_count."""

    @pytest.fixture()
    def ui_env(self, tmp_path, monkeypatch):
        db_path = tmp_path / "data.db"
        monkeypatch.setenv("FRIDAY_BP_APP_DIR", str(tmp_path))
        _init_db(db_path)
        conn = get_db(db_path)
        import time

        uid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, 'u', 'x', ?)",
            (uid, int(time.time())),
        )
        cid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO bank_connections "
            "(id, plaid_item_id, plaid_access_token_encrypted, status, user_id, institution_name) "
            "VALUES (?, 'item', 'tok', 'active', ?, 'TestBank')",
            (cid, uid),
        )
        # Primary account
        aid_p = "10000000-aaaa-aaaa-aaaa-000000000001"
        # Duplicate account
        aid_d = "20000000-bbbb-bbbb-bbbb-000000000002"
        conn.execute(
            "INSERT INTO bank_accounts (id, connection_id, plaid_account_id, name, mask, type, subtype, is_duplicate) "
            "VALUES (?, ?, 'p-acct-1', 'Chequing', '1111', 'depository', 'checking', 0)",
            (aid_p, cid),
        )
        conn.execute(
            "INSERT INTO bank_accounts (id, connection_id, plaid_account_id, name, mask, type, subtype, is_duplicate) "
            "VALUES (?, ?, 'p-acct-2', 'Chequing', '1111', 'depository', 'checking', 1)",
            (aid_d, cid),
        )
        conn.commit()
        conn.close()
        return {"db": db_path, "uid": uid, "aid_p": aid_p, "aid_d": aid_d}

    def test_duplicate_hidden_and_counted(self, ui_env, monkeypatch):
        monkeypatch.setattr("ui.server._db_path", lambda: str(ui_env["db"]))

        # Import after patching
        from ui.server import _get_accounts_grouped

        grouped = _get_accounts_grouped(ui_env["uid"])
        assert "TestBank" in grouped
        data = grouped["TestBank"]
        # Only the non-duplicate should appear in accounts list
        ids_shown = [a["id"] for a in data["accounts"]]
        assert ui_env["aid_p"] in ids_shown
        assert ui_env["aid_d"] not in ids_shown
        # hidden_count should be 1
        assert data["hidden_count"] == 1
