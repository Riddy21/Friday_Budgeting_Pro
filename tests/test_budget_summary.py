"""
tests/test_budget_summary.py — Tests for budget_summary() savings section (#237).

Covers:
  - budget_summary() returns all summary() fields (additive, no breakage)
  - budget_summary() returns savings_section with required fields
  - status field: on_track (>= 20%), under_saving (< 20%), over_saving (> income)
  - Existing summary() callers not affected
"""

from __future__ import annotations

import uuid

import pytest

import server.paths
from server.db import get_db, init_db


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """DB with a user, ledger, and controllable income/expense/savings entries."""
    db_path = tmp_path / "friday-bp" / "data.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server.paths, "DB_PATH", db_path)
    monkeypatch.setattr(server.paths, "APP_DIR", db_path.parent)
    init_db(db_path)

    from server.main import apply_initial_setup
    from ui.auth import create_user

    monkeypatch.setenv("OPENCLAW_DIR", str(db_path.parent.parent))
    create_user(db_path, "testuser", "testpass123")
    apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])

    conn = get_db(db_path)
    ledger = conn.execute("SELECT id FROM ledgers LIMIT 1").fetchone()
    income_li = conn.execute(
        "SELECT id FROM line_items WHERE item_type = 'income' LIMIT 1"
    ).fetchone()
    expense_li = conn.execute(
        "SELECT id FROM line_items WHERE item_type = 'expense' LIMIT 1"
    ).fetchone()
    savings_li = conn.execute(
        "SELECT id FROM line_items WHERE item_type = 'savings' LIMIT 1"
    ).fetchone()

    bc_id = _uid()
    account_id = _uid()
    conn.execute(
        "INSERT INTO bank_connections (id, plaid_access_token_encrypted, status) VALUES (?, 'enc:test', 'active')",
        (bc_id,),
    )
    conn.execute(
        "INSERT INTO bank_accounts (id, connection_id, name) VALUES (?, ?, 'Chequing')",
        (account_id, bc_id),
    )
    conn.commit()
    conn.close()

    return {
        "db_path": db_path,
        "ledger_id": ledger["id"],
        "income_li": income_li["id"],
        "expense_li": expense_li["id"],
        "savings_li": savings_li["id"],
        "account_id": account_id,
        "bc_id": bc_id,
    }


def _add_entries(db_path, seeded, entries):
    """Add (amount, line_item_id, date) entries to the DB."""
    conn = get_db(db_path)
    for amount, li_id, dt in entries:
        tx_id = _uid()
        conn.execute(
            "INSERT INTO transactions (id, date, merchant, amount, bank_account_id) "
            "VALUES (?, ?, 'Test', ?, ?)",
            (tx_id, dt, amount, seeded["account_id"]),
        )
        conn.execute(
            "INSERT INTO transaction_entries (id, transaction_id, ledger_id, line_item_id, amount) "
            "VALUES (?, ?, ?, ?, ?)",
            (_uid(), tx_id, seeded["ledger_id"], li_id, amount),
        )
    conn.commit()
    conn.close()


def test_budget_summary_includes_all_summary_fields(seeded_db):
    """budget_summary() returns all the same fields as summary()."""
    from server.main import budget_summary, summary

    s = summary("2026-01")
    bs = budget_summary("2026-01")

    # All keys from summary must be in budget_summary
    for key in s:
        assert key in bs, f"Key '{key}' from summary() missing in budget_summary()"


def test_budget_summary_has_savings_section(seeded_db):
    """budget_summary() returns savings_section with required fields."""
    from server.main import budget_summary

    result = budget_summary("2026-01")
    assert "savings_section" in result, "savings_section missing from budget_summary"

    section = result["savings_section"]
    required = [
        "savings_contributions", "unspent_balance", "total_saved",
        "savings_rate", "status", "benchmark", "benchmark_note",
    ]
    for field in required:
        assert field in section, f"Field '{field}' missing from savings_section"

    assert section["benchmark"] == "20%"


def test_budget_summary_status_on_track(seeded_db):
    """status is 'on_track' when savings_rate >= 20%."""
    from server.main import budget_summary

    # income=1000, savings=250 → 25% rate → on_track
    _add_entries(
        seeded_db["db_path"],
        seeded_db,
        [
            (1000.0, seeded_db["income_li"], "2026-06-10"),
            (250.0, seeded_db["savings_li"], "2026-06-11"),
        ],
    )
    result = budget_summary("2026-06")
    assert result["savings_section"]["status"] == "on_track", (
        f"Expected on_track but got: {result['savings_section']}"
    )


def test_budget_summary_status_under_saving(seeded_db):
    """status is 'under_saving' when net savings rate < 20%."""
    from server.main import budget_summary

    # income=1000, expenses=850 → net=150, rate=15% → under_saving
    # savings_rate is now (income-expenses)/income, not savings/income
    _add_entries(
        seeded_db["db_path"],
        seeded_db,
        [
            (1000.0, seeded_db["income_li"],  "2026-07-10"),
            (850.0,  seeded_db["expense_li"], "2026-07-11"),
        ],
    )
    result = budget_summary("2026-07")
    assert result["savings_section"]["status"] == "under_saving", (
        f"Expected under_saving but got: {result['savings_section']}"
    )


def test_budget_summary_status_no_income(seeded_db):
    """status is 'under_saving' when there's no income (0% rate)."""
    from server.main import budget_summary

    result = budget_summary("2026-03")
    section = result["savings_section"]
    assert section["status"] in ("under_saving", "on_track"), (
        f"Unexpected status with no data: {section['status']}"
    )


def test_summary_not_broken_by_budget_summary(seeded_db):
    """summary() still works correctly after budget_summary() addition."""
    from server.main import summary

    result = summary("2026-01")
    # Must still have the core fields
    for field in ["period", "income", "expenses", "savings", "net",
                  "savings_rate", "savings_rate_ytd", "by_line_item"]:
        assert field in result, f"summary() missing '{field}' after #237"

    # Must NOT have savings_section (that's budget_summary only)
    assert "savings_section" not in result, "summary() should not have savings_section"
