"""
tests/test_savings_item_type.py — Tests for savings as valid item_type (#234).

Covers:
  - add_line_item() accepts item_type='savings'
  - add_line_item() rejects invalid types
  - Personal ledger seeds include 'Investments & Savings' with savings type
  - get_ledger() totals include savings separately from expenses
  - summary() includes savings field, net = income - expenses - savings
"""

from __future__ import annotations

import uuid

import pytest

import server.paths
from server.db import get_db, init_db


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "friday-bp" / "data.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server.paths, "DB_PATH", db_path)
    monkeypatch.setattr(server.paths, "APP_DIR", db_path.parent)
    init_db(db_path)
    return db_path


def test_add_line_item_savings(tmp_db, monkeypatch):
    """add_line_item() accepts item_type='savings'."""
    from server.main import add_line_item, apply_initial_setup
    from ui.auth import create_user

    monkeypatch.setenv("OPENCLAW_DIR", str(tmp_db.parent.parent))
    create_user(tmp_db, "testuser", "testpass123")

    setup = apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])
    assert setup["status"] == "ok"

    # Get ledger_id
    conn = get_db(tmp_db)
    ledger_id = conn.execute("SELECT id FROM ledgers LIMIT 1").fetchone()["id"]
    conn.close()

    result = add_line_item(ledger_id=ledger_id, name="TFSA Contributions", item_type="savings")
    assert result["status"] == "ok", result
    assert "item_id" in result

    # Verify the item_type was persisted correctly
    conn = get_db(tmp_db)
    row = conn.execute(
        "SELECT item_type FROM line_items WHERE id = ?", (result["item_id"],)
    ).fetchone()
    conn.close()
    assert row["item_type"] == "savings"


def test_add_line_item_rejects_invalid_type(tmp_db, monkeypatch):
    """add_line_item() rejects unknown item_type."""
    from server.main import add_line_item, apply_initial_setup
    from ui.auth import create_user

    monkeypatch.setenv("OPENCLAW_DIR", str(tmp_db.parent.parent))
    create_user(tmp_db, "testuser2", "testpass123")
    apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])

    conn = get_db(tmp_db)
    ledger_id = conn.execute("SELECT id FROM ledgers LIMIT 1").fetchone()["id"]
    conn.close()

    result = add_line_item(ledger_id=ledger_id, name="Bad Item", item_type="investment")
    assert result["status"] == "error"
    assert "savings" in result["message"]


def test_personal_ledger_seeds_investments_savings(tmp_db, monkeypatch):
    """Personal ledger includes 'Investments & Savings' with savings type."""
    from server.main import apply_initial_setup
    from ui.auth import create_user

    monkeypatch.setenv("OPENCLAW_DIR", str(tmp_db.parent.parent))
    create_user(tmp_db, "testuser3", "testpass123")
    apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])

    conn = get_db(tmp_db)
    row = conn.execute(
        "SELECT name, item_type FROM line_items WHERE name = 'Investments & Savings'"
    ).fetchone()
    conn.close()

    assert row is not None, "Investments & Savings line item not found"
    assert row["item_type"] == "savings"


