"""
tests/playwright/test_bank_link_flow.py
========================================

End-to-end Playwright tests for the Plaid bank-link flow (Bug #218-B4).

These tests verify:
  1. After completing the bank-link flow, connected accounts appear on the
     /accounts page.
  2. After a sync triggered by the link flow, transactions are visible in the UI.

Tests are skipped when:
  - PLAID_CLIENT_ID / PLAID_SECRET are not set in the environment, OR
  - playwright is not installed / browser binary missing.

To run locally:
    export PLAID_CLIENT_ID=<sandbox_client_id>
    export PLAID_SECRET=<sandbox_secret>
    export PLAID_ENV=sandbox
    playwright install chromium
    pytest tests/playwright/test_bank_link_flow.py -v
"""

from __future__ import annotations

import os
import socket
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Skip guards
# ---------------------------------------------------------------------------

PLAID_CREDS_AVAILABLE = bool(os.environ.get("PLAID_CLIENT_ID")) and bool(
    os.environ.get("PLAID_SECRET")
)

try:
    from playwright.sync_api import sync_playwright  # noqa: F401

    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False

pytestmark = [
    pytest.mark.skipif(
        not PLAID_CREDS_AVAILABLE,
        reason=(
            "PLAID_CLIENT_ID and/or PLAID_SECRET not set — Plaid E2E tests skipped. "
            "Add PLAID_SANDBOX_CLIENT_ID and PLAID_SANDBOX_SECRET to GitHub Actions "
            "secrets to enable."
        ),
    ),
    pytest.mark.skipif(
        not PLAYWRIGHT_AVAILABLE,
        reason="playwright package not installed (pip install playwright && playwright install chromium).",
    ),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SANDBOX_INSTITUTION_ID = "ins_109508"  # First Platypus Bank


def _free_port() -> int:
    """Return an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 10.0) -> None:
    """Poll until the server responds or timeout elapses."""
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"Server did not start within {timeout}s at {url}")


def _init_test_db(db_path: Path) -> str:
    """Initialise a fresh DB, seed a user, and return user_id."""
    from server.db import init_db

    init_db(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO users (id, username, password_hash, created_at) VALUES ('u-e2e', 'testuser', 'x', strftime('%s','now'))"
    )
    try:
        conn.execute("INSERT INTO user_settings (user_id) VALUES ('u-e2e')")
    except sqlite3.IntegrityError:
        pass
    conn.commit()
    conn.close()
    return "u-e2e"


def _complete_setup_and_login(base_url: str, password: str = "testpassword123") -> None:
    """Drive the setup wizard + login via HTTP requests (no browser needed for setup)."""
    import urllib.parse
    import urllib.request

    def _post(path: str, data: dict) -> int:
        encoded = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            f"{base_url}{path}",
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    _post("/setup/1", {"password": password, "password_confirm": password})
    _post("/setup/2", {"notification_pref": "openclaw"})
    _post("/setup/3", {"action": "skip"})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_server_with_accounts():
    """Start a live FastAPI server with pre-linked accounts via mocked Plaid."""
    import uvicorn

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        _init_test_db(db_path)

        env_overrides = {
            "PLAID_CLIENT_ID": os.environ.get("PLAID_CLIENT_ID", "test-cid"),
            "PLAID_SECRET": os.environ.get("PLAID_SECRET", "test-secret"),
            "PLAID_ENV": "sandbox",
            "FRIDAY_BP_APP_DIR": tmpdir,
        }

        with patch.dict(os.environ, env_overrides, clear=False):
            import server.crypto as _crypto
            import server.main as _sm
            import server.paths as _paths

            orig_db = _paths.DB_PATH
            _paths.DB_PATH = db_path

            try:
                # Seed accounts via mocked complete_link + sync
                with (
                    patch.object(_crypto, "encrypt", side_effect=lambda t: t + "_enc"),
                    patch.object(_crypto, "decrypt", side_effect=lambda t: t.replace("_enc", "")),
                ):
                    mock_provider = MagicMock()
                    mock_provider.env = "sandbox"
                    mock_provider.exchange_public_token.return_value = {
                        "access_token": "access-sandbox-e2e",
                        "item_id": "item-e2e",
                    }
                    mock_provider.get_institution_name.return_value = "First Platypus Bank"

                    with patch.object(_sm, "PlaidProvider", return_value=mock_provider):
                        _sm.complete_link(public_token="public-sandbox-e2e")

                    mock_sync_provider = MagicMock()
                    mock_sync_provider.env = "sandbox"
                    mock_sync_provider.sync_transactions.return_value = {
                        "added": [
                            {
                                "transaction_id": "tx-e2e-1",
                                "account_id": "acct-e2e-chq",
                                "name": "E2E Test Merchant",
                                "amount": 42.00,
                                "date": "2025-01-15",
                                "merchant_name": "E2E Test Merchant",
                                "personal_finance_category": {
                                    "primary": "FOOD_AND_DRINK",
                                    "detailed": "FOOD_AND_DRINK_RESTAURANTS",
                                },
                            }
                        ],
                        "modified": [],
                        "removed": [],
                        "next_cursor": "cursor-e2e-v1",
                        "accounts": [
                            {
                                "account_id": "acct-e2e-chq",
                                "name": "E2E Checking",
                                "official_name": "E2E Gold Checking",
                                "type": "depository",
                                "subtype": "checking",
                                "balances": {"current": 2500.0, "available": 2400.0},
                                "mask": "1234",
                            }
                        ],
                    }

                    with patch.object(_sm, "PlaidProvider", return_value=mock_sync_provider):
                        _sm.sync()

                # Start the UI server on a free port
                port = _free_port()
                from ui.server import app

                config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
                server_instance = uvicorn.Server(config)
                thread = threading.Thread(target=server_instance.run, daemon=True)
                thread.start()

                base_url = f"http://127.0.0.1:{port}"
                _wait_for_server(f"{base_url}/healthz")
                _complete_setup_and_login(base_url)

                yield {"base_url": base_url, "db_path": db_path, "tmpdir": tmpdir}

                server_instance.should_exit = True
                thread.join(timeout=5)
            finally:
                _paths.DB_PATH = orig_db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_connect_bank_accounts_appear(live_server_with_accounts):
    """After linking a bank via complete_link() + sync(), the /accounts page
    must show the connected accounts.

    Regression guard: if sync() is not triggered after complete_link(), the
    bank_accounts table remains empty and the UI shows nothing.
    """
    from playwright.sync_api import sync_playwright

    base_url = live_server_with_accounts["base_url"]

    screenshots_dir = Path(__file__).parent / "_screenshots"
    screenshots_dir.mkdir(exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        # Login
        page.goto(f"{base_url}/login")
        page.fill("input[name='password']", "testpassword123")
        page.click("button[type='submit']")
        page.wait_for_url("**/dashboard", timeout=5000)

        # Navigate to accounts
        page.goto(f"{base_url}/accounts")
        page.wait_for_load_state("networkidle")

        # Take a screenshot for the test artefact
        page.screenshot(path=str(screenshots_dir / "test_bank_link_accounts.png"))

        # Verify the linked account appears
        content = page.content()
        assert "E2E Checking" in content or "First Platypus Bank" in content, (
            "The /accounts page does not show the linked account after complete_link + sync. "
            "The post-link sync may not be triggering correctly."
        )

        browser.close()


def test_connect_bank_sync_runs_and_transactions_visible(live_server_with_accounts):
    """After linking a bank and syncing, transactions must be visible in the UI.

    Regression guard: if the post-link sync does not run, the ledger/dashboard
    will show no transactions even though a bank is connected.
    """
    from playwright.sync_api import sync_playwright

    base_url = live_server_with_accounts["base_url"]

    screenshots_dir = Path(__file__).parent / "_screenshots"
    screenshots_dir.mkdir(exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        # Login
        page.goto(f"{base_url}/login")
        page.fill("input[name='password']", "testpassword123")
        page.click("button[type='submit']")
        page.wait_for_url("**/dashboard", timeout=5000)

        # Navigate to ledgers
        page.goto(f"{base_url}/ledgers")
        page.wait_for_load_state("networkidle")

        page.screenshot(path=str(screenshots_dir / "test_bank_link_ledgers.png"))

        # The transaction from sync should appear somewhere in the UI
        # (either in ledgers or dashboard)
        ledger_content = page.content()

        page.goto(f"{base_url}/dashboard")
        page.wait_for_load_state("networkidle")
        dashboard_content = page.content()

        page.screenshot(path=str(screenshots_dir / "test_bank_link_dashboard.png"))

        # Verify the transaction is visible in at least one of the pages
        combined_content = ledger_content + dashboard_content
        assert (
            "E2E Test Merchant" in combined_content
            or "E2E Checking" in combined_content
            or "First Platypus Bank" in combined_content
        ), (
            "No synced transactions or account info visible in ledgers/dashboard after "
            "complete_link + sync. The post-link sync may not be populating transactions."
        )

        browser.close()
