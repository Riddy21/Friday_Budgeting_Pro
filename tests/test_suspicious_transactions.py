"""Tests for suspicious transaction detection (#273)."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

from server.db import get_db, init_db
from server.main import _detect_suspicious_transactions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> tuple[Path, sqlite3.Connection]:
    tmp = tempfile.mktemp(suffix=".db")
    p = Path(tmp)
    init_db(p)
    conn = get_db(p)
    return p, conn


def _insert_user(conn) -> str:
    uid = str(uuid.uuid4())
    from argon2 import PasswordHasher

    ph = PasswordHasher()
    conn.execute(
        "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (uid, "testuser", ph.hash("password123"), int(time.time())),
    )
    conn.commit()
    return uid


def _insert_connection(conn, user_id: str) -> str:
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO bank_connections (id, user_id, plaid_access_token_encrypted, status, plaid_env) "
        "VALUES (?, ?, ?, 'active', 'sandbox')",
        (cid, user_id, "enc_token"),
    )
    conn.commit()
    return cid


def _insert_account(conn, connection_id: str, name: str = "Chequing") -> str:
    aid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO bank_accounts (id, connection_id, plaid_account_id, name) VALUES (?, ?, ?, ?)",
        (aid, connection_id, str(uuid.uuid4()), name),
    )
    conn.commit()
    return aid


def _insert_txn(
    conn,
    account_id: str,
    merchant: str,
    amount: float,
    date: str = "2024-06-01",
    authorized_datetime: str | None = None,
    pending: int = 0,
) -> str:
    tid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO transactions (id, bank_account_id, plaid_transaction_id, date, "
        "authorized_datetime, merchant, amount, currency, pending) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'CAD', ?)",
        (tid, account_id, str(uuid.uuid4()), date, authorized_datetime, merchant, amount, pending),
    )
    conn.commit()
    return tid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_duplicate_charge_detected():
    """Same merchant, same amount, same account within 24h → risk: high."""
    _db, conn = _make_db()
    uid = _insert_user(conn)
    cid = _insert_connection(conn, uid)
    aid = _insert_account(conn, cid)

    # Two identical charges close together (same day)
    _insert_txn(
        conn, aid, "Netflix", 15.99, date="2024-06-01", authorized_datetime="2024-06-01T10:00:00"
    )
    _insert_txn(
        conn, aid, "Netflix", 15.99, date="2024-06-01", authorized_datetime="2024-06-01T11:00:00"
    )

    flags = _detect_suspicious_transactions(conn, uid)
    conn.close()

    merchants = [f["merchant"] for f in flags]
    reasons = [f["reason"] for f in flags]
    assert any("Netflix" in (m or "") for m in merchants), f"Expected Netflix in flags: {flags}"
    assert any("duplicate" in r.lower() for r in reasons), f"Expected duplicate reason: {reasons}"
    assert any(f["risk_level"] == "high" for f in flags), f"Expected high risk: {flags}"


def test_unusually_large_charge_detected():
    """Amount > 3× merchant average (with ≥3 prior txns) → risk: medium."""
    _db, conn = _make_db()
    uid = _insert_user(conn)
    cid = _insert_connection(conn, uid)
    aid = _insert_account(conn, cid)

    # 3 prior ~$10 charges, then one $500 charge
    for i in range(3):
        _insert_txn(conn, aid, "Starbucks", 10.00, date=f"2024-05-{i+1:02d}")
    _insert_txn(conn, aid, "Starbucks", 500.00, date="2024-06-01")

    flags = _detect_suspicious_transactions(conn, uid)
    conn.close()

    merchants = [f["merchant"] for f in flags]
    reasons = [f["reason"] for f in flags]
    assert any("Starbucks" in (m or "") for m in merchants), f"Expected Starbucks in flags: {flags}"
    assert any(
        "large" in r.lower() or "unusually" in r.lower() for r in reasons
    ), f"Expected unusually large reason: {reasons}"
    assert any(f["risk_level"] == "medium" for f in flags), f"Expected medium risk: {flags}"


def test_new_merchant_large_amount_detected():
    """First-ever merchant + amount > $200 → risk: low."""
    _db, conn = _make_db()
    uid = _insert_user(conn)
    cid = _insert_connection(conn, uid)
    aid = _insert_account(conn, cid)

    _insert_txn(conn, aid, "AcmeCorp Unknown Store", 350.00, date="2024-06-01")

    flags = _detect_suspicious_transactions(conn, uid)
    conn.close()

    merchants = [f["merchant"] for f in flags]
    reasons = [f["reason"] for f in flags]
    assert any("AcmeCorp" in (m or "") for m in merchants), f"Expected AcmeCorp in flags: {flags}"
    assert any(
        "new merchant" in r.lower() or "first" in r.lower() for r in reasons
    ), f"Expected new-merchant reason: {reasons}"
    assert any(f["risk_level"] == "low" for f in flags), f"Expected low risk: {flags}"


def test_card_testing_detected():
    """3+ micro-charges (< $5) from same merchant within 1 h → risk: high."""
    _db, conn = _make_db()
    uid = _insert_user(conn)
    cid = _insert_connection(conn, uid)
    aid = _insert_account(conn, cid)

    # Four $1.00 charges within 30 minutes
    base = "2024-06-01T12:00:00"
    for i in range(4):
        ts = f"2024-06-01T12:{i * 5:02d}:00"
        _insert_txn(conn, aid, "ShadyMerchant", 1.00, date="2024-06-01", authorized_datetime=ts)

    flags = _detect_suspicious_transactions(conn, uid)
    conn.close()

    merchants = [f["merchant"] for f in flags]
    reasons = [f["reason"] for f in flags]
    assert any(
        "ShadyMerchant" in (m or "") for m in merchants
    ), f"Expected ShadyMerchant in flags: {flags}"
    assert any(
        "card" in r.lower() or "micro" in r.lower() for r in reasons
    ), f"Expected card-testing reason: {reasons}"
    assert any(f["risk_level"] == "high" for f in flags), f"Expected high risk: {flags}"


def test_dismissed_transaction_not_reflagged():
    """A previously dismissed transaction must not be re-flagged."""
    _db, conn = _make_db()
    uid = _insert_user(conn)
    cid = _insert_connection(conn, uid)
    aid = _insert_account(conn, cid)

    tid1 = _insert_txn(
        conn, aid, "Netflix", 15.99, date="2024-06-01", authorized_datetime="2024-06-01T10:00:00"
    )
    tid2 = _insert_txn(
        conn, aid, "Netflix", 15.99, date="2024-06-01", authorized_datetime="2024-06-01T11:00:00"
    )

    # Pre-insert a dismissed flag for tid1
    conn.execute(
        "INSERT INTO suspicious_transactions (id, transaction_id, reason, risk_level, dismissed) "
        "VALUES (?, ?, ?, ?, 1)",
        (str(uuid.uuid4()), tid1, "Possible duplicate", "high"),
    )
    conn.commit()

    flags = _detect_suspicious_transactions(conn, uid)
    conn.close()

    # tid1 should NOT appear again in new_flags
    new_tx_ids = [f["transaction_id"] for f in flags]
    assert tid1 not in new_tx_ids, f"Dismissed transaction {tid1} was re-flagged"


def test_whitelisted_merchants_not_flagged():
    """Payroll, rent, and mortgage merchants must not be flagged."""
    _db, conn = _make_db()
    uid = _insert_user(conn)
    cid = _insert_connection(conn, uid)
    aid = _insert_account(conn, cid)

    # Payroll — two identical charges (would normally be duplicate)
    _insert_txn(
        conn,
        aid,
        "ADP Payroll",
        3000.00,
        date="2024-06-01",
        authorized_datetime="2024-06-01T09:00:00",
    )
    _insert_txn(
        conn,
        aid,
        "ADP Payroll",
        3000.00,
        date="2024-06-01",
        authorized_datetime="2024-06-01T09:05:00",
    )

    # Rent — new merchant large amount
    _insert_txn(conn, aid, "Rent Payment", 1800.00, date="2024-06-01")

    # Mortgage
    _insert_txn(conn, aid, "Mortgage Payment", 2200.00, date="2024-06-01")

    flags = _detect_suspicious_transactions(conn, uid)
    conn.close()

    merchants = [f["merchant"] for f in flags]
    assert not any(
        "ADP" in (m or "") for m in merchants
    ), f"ADP Payroll should not be flagged: {flags}"
    assert not any(
        "Rent" in (m or "") for m in merchants
    ), f"Rent Payment should not be flagged: {flags}"
    assert not any(
        "Mortgage" in (m or "") for m in merchants
    ), f"Mortgage Payment should not be flagged: {flags}"
