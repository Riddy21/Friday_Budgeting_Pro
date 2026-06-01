"""
tests/seed_test_data.py — Seed a sandbox SQLite DB with realistic test transactions.

Usage:
    # Fresh test DB (default: /tmp/friday-test-seed.db)
    python tests/seed_test_data.py

    # Custom path
    TEST_DB=/tmp/my-test.db python tests/seed_test_data.py

Expected totals for assertions (May 2026):
    income     = 7600.00   (2x bi-weekly Tenstorrent paycheques)
    expenses   = 1997.90   (groceries + dining + transport + shopping + subscriptions + utilities)
    savings    =  800.00   (Wealthsimple TFSA + RRSP transfers)
    net        = 5602.10   (income - expenses = 7600 - 1997.90)
    unspent    = 4802.10   (net - savings = 5602.10 - 800)
    net_rate   ~  73.7%    (net / income)

    transfers  = 1000.00   (internal chequing-to-chequing — excluded from totals)
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from datetime import date

# ── Allow running from project root ──────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = Path(os.environ.get("TEST_DB", "/tmp/friday-test-seed.db"))

# ── Expected totals (document these so tests can assert against them) ─────────
EXPECTED = {
    "income":    7600.00,
    "expenses":  1997.90,   # groceries(725.41) + dining(263.10) + transport(283.40)
                            # + shopping(437.53) + subscriptions(83.96) + utilities(204.50)
    "savings":    800.00,   # Wealthsimple TFSA(500) + RRSP(300)
    "net":       5602.10,   # income - expenses = 7600 - 1997.90
    "unspent":   4802.10,   # net - savings = 5602.10 - 800
    "transfers": 1000.00,   # internal chequing-to-chequing — excluded from totals
}

# Transactions to seed (sign convention: negative = money IN like Plaid)
TRANSACTIONS = [
    # ── Income: 2× bi-weekly Tenstorrent paycheques ──────────────────────────
    dict(date="2026-05-01", merchant="TENSTORRENT AI - Direct deposit",  amount=-3800.00, category="INCOME_WAGES",    line_item="salary"),
    dict(date="2026-05-15", merchant="TENSTORRENT AI - Direct deposit",  amount=-3800.00, category="INCOME_WAGES",    line_item="salary"),

    # ── Groceries ─────────────────────────────────────────────────────────────
    dict(date="2026-05-02", merchant="Loblaws",       amount=92.34,  category="GROCERIES", line_item="groceries"),
    dict(date="2026-05-05", merchant="Metro",          amount=67.89,  category="GROCERIES", line_item="groceries"),
    dict(date="2026-05-09", merchant="Costco",         amount=178.45, category="GROCERIES", line_item="groceries"),
    dict(date="2026-05-12", merchant="FreshCo",        amount=41.22,  category="GROCERIES", line_item="groceries"),
    dict(date="2026-05-16", merchant="Loblaws",        amount=55.67,  category="GROCERIES", line_item="groceries"),
    dict(date="2026-05-20", merchant="Metro",          amount=83.10,  category="GROCERIES", line_item="groceries"),
    dict(date="2026-05-23", merchant="No Frills",      amount=49.99,  category="GROCERIES", line_item="groceries"),
    dict(date="2026-05-28", merchant="Costco",         amount=156.75, category="GROCERIES", line_item="groceries"),

    # ── Dining ────────────────────────────────────────────────────────────────
    dict(date="2026-05-03", merchant="Banjara Indian Cuisine", amount=47.80, category="FOOD_AND_DRINK", line_item="dining"),
    dict(date="2026-05-07", merchant="Tim Hortons",            amount=15.25, category="FOOD_AND_DRINK", line_item="dining"),
    dict(date="2026-05-11", merchant="Mandarin Restaurant",    amount=85.40, category="FOOD_AND_DRINK", line_item="dining"),
    dict(date="2026-05-14", merchant="Harvey's",               amount=22.15, category="FOOD_AND_DRINK", line_item="dining"),
    dict(date="2026-05-19", merchant="Kinka Izakaya",          amount=73.60, category="FOOD_AND_DRINK", line_item="dining"),
    dict(date="2026-05-25", merchant="McDonald's",             amount=18.90, category="FOOD_AND_DRINK", line_item="dining"),

    # ── Transportation ────────────────────────────────────────────────────────
    dict(date="2026-05-01", merchant="TTC Monthly Pass",       amount=156.00, category="TRANSPORTATION", line_item="transport"),
    dict(date="2026-05-06", merchant="Uber",                   amount=22.40,  category="TRANSPORTATION", line_item="transport"),
    dict(date="2026-05-13", merchant="Shell Gas Station",      amount=80.00,  category="TRANSPORTATION", line_item="transport"),
    dict(date="2026-05-22", merchant="Green P Parking",        amount=25.00,  category="TRANSPORTATION", line_item="transport"),

    # ── Shopping ──────────────────────────────────────────────────────────────
    dict(date="2026-05-04", merchant="Amazon",        amount=89.99,  category="SHOPPING", line_item="shopping"),
    dict(date="2026-05-08", merchant="Best Buy",      amount=199.99, category="SHOPPING", line_item="shopping"),
    dict(date="2026-05-17", merchant="H&M",           amount=65.50,  category="SHOPPING", line_item="shopping"),
    dict(date="2026-05-21", merchant="Canadian Tire", amount=47.30,  category="SHOPPING", line_item="shopping"),
    dict(date="2026-05-27", merchant="IKEA",          amount=34.75,  category="SHOPPING", line_item="shopping"),

    # ── Subscriptions ─────────────────────────────────────────────────────────
    dict(date="2026-05-01", merchant="Netflix",        amount=17.99, category="SUBSCRIPTION", line_item="subscriptions"),
    dict(date="2026-05-01", merchant="Spotify",        amount=11.99, category="SUBSCRIPTION", line_item="subscriptions"),
    dict(date="2026-05-01", merchant="Apple iCloud+",  amount=3.99,  category="SUBSCRIPTION", line_item="subscriptions"),
    dict(date="2026-05-03", merchant="GoodLife Fitness", amount=49.99, category="SUBSCRIPTION", line_item="subscriptions"),

    # ── Utilities ─────────────────────────────────────────────────────────────
    dict(date="2026-05-10", merchant="Toronto Hydro",     amount=94.50, category="UTILITIES", line_item="utilities"),
    dict(date="2026-05-10", merchant="Rogers Internet",   amount=65.00, category="UTILITIES", line_item="utilities"),
    dict(date="2026-05-10", merchant="Fizz Mobile",       amount=45.00, category="UTILITIES", line_item="utilities"),

    # ── Savings/Investments ───────────────────────────────────────────────────
    dict(date="2026-05-05", merchant="Wealthsimple TFSA",  amount=500.00, category="TRANSFER_OUT", line_item="savings"),
    dict(date="2026-05-20", merchant="Wealthsimple RRSP",  amount=300.00, category="TRANSFER_OUT", line_item="savings"),

    # ── Internal transfer (excluded from totals) ──────────────────────────────
    dict(date="2026-05-15", merchant="Transfer to Savings Account", amount=1000.00, category="TRANSFER_OUT", line_item="transfer"),
]


def _uid() -> str:
    return str(uuid.uuid4())


def seed(db_path: Path) -> None:
    """Create a fresh DB at *db_path* and populate it with test data."""
    import importlib
    import os

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
        print(f"Removed existing DB at {db_path}")

    # Monkey-patch paths before importing server modules
    os.environ["FRIDAY_BP_APP_DIR"] = str(db_path.parent)
    os.environ["OPENCLAW_DIR"] = str(db_path.parent.parent)

    # Force reimport with correct paths
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("server") or mod_name.startswith("ui"):
            del sys.modules[mod_name]

    import server.paths
    server.paths.APP_DIR = db_path.parent
    server.paths.DB_PATH = db_path

    from server.db import get_db, init_db
    from ui.auth import create_user

    init_db(db_path)
    create_user(db_path, "testuser", "testpass123")

    from server.main import apply_initial_setup, get_active_user_id
    apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])

    conn = get_db(db_path)

    # Look up Personal ledger and its line items
    personal_ledger = conn.execute(
        "SELECT id FROM ledgers WHERE name = 'Personal' LIMIT 1"
    ).fetchone()
    assert personal_ledger, "Personal ledger not found — apply_initial_setup failed?"
    ledger_id = personal_ledger["id"]

    # Map friendly line_item keys → actual line_item IDs
    line_items = conn.execute(
        "SELECT id, name, item_type FROM line_items WHERE ledger_id = ?",
        (ledger_id,),
    ).fetchall()
    li_by_name = {row["name"].lower(): row for row in line_items}
    li_by_type = {}
    for row in line_items:
        li_by_type.setdefault(row["item_type"], []).append(row)

    def resolve_line_item(key: str):
        """Map our seed key to a real line_item id."""
        # Try exact match first, then fallback to alternatives
        # Default Personal ledger has: Salary, Groceries, Dining, Transport,
        # Subscriptions, Healthcare, Travel, Shopping, Misc, Other
        fallbacks = {
            "salary": ["salary", "salary — ridvan (tenstorrent)"],
            "groceries": ["groceries"],
            "dining": ["dining"],
            "transport": ["transport"],
            "shopping": ["shopping"],
            "subscriptions": ["subscriptions", "misc / incidents", "misc", "other"],
            "utilities": ["utilities", "home & maintenance", "misc", "other"],
            "savings": [],   # handled separately
            "transfer": [],  # handled separately
        }
        candidates = fallbacks.get(key, [key])
        for name_key in candidates:
            row = li_by_name.get(name_key)
            if row:
                return row["id"]
        return None

    # Resolve savings line item
    savings_li = conn.execute(
        "SELECT id FROM line_items WHERE item_type = 'savings' AND ledger_id = ? LIMIT 1",
        (ledger_id,),
    ).fetchone()

    # Create a dummy bank connection + account
    bc_id = _uid()
    account_id = _uid()
    conn.execute(
        "INSERT INTO bank_connections (id, plaid_access_token_encrypted, status, plaid_item_id) "
        "VALUES (?, 'enc:test', 'active', ?)",
        (bc_id, _uid()),
    )
    conn.execute(
        "INSERT INTO bank_accounts (id, connection_id, name, type, subtype, currency) "
        "VALUES (?, ?, 'RBC Day to Day Chequing', 'depository', 'checking', 'CAD')",
        (account_id, bc_id),
    )
    conn.execute(
        "UPDATE bank_accounts SET default_ledger_id = ? WHERE id = ?",
        (ledger_id, account_id),
    )
    conn.commit()

    # Insert transactions + entries
    inserted = 0
    skipped_transfers = 0
    for txn in TRANSACTIONS:
        txn_id = _uid()
        plaid_id = f"sandbox-{txn_id[:8]}"
        conn.execute(
            "INSERT INTO transactions "
            "(id, bank_account_id, plaid_transaction_id, date, merchant, amount, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, 'CAD')",
            (txn_id, account_id, plaid_id, txn["date"], txn["merchant"], txn["amount"]),
        )

        key = txn["line_item"]
        if key == "transfer":
            # Insert a skip/transfer entry so it's classified but excluded from totals
            conn.execute(
                "INSERT INTO transaction_entries "
                "(id, transaction_id, ledger_id, line_item_id, amount, entry_type, source, confidence, reviewed) "
                "VALUES (?, ?, ?, NULL, ?, 'transfer', 'manual', 1.0, 1)",
                (_uid(), txn_id, ledger_id, abs(txn["amount"])),
            )
            skipped_transfers += 1
        elif key == "savings":
            assert savings_li, "No savings line item found"
            conn.execute(
                "INSERT INTO transaction_entries "
                "(id, transaction_id, ledger_id, line_item_id, amount, entry_type, source, confidence, reviewed) "
                "VALUES (?, ?, ?, ?, ?, 'savings', 'manual', 1.0, 1)",
                (_uid(), txn_id, ledger_id, savings_li["id"], abs(txn["amount"])),
            )
            inserted += 1
        elif key == "salary":
            li_id = resolve_line_item(key)
            conn.execute(
                "INSERT INTO transaction_entries "
                "(id, transaction_id, ledger_id, line_item_id, amount, entry_type, source, confidence, reviewed) "
                "VALUES (?, ?, ?, ?, ?, 'income', 'manual', 1.0, 1)",
                (_uid(), txn_id, ledger_id, li_id, abs(txn["amount"])),
            )
            inserted += 1
        else:
            li_id = resolve_line_item(key)
            if not li_id:
                print(f"WARNING: no line item for key={key!r}, skipping entry for {txn['merchant']!r}")
                continue
            conn.execute(
                "INSERT INTO transaction_entries "
                "(id, transaction_id, ledger_id, line_item_id, amount, entry_type, source, confidence, reviewed) "
                "VALUES (?, ?, ?, ?, ?, 'spending', 'manual', 1.0, 1)",
                (_uid(), txn_id, ledger_id, li_id, abs(txn["amount"])),
            )
            inserted += 1

    conn.commit()
    conn.close()

    print(f"\n✅ Seeded {db_path}")
    print(f"   Transactions: {len(TRANSACTIONS)} ({inserted} with entries, {skipped_transfers} transfers)")
    print(f"\n   Expected totals for 2026-05:")
    print(f"     income   = ${EXPECTED['income']:,.2f}")
    print(f"     expenses = ${EXPECTED['expenses']:,.2f}")
    print(f"     savings  = ${EXPECTED['savings']:,.2f}")
    print(f"     net      = ${EXPECTED['net']:,.2f}  (income - expenses)")
    print(f"     unspent  = ${EXPECTED['unspent']:,.2f} (net - savings)")
    print(f"     transfers= ${EXPECTED['transfers']:,.2f} (excluded)")

    # Verify via summary()
    _verify(db_path)


def _verify(db_path: Path) -> None:
    """Run summary() and print actual vs expected totals."""
    import server.paths
    server.paths.DB_PATH = db_path

    from server.main import summary
    try:
        result = summary("2026-05")
        print("\n   Actual totals from summary():")
        print(f"     income   = ${result['income']:,.2f}")
        print(f"     expenses = ${result['expenses']:,.2f}")
        print(f"     savings  = ${result['savings']:,.2f}")
        print(f"     net      = ${result['net']:,.2f}   (income - expenses)")
        print(f"     unspent  = ${result['unspent_balance']:,.2f}")

        # Validate math
        assert abs(result["net"] - (result["income"] - result["expenses"])) < 0.01, \
            f"Net mismatch: {result['net']} != income({result['income']}) - expenses({result['expenses']})"
        assert abs(result["total_saved"] - result["net"]) < 0.01, \
            f"total_saved mismatch: {result['total_saved']} != net({result['net']})"
        assert abs(result["unspent_balance"] - (result["net"] - result["savings"])) < 0.01, \
            f"unspent mismatch: {result['unspent_balance']} != net({result['net']}) - savings({result['savings']})"
        print("\n   ✅ All math checks passed!")
    except Exception as e:
        print(f"\n   ❌ Verification failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    seed(DB_PATH)
