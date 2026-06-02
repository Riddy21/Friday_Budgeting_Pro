#!/usr/bin/env python3
"""
Standalone visual check tool that:
  1. Spins up a fresh test server on a free port with isolated DB
  2. Completes the setup wizard (creates testuser/testpass123)
  3. Seeds some test data (a connection, accounts, transactions, ledgers)
  4. Logs in via Playwright, navigates to each page at 3 viewport sizes
  5. Saves screenshots to /tmp/ui-check/<page>_<viewport>.png

Usage:
  python3 scripts/visual_check.py [--dark]
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = Path("/tmp/ui-check")
OUT_DIR.mkdir(exist_ok=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post(base_url: str, path: str, data: dict) -> int:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        base_url + path,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def _wait_for_server(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"Server never became ready: {url}")


def seed_data(db_path: Path) -> None:
    """Seed some realistic-looking ledgers/accounts/transactions for screenshots."""
    import sqlite3
    import uuid
    import time as _t

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # Get the user id (testuser)
    cur.execute("SELECT id FROM users WHERE username='testuser'")
    row = cur.fetchone()
    if not row:
        conn.close()
        return
    user_id = row[0]
    now = int(_t.time())

    # Add a fake bank connection (so /accounts shows something)
    conn_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO bank_connections (id, user_id, institution_name, status, "
        "plaid_access_token_encrypted, plaid_item_id, last_synced_at, plaid_env) "
        "VALUES (?, ?, ?, 'active', '', ?, ?, 'sandbox')",
        (conn_id, user_id, "Sandbox Bank", "fake_item_" + conn_id[:8], now),
    )

    # Add bank accounts (one chequing, one savings, one credit, one investment)
    acct_ids = []
    for name, subtype, balance, mask in [
        ("Everyday Chequing", "checking", 2483.91, "1234"),
        ("High-Interest Savings", "savings", 12500.00, "9988"),
        ("Visa Rewards Card", "credit card", 423.55, "5678"),
        ("TFSA Investment", "tfsa", 45230.18, "4321"),
    ]:
        aid = str(uuid.uuid4())
        acct_ids.append(aid)
        if "credit" in subtype:
            acct_type = "credit"
        elif subtype in ("tfsa", "rrsp", "brokerage", "401k", "403b", "ira", "roth"):
            acct_type = "investment"
        else:
            acct_type = "depository"
        cur.execute(
            "INSERT INTO bank_accounts (id, connection_id, plaid_account_id, name, "
            "type, subtype, mask, balance_current, balance_available, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'CAD')",
            (aid, conn_id, f"plaid_{aid[:8]}", name,
             acct_type, subtype, mask, balance, balance),
        )

    # Find ledgers (the setup wizard may have created one — Personal)
    # If none, create one.
    cur.execute("SELECT id FROM ledgers WHERE user_id=?", (user_id,))
    ledger_rows = cur.fetchall()
    cur.execute("PRAGMA table_info(ledgers)")
    ledger_cols = {r[1] for r in cur.fetchall()}
    if not ledger_rows:
        ledger_id = str(uuid.uuid4())
        if "created_at" in ledger_cols:
            cur.execute(
                "INSERT INTO ledgers (id, user_id, name, created_at) VALUES (?, ?, 'Personal', ?)",
                (ledger_id, user_id, now),
            )
        else:
            cur.execute(
                "INSERT INTO ledgers (id, user_id, name) VALUES (?, ?, 'Personal')",
                (ledger_id, user_id),
            )
    else:
        ledger_id = ledger_rows[0][0]

    # Add some line items
    items = [
        ("Salary", "income"),
        ("Groceries", "expense"),
        ("Dining out", "expense"),
        ("Subscriptions", "expense"),
        ("Transport", "expense"),
        ("TFSA contributions", "savings"),
    ]
    # Inspect line_items schema once to be tolerant
    cur.execute("PRAGMA table_info(line_items)")
    li_cols = {r[1] for r in cur.fetchall()}
    item_ids = {}
    for name, item_type in items:
        cur.execute("SELECT id FROM line_items WHERE ledger_id=? AND name=?", (ledger_id, name))
        existing = cur.fetchone()
        if existing:
            item_ids[name] = existing[0]
            continue
        iid = str(uuid.uuid4())
        if "created_at" in li_cols:
            cur.execute(
                "INSERT INTO line_items (id, ledger_id, name, item_type, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (iid, ledger_id, name, item_type, now),
            )
        else:
            cur.execute(
                "INSERT INTO line_items (id, ledger_id, name, item_type) "
                "VALUES (?, ?, ?, ?)",
                (iid, ledger_id, name, item_type),
            )
        item_ids[name] = iid

    # Add some transactions
    import datetime as _dt
    today = _dt.date.today()
    # Plaid convention: expenses are POSITIVE, income is NEGATIVE.
    # Days_ago kept small so transactions fall within the current month
    # for the dashboard top-spending widget (which filters by month).
    txns = [
        ("Loblaws", 87.43, 0, item_ids.get("Groceries")),
        ("Tim Hortons", 8.20, 0, item_ids.get("Dining out")),
        ("Netflix", 16.99, 0, item_ids.get("Subscriptions")),
        ("Uber", 23.50, 0, item_ids.get("Transport")),
        ("ACME Corp Payroll", -3200.00, 1, item_ids.get("Salary")),
        ("Wealthsimple Transfer", 500.00, 0, item_ids.get("TFSA contributions")),
        ("Sobeys", 52.10, 0, item_ids.get("Groceries")),
        ("Spotify", 10.99, 0, item_ids.get("Subscriptions")),
    ]
    for merchant, amount, days_ago, li_id in txns:
        tx_id = str(uuid.uuid4())
        date = (today - _dt.timedelta(days=days_ago)).isoformat()
        cur.execute(
            "INSERT INTO transactions (id, bank_account_id, plaid_transaction_id, "
            "merchant, amount, date, currency, pending) "
            "VALUES (?, ?, ?, ?, ?, ?, 'CAD', 0)",
            (tx_id, acct_ids[0], f"plaid_tx_{tx_id[:8]}", merchant, amount, date),
        )
        if li_id:
            entry_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO transaction_entries (id, transaction_id, ledger_id, line_item_id, "
                "amount, entry_type, source, reviewed, uncertain) "
                "VALUES (?, ?, ?, ?, ?, ?, 'manual', 1, 0)",
                (entry_id, tx_id, ledger_id, li_id, amount,
                 "income" if amount < 0 else ("savings" if "TFSA" in merchant else "spending")),
            )

    # routing_rules is a minimal table (no priority/name fields) — skip seeding rules for now
    pass

    conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dark", action="store_true", help="Force dark mode for screenshots (sets localStorage theme=dark)")
    parser.add_argument("--light", action="store_true", help="Force light mode")
    parser.add_argument("--label", default="", help="Label suffix for screenshot filenames")
    parser.add_argument("--mobile-only", action="store_true")
    parser.add_argument("--viewport-only", action="store_true", help="Screenshot just the viewport (no full_page), useful for verifying fixed elements")
    args = parser.parse_args()

    import uvicorn
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="ui_check_") as tmpdir:
        os.environ["FRIDAY_BP_APP_DIR"] = tmpdir
        os.environ["FRIDAY_BP_DB_PATH"] = str(Path(tmpdir) / "data.db")

        # Reload modules to pick up env changes
        import importlib
        if "server.paths" in sys.modules:
            importlib.reload(sys.modules["server.paths"])
        import server.paths as _paths
        db_path = Path(tmpdir) / "data.db"
        _paths.DB_PATH = db_path

        from server.db import init_db
        init_db(db_path)

        port = _free_port()
        if "ui.server" in sys.modules:
            importlib.reload(sys.modules["ui.server"])
        from ui.server import app

        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        server_instance = uvicorn.Server(config)
        thread = threading.Thread(target=server_instance.run, daemon=True)
        thread.start()

        base_url = f"http://127.0.0.1:{port}"
        try:
            _wait_for_server(f"{base_url}/healthz")
        except RuntimeError:
            _wait_for_server(f"{base_url}/login")

        # Setup
        _post(base_url, "/setup/1", {"username": "testuser", "password": "testpass123",
                                      "password_confirm": "testpass123"})
        _post(base_url, "/setup/2", {"notification_channel": "openclaw_chat"})
        _post(base_url, "/setup/3", {"action": "skip"})

        try:
            seed_data(db_path)
        except Exception as e:
            print(f"⚠️  Seed failed: {e}", file=sys.stderr)

        # Now drive playwright
        viewports = [
            ("desktop", 1440, 900),
            ("mobile",  390, 844),
        ]
        if args.mobile_only:
            viewports = [v for v in viewports if v[0] == "mobile"]

        pages = ["/dashboard", "/accounts", "/ledgers", "/settings", "/profile"]

        suffix = ("_" + args.label) if args.label else ""

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for vp_name, w, h in viewports:
                ctx_kwargs = {"viewport": {"width": w, "height": h}}
                if args.dark:
                    ctx_kwargs["color_scheme"] = "dark"
                elif args.light:
                    ctx_kwargs["color_scheme"] = "light"
                ctx = browser.new_context(**ctx_kwargs)
                # Force-set the theme so the time-of-day JS doesn't override us.
                if args.dark or args.light:
                    theme = "dark" if args.dark else "light"
                    ctx.add_init_script(
                        "try { localStorage.setItem('friday-theme', '" + theme + "'); } catch(e){}"
                    )
                page = ctx.new_page()
                # Login
                page.goto(f"{base_url}/login")
                page.wait_for_load_state("domcontentloaded")
                page.fill("input[name='username']", "testuser")
                page.fill("input[name='password']", "testpass123")
                page.click("button[type='submit']")
                page.wait_for_url("**/dashboard", timeout=10000)

                for path in pages:
                    page.goto(base_url + path)
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(500)
                    safe = path.strip("/").replace("/", "_") or "root"
                    out = OUT_DIR / f"{safe}_{vp_name}{suffix}.png"
                    page.screenshot(path=str(out), full_page=not args.viewport_only)
                    print(f"📸 {out}")
                ctx.close()
            browser.close()

        server_instance.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
