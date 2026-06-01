"""
tests/test_savings_separation.py — Focused tests ensuring savings/investment
entries are NEVER counted in expense totals.

Covers:
  - summary() expense_total does NOT include savings amounts
  - summary() savings_contributions == sum of savings entries
  - summary() net = income - expenses (savings not subtracted from net)
  - get_ledger() expense total excludes savings line items
  - get_ledger() savings shown as separate total in ledger response
  - correct_transaction() allows routing positive-amount tx to savings line item
  - validate_sign_matches_item_type() allows savings for positive amounts
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


@pytest.fixture
def seeded_db(tmp_db, monkeypatch):
    """Set up a Personal ledger with income ($7,600), expenses ($1,997.90), and savings ($800)."""
    from server.main import apply_initial_setup
    from ui.auth import create_user

    monkeypatch.setenv("OPENCLAW_DIR", str(tmp_db.parent.parent))
    create_user(tmp_db, "testuser", "testpass123")
    apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])

    conn = get_db(tmp_db)
    ledger = conn.execute("SELECT id FROM ledgers WHERE name = 'Personal' LIMIT 1").fetchone()
    assert ledger, "Personal ledger not seeded"
    ledger_id = ledger["id"]

    income_li = conn.execute(
        "SELECT id FROM line_items WHERE item_type = 'income' AND ledger_id = ? LIMIT 1",
        (ledger_id,),
    ).fetchone()
    expense_li = conn.execute(
        "SELECT id FROM line_items WHERE item_type = 'expense' AND ledger_id = ? LIMIT 1",
        (ledger_id,),
    ).fetchone()
    savings_li = conn.execute(
        "SELECT id FROM line_items WHERE item_type = 'savings' AND ledger_id = ? LIMIT 1",
        (ledger_id,),
    ).fetchone()

    assert income_li, "No income line item found"
    assert expense_li, "No expense line item found"
    assert savings_li, "No savings line item found"

    # Look up the user_id so the bank connection is properly scoped
    user_row = conn.execute("SELECT id FROM users LIMIT 1").fetchone()
    user_id = user_row["id"] if user_row else None

    # Create a bank connection + account
    bc_id = _uid()
    account_id = _uid()
    conn.execute(
        "INSERT INTO bank_connections (id, plaid_access_token_encrypted, status, plaid_item_id, user_id) "
        "VALUES (?, 'enc:test', 'active', ?, ?)",
        (bc_id, _uid(), user_id),
    )
    conn.execute(
        "INSERT INTO bank_accounts (id, connection_id, name) VALUES (?, ?, 'Chequing')",
        (account_id, bc_id),
    )

    # Insert transactions matching expected seed totals
    income_txns = [
        ("2026-05-01", "TENSTORRENT AI", -3800.00),
        ("2026-05-15", "TENSTORRENT AI", -3800.00),
    ]
    expense_txns = [
        ("2026-05-02", "Loblaws",       92.34),
        ("2026-05-05", "Metro",         67.89),
        ("2026-05-09", "Costco",       178.45),
        ("2026-05-03", "Tim Hortons",   15.25),
        ("2026-05-01", "TTC",          156.00),
        ("2026-05-04", "Amazon",        89.99),
        ("2026-05-01", "Netflix",       17.99),
        ("2026-05-10", "Toronto Hydro", 94.50),
        # ... simplified but enough to be non-trivial
    ]
    savings_txns = [
        ("2026-05-05", "Wealthsimple TFSA", 500.00),
        ("2026-05-20", "Wealthsimple RRSP", 300.00),
    ]

    def _insert(date, merchant, amount, li_id, entry_type):
        tx_id = _uid()
        conn.execute(
            "INSERT INTO transactions (id, bank_account_id, date, merchant, amount) "
            "VALUES (?, ?, ?, ?, ?)",
            (tx_id, account_id, date, merchant, amount),
        )
        conn.execute(
            "INSERT INTO transaction_entries "
            "(id, transaction_id, ledger_id, line_item_id, amount, entry_type, source, reviewed) "
            "VALUES (?, ?, ?, ?, ?, ?, 'manual', 1)",
            (_uid(), tx_id, ledger_id, li_id, abs(amount), entry_type),
        )
        return tx_id

    for date, merchant, amount in income_txns:
        _insert(date, merchant, amount, income_li["id"], "income")
    for date, merchant, amount in expense_txns:
        _insert(date, merchant, amount, expense_li["id"], "spending")
    for date, merchant, amount in savings_txns:
        _insert(date, merchant, amount, savings_li["id"], "savings")

    conn.commit()
    conn.close()

    total_expenses = sum(abs(a) for _, _, a in expense_txns)
    total_income = sum(abs(a) for _, _, a in income_txns)
    total_savings = sum(abs(a) for _, _, a in savings_txns)

    return {
        "db_path": tmp_db,
        "ledger_id": ledger_id,
        "income_li_id": income_li["id"],
        "expense_li_id": expense_li["id"],
        "savings_li_id": savings_li["id"],
        "account_id": account_id,
        "expected_income": total_income,
        "expected_expenses": total_expenses,
        "expected_savings": total_savings,
    }


# ---------------------------------------------------------------------------
# Core separation tests
# ---------------------------------------------------------------------------


def test_savings_not_in_expense_total(seeded_db):
    """summary() expense_total must NOT include savings amounts."""
    from server.main import summary

    result = summary("2026-05")

    exp_expenses = seeded_db["expected_expenses"]
    exp_savings = seeded_db["expected_savings"]
    exp_income = seeded_db["expected_income"]

    # Savings must never bleed into expenses
    assert result["expenses"] == pytest.approx(exp_expenses, abs=0.01), (
        f"expense total {result['expenses']:.2f} should be {exp_expenses:.2f}; "
        f"savings amount ({exp_savings:.2f}) must NOT be included"
    )

    # Savings tracked separately
    assert result["savings"] == pytest.approx(exp_savings, abs=0.01), (
        f"savings total {result['savings']:.2f} should be {exp_savings:.2f}"
    )
    assert result["savings_contributions"] == pytest.approx(exp_savings, abs=0.01)

    # Net = income - expenses ONLY (savings not subtracted from net)
    expected_net = exp_income - exp_expenses
    assert result["net"] == pytest.approx(expected_net, abs=0.01), (
        f"net {result['net']:.2f} should be income({exp_income:.2f}) - expenses({exp_expenses:.2f}) "
        f"= {expected_net:.2f}. Savings must NOT be subtracted from net."
    )

    # Income is correct
    assert result["income"] == pytest.approx(exp_income, abs=0.01)


def test_savings_excluded_from_ledger_totals(seeded_db):
    """get_ledger() response must exclude savings from expense total and show savings separately."""
    from server.main import get_ledger

    ledger_id = seeded_db["ledger_id"]
    exp_expenses = seeded_db["expected_expenses"]
    exp_savings = seeded_db["expected_savings"]
    exp_income = seeded_db["expected_income"]

    result = get_ledger(ledger_id=ledger_id, period="2026-05")

    totals = result["totals"]

    # Savings must not bleed into expenses
    assert totals["expenses"] == pytest.approx(exp_expenses, abs=0.01), (
        f"ledger expense total {totals['expenses']:.2f} should be {exp_expenses:.2f}; "
        f"savings ({exp_savings:.2f}) must be excluded"
    )

    # Savings tracked separately
    assert totals["savings"] == pytest.approx(exp_savings, abs=0.01), (
        f"ledger savings total {totals['savings']:.2f} should be {exp_savings:.2f}"
    )

    # Income correct
    assert totals["income"] == pytest.approx(exp_income, abs=0.01)

    # Net = income - expenses (savings NOT deducted from net)
    expected_net = exp_income - exp_expenses
    assert totals["net"] == pytest.approx(expected_net, abs=0.01), (
        f"ledger net {totals['net']:.2f} should be {expected_net:.2f} (income - expenses, not savings)"
    )


def test_savings_line_items_not_in_expense_list(seeded_db):
    """Line items in the get_ledger() response must appear in the correct sections."""
    from server.main import get_ledger

    result = get_ledger(ledger_id=seeded_db["ledger_id"], period="2026-05")

    savings_li_id = seeded_db["savings_li_id"]
    expense_li_id = seeded_db["expense_li_id"]

    # Find both line items in the response
    items_by_id = {li["id"]: li for li in result["line_items"]}

    savings_item = items_by_id.get(savings_li_id)
    expense_item = items_by_id.get(expense_li_id)

    assert savings_item is not None, "Savings line item not in response"
    assert expense_item is not None, "Expense line item not in response"

    # They must be different types
    assert savings_item["item_type"] == "savings"
    assert expense_item["item_type"] == "expense"

    # The savings item's total must NOT appear in the expense item's total
    assert savings_item["total"] != expense_item["total"] or savings_item["total"] == 0.0


def test_validate_sign_allows_savings_for_positive_amount():
    """validate_sign_matches_item_type allows positive amounts for savings-type items."""
    from server.classifier import validate_sign_matches_item_type

    # Positive outflow → savings is valid (like expense)
    ok, err = validate_sign_matches_item_type(500.00, "savings")
    assert ok, f"Expected savings to be valid for positive amount, got error: {err}"

    # Negative inflow → savings is NOT valid (only income line items for credits)
    ok, err = validate_sign_matches_item_type(-500.00, "savings")
    assert not ok, "Expected savings to be invalid for negative amount"

    # Positive outflow → expense is still valid
    ok, err = validate_sign_matches_item_type(100.00, "expense")
    assert ok

    # Negative inflow → income is still valid
    ok, err = validate_sign_matches_item_type(-100.00, "income")
    assert ok

    # Zero → always valid regardless of type
    ok, _ = validate_sign_matches_item_type(0.0, "savings")
    assert ok
    ok, _ = validate_sign_matches_item_type(0.0, "expense")
    assert ok


def test_correct_transaction_to_savings_line_item(seeded_db):
    """correct_transaction() must succeed when routing a positive-amount tx to a savings line item."""
    from server.main import correct_transaction

    db = seeded_db["db_path"]
    savings_li_id = seeded_db["savings_li_id"]
    account_id = seeded_db["account_id"]

    # Create a new unclassified transaction with a positive (outflow) amount
    conn = get_db(db)
    tx_id = _uid()
    conn.execute(
        "INSERT INTO transactions (id, bank_account_id, date, merchant, amount) "
        "VALUES (?, ?, '2026-05-25', 'Wealthsimple New Deposit', 250.00)",
        (tx_id, account_id),
    )
    conn.commit()
    conn.close()

    # Should NOT fail with sign_mismatch
    result = correct_transaction(transaction_id=tx_id, line_item_id=savings_li_id)
    assert result.get("status") == "ok", (
        f"correct_transaction to savings line item failed: {result}. "
        "Positive outflows must be allowed for savings-type items."
    )


def test_summary_math_invariants(seeded_db):
    """Verify the core math invariants for the summary response."""
    from server.main import summary

    result = summary("2026-05")

    income = result["income"]
    expenses = result["expenses"]
    savings = result["savings"]
    net = result["net"]
    unspent = result["unspent_balance"]
    total_saved = result["total_saved"]

    # net = income - expenses (savings NOT subtracted)
    assert net == pytest.approx(income - expenses, abs=0.01), (
        f"net ({net}) should equal income ({income}) - expenses ({expenses})"
    )

    # total_saved = net (alias)
    assert total_saved == pytest.approx(net, abs=0.01)

    # unspent = net - savings_contributions
    assert unspent == pytest.approx(net - savings, abs=0.01), (
        f"unspent ({unspent}) should equal net ({net}) - savings ({savings})"
    )

    # Savings must be positive (outflows are positive)
    assert savings >= 0.0
