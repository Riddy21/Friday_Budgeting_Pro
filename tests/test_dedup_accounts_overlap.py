"""
tests/test_dedup_accounts_overlap.py — Pass 3 & Pass 4 dedup tests (#269 follow-up).

Covers same-connection, mask-less duplicate accounts that Passes 1-2 skip:

  * Pass 3 — stale-reissue: same (name, type, subtype) within one connection,
    one account's latest transaction is >=30 days older than the sibling's.
  * Pass 4 — name-independent overlap: same connection, DIFFERENT names, but
    the two accounts share identical (date, amount, merchant) transactions
    (the same account imported under two different names, e.g. RBC via SimpleFIN).

Also asserts genuinely-distinct same-connection accounts are NOT collapsed.
"""

from __future__ import annotations

import time
import uuid

import pytest

from server.db import get_db, init_db
from server.main import _deduplicate_accounts


def _mk_user(conn, user_id=None):
    uid = user_id or str(uuid.uuid4())
    conn.execute(
        "INSERT OR IGNORE INTO users (id, username, password_hash, created_at) "
        "VALUES (?, ?, ?, ?)",
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
    mask=None,
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


def _mk_txn(conn, account_id, *, date, amount, merchant, txn_id=None):
    tid = txn_id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO transactions "
        "(id, bank_account_id, plaid_transaction_id, date, merchant, amount) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tid, account_id, f"ptx-{tid[:8]}", date, merchant, amount),
    )
    return tid


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "data.db"
    monkeypatch.setattr("server.paths.DB_PATH", db_path)
    init_db(db_path)
    conn = get_db(db_path)
    yield conn
    conn.close()


def _dup(conn, aid):
    row = conn.execute(
        "SELECT is_duplicate, primary_account_id FROM bank_accounts WHERE id=?",
        (aid,),
    ).fetchone()
    return row["is_duplicate"], row["primary_account_id"]


class TestPass3StaleReissue:
    def test_stale_account_flagged_live_kept(self, db):
        uid = _mk_user(db)
        cid = _mk_connection(db, uid)
        # Same name, same connection, NO mask -> Passes 1-2 skip.
        stale = _mk_account(
            db,
            cid,
            acct_id="aaaa0000-0000-0000-0000-000000000001",
            name="Primary Chequing Account",
            mask=None,
        )
        live = _mk_account(
            db,
            cid,
            acct_id="bbbb0000-0000-0000-0000-000000000002",
            name="Primary Chequing Account",
            mask=None,
        )
        # stale: last txn 2026-07-31 ; live: last txn 2026-10-15 (>30d gap)
        _mk_txn(db, stale, date="2026-07-31", amount=30.95, merchant="Fee")
        _mk_txn(db, live, date="2026-10-01", amount=10.0, merchant="A")
        _mk_txn(db, live, date="2026-10-15", amount=20.0, merchant="B")
        db.commit()

        _deduplicate_accounts(db, uid)
        db.commit()

        assert _dup(db, stale) == (1, live)
        assert _dup(db, live) == (0, None)

    def test_both_active_same_name_not_flagged_without_overlap(self, db):
        """Two same-name accounts both recently active, no shared txns -> keep both."""
        uid = _mk_user(db)
        cid = _mk_connection(db, uid)
        a = _mk_account(
            db, cid, acct_id="aaaa1111-0000-0000-0000-000000000001", name="Savings", mask=None
        )
        b = _mk_account(
            db, cid, acct_id="bbbb1111-0000-0000-0000-000000000002", name="Savings", mask=None
        )
        _mk_txn(db, a, date="2026-10-10", amount=1.0, merchant="X")
        _mk_txn(db, b, date="2026-10-12", amount=2.0, merchant="Y")
        db.commit()

        _deduplicate_accounts(db, uid)
        db.commit()

        assert _dup(db, a) == (0, None)
        assert _dup(db, b) == (0, None)


class TestPass4NameIndependentOverlap:
    def test_different_names_same_txns_flagged(self, db):
        uid = _mk_user(db)
        cid = _mk_connection(db, uid)
        # Same account imported under two names (RBC/SimpleFIN case).
        friendly = _mk_account(
            db,
            cid,
            acct_id="cccc0000-0000-0000-0000-000000000001",
            name="RBC Day to Day Banking (7077)",
            mask=None,
        )
        raw = _mk_account(
            db,
            cid,
            acct_id="dddd0000-0000-0000-0000-000000000002",
            name="Chequing 018225247077 (7077)",
            mask=None,
        )
        for d, amt, m in [
            ("2026-10-15", -5139.89, "Payroll Deposit"),
            ("2026-10-15", 191.0, "Property Tax"),
            ("2026-10-15", 2400.0, "e-Transfer"),
        ]:
            _mk_txn(db, friendly, date=d, amount=amt, merchant=m)
            _mk_txn(db, raw, date=d, amount=amt, merchant=m)
        # friendly has extra unique txns -> more txns -> becomes primary.
        _mk_txn(db, friendly, date="2026-10-14", amount=5.0, merchant="Coffee")
        _mk_txn(db, friendly, date="2026-10-13", amount=6.0, merchant="Tea")
        db.commit()

        _deduplicate_accounts(db, uid)
        db.commit()

        assert _dup(db, raw) == (1, friendly)
        assert _dup(db, friendly) == (0, None)

    def test_distinct_accounts_no_overlap_not_flagged(self, db):
        uid = _mk_user(db)
        cid = _mk_connection(db, uid)
        chk = _mk_account(
            db, cid, acct_id="eeee0000-0000-0000-0000-000000000001", name="Chequing", mask=None
        )
        sav = _mk_account(
            db, cid, acct_id="ffff0000-0000-0000-0000-000000000002", name="Savings", mask=None
        )
        _mk_txn(db, chk, date="2026-10-10", amount=100.0, merchant="Grocery")
        _mk_txn(db, sav, date="2026-10-11", amount=200.0, merchant="Deposit")
        db.commit()

        _deduplicate_accounts(db, uid)
        db.commit()

        assert _dup(db, chk) == (0, None)
        assert _dup(db, sav) == (0, None)

    def test_cross_connection_still_collapsed_once(self, db):
        """Identical txns across DIFFERENT connections with the same name are
        collapsed by Pass 2 (cross-connection name match); exactly one survives."""
        uid = _mk_user(db)
        c1 = _mk_connection(db, uid)
        c2 = _mk_connection(db, uid)
        a = _mk_account(
            db, c1, acct_id="a1110000-0000-0000-0000-000000000001", name="Acct", mask=None
        )
        b = _mk_account(
            db, c2, acct_id="b2220000-0000-0000-0000-000000000002", name="Acct", mask=None
        )
        for d, amt, m in [("2026-10-01", 1.0, "X"), ("2026-10-02", 2.0, "Y")]:
            _mk_txn(db, a, date=d, amount=amt, merchant=m)
            _mk_txn(db, b, date=d, amount=amt, merchant=m)
        db.commit()

        _deduplicate_accounts(db, uid)
        db.commit()

        dups = [x for x in (a, b) if _dup(db, x)[0] == 1]
        assert len(dups) == 1
