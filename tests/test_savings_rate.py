"""
tests/test_savings_rate.py — Tests for savings rate tracking (#248).

Covers:
  - summary() returns savings_rate, savings_rate_ytd, savings_contributions,
    unspent_balance, total_saved
  - savings_rate = savings / income × 100
  - unspent_balance = income - expenses (can be negative)
  - total_saved = savings_contributions + unspent_balance
  - savings_trend(months) returns month-by-month breakdown with cumulative
  - Gracefully returns 0% when no income
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
    """DB with a user, ledger, and some savings/income/expense transactions."""
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

    # Create bank account
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

    # Insert transactions for 2026-05 (fixed month for deterministic tests)
    def add_tx(amount, li_id, tx_date="2026-05-15"):
        tx_id = _uid()
        conn.execute(
            "INSERT INTO transactions (id, date, merchant, amount, bank_account_id) "
            "VALUES (?, ?, 'Test', ?, ?)",
            (tx_id, tx_date, amount, account_id),
        )
        conn.execute(
            "INSERT INTO transaction_entries (id, transaction_id, ledger_id, line_item_id, amount) "
            "VALUES (?, ?, ?, ?, ?)",
            (_uid(), tx_id, ledger["id"], li_id, amount),
        )

    # income: 4000, expenses: 2500, savings: 500
    add_tx(4000.0, income_li["id"])
    add_tx(2500.0, expense_li["id"])
    add_tx(500.0, savings_li["id"])

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


def test_summary_includes_savings_rate(seeded_db):
    """summary() returns savings_rate as a percentage string."""
    from server.main import summary

    result = summary("2026-05")
    assert "savings_rate" in result, f"savings_rate missing from summary: {result.keys()}"
    assert result["savings_rate"] == "12.5%"  # 500/4000 = 12.5%


def test_summary_includes_savings_rate_ytd(seeded_db):
    """summary() returns savings_rate_ytd."""
    from server.main import summary

    result = summary("2026-05")
    assert "savings_rate_ytd" in result
    # YTD should be a percentage string
    assert result["savings_rate_ytd"].endswith("%")


def test_summary_includes_savings_contributions(seeded_db):
    """summary() returns savings_contributions field."""
    from server.main import summary

    result = summary("2026-05")
    assert "savings_contributions" in result
    assert result["savings_contributions"] == 500.0


def test_summary_unspent_balance(seeded_db):
    """unspent_balance = income - expenses (not including savings)."""
    from server.main import summary

    result = summary("2026-05")
    assert "unspent_balance" in result
    assert result["unspent_balance"] == 1500.0  # 4000 - 2500


def test_summary_total_saved(seeded_db):
    """total_saved = savings_contributions + unspent_balance."""
    from server.main import summary

    result = summary("2026-05")
    assert "total_saved" in result
    assert result["total_saved"] == 2000.0  # 500 + 1500


def test_summary_zero_savings_rate_with_no_income(tmp_path, monkeypatch):
    """summary() returns 0% savings_rate gracefully when no income."""
    db_path = tmp_path / "friday-bp" / "data.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server.paths, "DB_PATH", db_path)
    monkeypatch.setattr(server.paths, "APP_DIR", db_path.parent)
    init_db(db_path)

    from server.main import apply_initial_setup, summary
    from ui.auth import create_user

    monkeypatch.setenv("OPENCLAW_DIR", str(db_path.parent.parent))
    create_user(db_path, "testuser2", "testpass123")
    apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])

    result = summary("2026-01")
    assert result["savings_rate"] == "0.0%"
    assert result["savings_rate_ytd"] == "0.0%"


def test_savings_trend_returns_months(seeded_db):
    """savings_trend(12) returns a list of 12 month entries."""
    from server.main import savings_trend

    result = savings_trend(months=12)
    assert "months" in result
    assert len(result["months"]) == 12, f"Expected 12 months, got {len(result['months'])}"


def test_savings_trend_entry_structure(seeded_db):
    """Each savings_trend entry has the required fields."""
    from server.main import savings_trend

    result = savings_trend(months=1)
    entry = result["months"][0]
    required_fields = [
        "month", "income", "expenses", "savings_contributions",
        "unspent_balance", "total_saved", "savings_rate", "cumulative_saved",
    ]
    for field in required_fields:
        assert field in entry, f"Field '{field}' missing from trend entry: {entry.keys()}"


def test_savings_trend_cumulative(seeded_db):
    """savings_trend cumulative_saved accumulates across months."""
    from server.main import savings_trend

    result = savings_trend(months=3)
    months = result["months"]

    # Cumulative should be monotonically increasing (or at least >= previous)
    cumulative_values = [m["cumulative_saved"] for m in months]
    # Each entry's cumulative = prev cumulative + total_saved
    running = 0.0
    for entry in months:
        running = round(running + entry["total_saved"], 2)
        assert abs(entry["cumulative_saved"] - running) < 0.01, (
            f"Cumulative mismatch: {entry['cumulative_saved']} != {running}"
        )


def test_savings_trend_oldest_first(seeded_db):
    """savings_trend returns months in oldest-first order."""
    from server.main import savings_trend

    result = savings_trend(months=6)
    months_out = [m["month"] for m in result["months"]]
    assert months_out == sorted(months_out), "Expected oldest-first ordering"