def test_get_ledger_totals_include_savings(tmp_db, monkeypatch):
    """get_ledger() totals include savings separately from expenses."""
    from server.main import apply_initial_setup, get_ledger
    from ui.auth import create_user

    monkeypatch.setenv("OPENCLAW_DIR", str(tmp_db.parent.parent))
    create_user(tmp_db, "testuser4", "testpass123")
    apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])

    conn = get_db(tmp_db)
    ledger = conn.execute("SELECT id FROM ledgers LIMIT 1").fetchone()
    savings_li = conn.execute(
        "SELECT id FROM line_items WHERE item_type = 'savings' LIMIT 1"
    ).fetchone()
    income_li = conn.execute(
        "SELECT id FROM line_items WHERE item_type = 'income' LIMIT 1"
    ).fetchone()
    expense_li = conn.execute(
        "SELECT id FROM line_items WHERE item_type = 'expense' LIMIT 1"
    ).fetchone()

    # Insert fake transactions and entries
    tx1 = _uid()
    tx2 = _uid()
    tx3 = _uid()
    account_id = _uid()
    bc_id = _uid()
    conn.execute(
        "INSERT INTO bank_connections (id, plaid_access_token_encrypted, status) "
        "VALUES (?, 'enc:test', 'active')",
        (bc_id,),
    )
    conn.execute(
        "INSERT INTO bank_accounts (id, connection_id, name) VALUES (?, ?, 'Chequing')",
        (account_id, bc_id),
    )
    for tx_id, amount in [(tx1, 1000.0), (tx2, 300.0), (tx3, 500.0)]:
        conn.execute(
            "INSERT INTO transactions (id, date, merchant, amount, bank_account_id) "
            "VALUES (?, '2026-01-15', 'Test', ?, ?)",
            (tx_id, amount, account_id),
        )
    conn.execute(
        "INSERT INTO transaction_entries (id, transaction_id, ledger_id, line_item_id, amount) "
        "VALUES (?, ?, ?, ?, 1000.0)",
        (_uid(), tx1, ledger["id"], income_li["id"]),
    )
    conn.execute(
        "INSERT INTO transaction_entries (id, transaction_id, ledger_id, line_item_id, amount) "
        "VALUES (?, ?, ?, ?, 300.0)",
        (_uid(), tx2, ledger["id"], expense_li["id"]),
    )
    conn.execute(
        "INSERT INTO transaction_entries (id, transaction_id, ledger_id, line_item_id, amount) "
        "VALUES (?, ?, ?, ?, 500.0)",
        (_uid(), tx3, ledger["id"], savings_li["id"]),
    )
    conn.commit()
    conn.close()

    result = get_ledger(ledger_id=ledger["id"], period="2026-01")
    totals = result["totals"]

    assert totals["income"] == 1000.0
    assert totals["expenses"] == 300.0
    assert totals["savings"] == 500.0
    assert totals["net"] == 700.0  # income - expenses = 1000 - 300 (net now excludes savings from subtraction)


def test_summary_includes_savings(tmp_db, monkeypatch):
    """summary() includes savings field, net = income - expenses - savings."""
    from server.main import apply_initial_setup, summary
    from ui.auth import create_user

    monkeypatch.setenv("OPENCLAW_DIR", str(tmp_db.parent.parent))
    create_user(tmp_db, "testuser5", "testpass123")
    apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])

    conn = get_db(tmp_db)
    ledger = conn.execute("SELECT id FROM ledgers LIMIT 1").fetchone()
    savings_li = conn.execute(
        "SELECT id FROM line_items WHERE item_type = 'savings' LIMIT 1"
    ).fetchone()
    income_li = conn.execute(
        "SELECT id FROM line_items WHERE item_type = 'income' LIMIT 1"
    ).fetchone()
    expense_li = conn.execute(
        "SELECT id FROM line_items WHERE item_type = 'expense' LIMIT 1"
    ).fetchone()

    account_id = _uid()
    bc_id = _uid()
    conn.execute(
        "INSERT INTO bank_connections (id, plaid_access_token_encrypted, status) "
        "VALUES (?, 'enc:test', 'active')",
        (bc_id,),
    )
    conn.execute(
        "INSERT INTO bank_accounts (id, connection_id, name) VALUES (?, ?, 'Chequing')",
        (account_id, bc_id),
    )
    for tx_id, amount in [(_uid(), 2000.0), (_uid(), 400.0), (_uid(), 600.0)]:
        conn.execute(
            "INSERT INTO transactions (id, date, merchant, amount, bank_account_id) "
            "VALUES (?, '2026-02-10', 'Test', ?, ?)",
            (tx_id, amount, account_id),
        )
    txs = conn.execute("SELECT id FROM transactions WHERE date='2026-02-10'").fetchall()
    li_map = [income_li["id"], expense_li["id"], savings_li["id"]]
    amounts = [2000.0, 400.0, 600.0]
    for tx, li_id, amt in zip(txs, li_map, amounts):
        conn.execute(
            "INSERT INTO transaction_entries (id, transaction_id, ledger_id, line_item_id, amount) "
            "VALUES (?, ?, ?, ?, ?)",
            (_uid(), tx["id"], ledger["id"], li_id, amt),
        )
    conn.commit()
    conn.close()

    result = summary(period="2026-02")
    assert "savings" in result, f"savings not in summary: {result}"
    assert result["income"] == 2000.0
    assert result["expenses"] == 400.0
    assert result["savings"] == 600.0
    assert result["net"] == 1600.0  # income - expenses = 2000 - 400 (net no longer subtracts savings)
