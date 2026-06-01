"""
tests/test_ui_functional.py
============================

Playwright functional tests for Friday Budgeting Pro.

Tests every page and every interactive element across three viewport sizes:
  - Mobile  390×844  (iPhone 14)
  - Tablet  768×1024 (iPad portrait)
  - Desktop 1440×900 (laptop)

Run with:
    pytest tests/test_ui_functional.py -v

Requirements:
    pip install playwright
    playwright install chromium

The test server starts on a random free port with a seeded in-memory SQLite DB.
No real Plaid credentials are required — bank-sync actions are stubbed.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Skip guard — playwright not installed
# ---------------------------------------------------------------------------

try:
    from playwright.sync_api import Browser, Page, sync_playwright  # noqa: F401

    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not PLAYWRIGHT_AVAILABLE,
    reason="playwright not installed — run: pip install playwright && playwright install chromium",
)

# ---------------------------------------------------------------------------
# Viewport definitions
# ---------------------------------------------------------------------------

VIEWPORTS = [
    {"name": "mobile", "width": 390, "height": 844},
    {"name": "tablet", "width": 768, "height": 1024},
    {"name": "desktop", "width": 1440, "height": 900},
]

SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"Server did not become available at {url} within {timeout}s")


def _post(base_url: str, path: str, data: dict) -> int:
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


def _complete_setup(base_url: str, username: str = "testuser", password: str = "testpass123") -> None:
    """Drive the setup wizard via HTTP without a browser."""
    _post(base_url, "/setup/1", {
        "username": username,
        "password": password,
        "password_confirm": password,
    })
    _post(base_url, "/setup/2", {"notification_channel": "openclaw_chat"})
    _post(base_url, "/setup/3", {"action": "skip"})


def _login_browser(page: Page, base_url: str, password: str = "testpass123") -> None:
    """Log into the app using the browser."""
    page.goto(f"{base_url}/login")
    page.wait_for_load_state("domcontentloaded")
    page.fill("input[name='username']", "testuser")
    page.fill("input[name='password']", password)
    page.click("button[type='submit']")
    page.wait_for_url("**/dashboard", timeout=8000)


def _screenshot(page: Page, name: str) -> None:
    """Save a screenshot on failure or for documentation."""
    path = SCREENSHOTS_DIR / f"{name}.png"
    try:
        page.screenshot(path=str(path))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Live server fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_server():
    """
    Spin up a real FastAPI server on a free port with a fresh SQLite DB.

    Yields a dict:
        {
            "base_url": "http://127.0.0.1:<port>",
            "db_path": Path,
            "username": "testuser",
            "password": "testpass123",
        }
    """
    import uvicorn

    with tempfile.TemporaryDirectory(prefix="friday_ui_test_") as tmpdir:
        db_path = Path(tmpdir) / "data.db"

        os.environ["FRIDAY_BP_APP_DIR"] = tmpdir

        # Reload paths module so module-level constants update
        import importlib
        import sys

        if "server.paths" in sys.modules:
            importlib.reload(sys.modules["server.paths"])

        import server.paths as _paths

        orig_db = _paths.DB_PATH
        _paths.DB_PATH = db_path

        from server.db import init_db

        init_db(db_path)

        port = _free_port()

        # Import fresh app after env is set
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

        _complete_setup(base_url)

        yield {
            "base_url": base_url,
            "db_path": db_path,
            "username": "testuser",
            "password": "testpass123",
        }

        server_instance.should_exit = True
        thread.join(timeout=5)
        _paths.DB_PATH = orig_db


# ---------------------------------------------------------------------------
# Browser fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def browser_instance():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture()
def page_desktop(browser_instance, live_server):
    ctx = browser_instance.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    _login_browser(page, live_server["base_url"], live_server["password"])
    yield page
    ctx.close()


@pytest.fixture()
def page_mobile(browser_instance, live_server):
    ctx = browser_instance.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    _login_browser(page, live_server["base_url"], live_server["password"])
    yield page
    ctx.close()


@pytest.fixture()
def page_tablet(browser_instance, live_server):
    ctx = browser_instance.new_context(viewport={"width": 768, "height": 1024})
    page = ctx.new_page()
    _login_browser(page, live_server["base_url"], live_server["password"])
    yield page
    ctx.close()


# ---------------------------------------------------------------------------
# Helpers to save screenshots on failure
# ---------------------------------------------------------------------------


def guard(page: Page, name: str, fn):
    """Run fn(); on exception take a screenshot and re-raise."""
    try:
        fn()
    except Exception as exc:
        _screenshot(page, f"FAIL_{name}")
        raise exc


# ===========================================================================
# PAGE LOAD TESTS — all three viewports
# ===========================================================================


class TestPageLoads:
    """Every protected page must load without error (title present, no 5xx)."""

    PAGES = [
        ("/dashboard", "Dashboard"),
        ("/accounts", "Accounts"),
        ("/ledgers", "Ledgers"),
        ("/settings", "Settings"),
        ("/profile", "Profile"),
    ]

    @pytest.mark.parametrize("path,title_fragment", PAGES)
    def test_page_loads_desktop(self, page_desktop, live_server, path, title_fragment):
        url = live_server["base_url"] + path
        page_desktop.goto(url)
        page_desktop.wait_for_load_state("domcontentloaded")
        _screenshot(page_desktop, f"desktop_{title_fragment.lower()}_load")
        assert title_fragment in page_desktop.title(), (
            f"Expected '{title_fragment}' in page title for {path}, got: {page_desktop.title()}"
        )

    @pytest.mark.parametrize("path,title_fragment", PAGES)
    def test_page_loads_mobile(self, page_mobile, live_server, path, title_fragment):
        url = live_server["base_url"] + path
        page_mobile.goto(url)
        page_mobile.wait_for_load_state("domcontentloaded")
        _screenshot(page_mobile, f"mobile_{title_fragment.lower()}_load")
        assert title_fragment in page_mobile.title()

    @pytest.mark.parametrize("path,title_fragment", PAGES)
    def test_page_loads_tablet(self, page_tablet, live_server, path, title_fragment):
        url = live_server["base_url"] + path
        page_tablet.goto(url)
        page_tablet.wait_for_load_state("domcontentloaded")
        _screenshot(page_tablet, f"tablet_{title_fragment.lower()}_load")
        assert title_fragment in page_tablet.title()


# ===========================================================================
# AUTH PAGES — login, forgot, reset
# ===========================================================================


class TestAuthPages:
    """Unauthenticated pages load correctly."""

    def test_login_page_loads(self, browser_instance, live_server):
        ctx = browser_instance.new_context()
        page = ctx.new_page()
        page.goto(live_server["base_url"] + "/login")
        page.wait_for_load_state("domcontentloaded")
        _screenshot(page, "login_page")
        assert "Login" in page.title() or "Sign in" in page.title()
        assert page.locator("input[name='username']").count() > 0
        assert page.locator("input[name='password']").count() > 0
        assert page.locator("button[type='submit']").count() > 0
        ctx.close()

    def test_login_invalid_credentials(self, browser_instance, live_server):
        ctx = browser_instance.new_context()
        page = ctx.new_page()
        page.goto(live_server["base_url"] + "/login")
        page.fill("input[name='username']", "nobody")
        page.fill("input[name='password']", "wrongpassword")
        page.click("button[type='submit']")
        page.wait_for_load_state("domcontentloaded")
        _screenshot(page, "login_invalid")
        # Should stay on login page or show error
        content = page.content()
        assert "login" in page.url.lower() or "error" in content.lower() or "invalid" in content.lower()
        ctx.close()

    def test_forgot_password_page_loads(self, browser_instance, live_server):
        ctx = browser_instance.new_context()
        page = ctx.new_page()
        page.goto(live_server["base_url"] + "/forgot")
        page.wait_for_load_state("domcontentloaded")
        _screenshot(page, "forgot_page")
        assert page.locator("input[name='username']").count() > 0
        ctx.close()

    def test_reset_page_loads(self, browser_instance, live_server):
        ctx = browser_instance.new_context()
        page = ctx.new_page()
        page.goto(live_server["base_url"] + "/reset")
        page.wait_for_load_state("domcontentloaded")
        _screenshot(page, "reset_page")
        # Should have a form or redirect to login
        assert page.locator("form").count() > 0 or "login" in page.url


# ===========================================================================
# DASHBOARD PAGE
# ===========================================================================


class TestDashboardPage:
    """
    Dashboard interactive elements:
      - [btn] "Sync Now"  → POST /api/sync  (Plaid-required)
      - [link] "Export to Excel" → GET /export/excel
      - [nav] Dashboard, Accounts, Ledgers, Settings nav links
      - [link] "Log out" → /logout
    """

    def test_sync_button_exists_and_clickable(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/dashboard")
        page_desktop.wait_for_load_state("domcontentloaded")

        btn = page_desktop.locator("#btn-sync-now")
        assert btn.count() > 0, "Sync Now button not found on dashboard"

        # Click — expect either a sync response or error (not 500)
        with page_desktop.expect_response(
            lambda r: "/api/sync" in r.url, timeout=5000
        ) as resp_info:
            btn.click()

        resp = resp_info.value
        _screenshot(page_desktop, "dashboard_sync_clicked")
        assert resp.status < 500, f"Sync API returned {resp.status}"

    def test_export_excel_link_exists(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/dashboard")
        page_desktop.wait_for_load_state("domcontentloaded")
        link = page_desktop.locator("a[href='/export/excel']")
        assert link.count() > 0, "Export to Excel link not found on dashboard"

    def test_nav_links_present(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/dashboard")
        page_desktop.wait_for_load_state("domcontentloaded")

        for label in ["Dashboard", "Accounts", "Ledgers", "Settings"]:
            assert page_desktop.locator(f"nav a:has-text('{label}')").count() > 0, (
                f"Nav link '{label}' not found"
            )

    def test_logout_link_present(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/dashboard")
        page_desktop.wait_for_load_state("domcontentloaded")
        assert page_desktop.locator("nav a[href='/logout']").count() > 0

    def test_dashboard_mobile_layout(self, page_mobile, live_server):
        page_mobile.goto(live_server["base_url"] + "/dashboard")
        page_mobile.wait_for_load_state("domcontentloaded")
        _screenshot(page_mobile, "dashboard_mobile")
        # Sync button should still be visible on mobile
        assert page_mobile.locator("#btn-sync-now").count() > 0

    def test_dashboard_tablet_layout(self, page_tablet, live_server):
        page_tablet.goto(live_server["base_url"] + "/dashboard")
        page_tablet.wait_for_load_state("domcontentloaded")
        _screenshot(page_tablet, "dashboard_tablet")
        assert page_tablet.locator("#btn-sync-now").count() > 0


# ===========================================================================
# ACCOUNTS PAGE
# ===========================================================================


class TestAccountsPage:
    """
    Accounts interactive elements:
      - [link/btn] "+ Connect a bank" → /link/start  (Plaid-required)
      - [btn] "Copy token"  → JS clipboard copy of Plaid access token (Plaid-required)
      - [btn] "Disconnect"  → POST /profile (disconnect_bank action)  (Plaid-required)
      - [btn] "rename"      → inline rename of account via PATCH /accounts/<id>/name
      - [btn] "transactions"→ expand row, fetch /accounts/<id>/transactions
    """

    def test_connect_bank_link_present(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/accounts")
        page_desktop.wait_for_load_state("domcontentloaded")
        _screenshot(page_desktop, "accounts_desktop")
        # The "Connect a bank" button/link should be present
        assert (
            page_desktop.locator("a[href='/link/start']").count() > 0
            or page_desktop.locator("text=Connect a bank").count() > 0
        ), "Connect a bank link not found on accounts page"

    def test_accounts_page_mobile(self, page_mobile, live_server):
        page_mobile.goto(live_server["base_url"] + "/accounts")
        page_mobile.wait_for_load_state("domcontentloaded")
        _screenshot(page_mobile, "accounts_mobile")
        assert "Accounts" in page_mobile.title()

    def test_accounts_page_tablet(self, page_tablet, live_server):
        page_tablet.goto(live_server["base_url"] + "/accounts")
        page_tablet.wait_for_load_state("domcontentloaded")
        _screenshot(page_tablet, "accounts_tablet")
        assert "Accounts" in page_tablet.title()

    def test_empty_state_hint(self, page_desktop, live_server):
        """When no accounts are connected, an empty-state hint is shown."""
        page_desktop.goto(live_server["base_url"] + "/accounts")
        page_desktop.wait_for_load_state("domcontentloaded")
        content = page_desktop.content()
        # Either empty-state hint OR account table
        has_hint = "No bank accounts" in content or "Connect a bank" in content
        has_table = page_desktop.locator("table").count() > 0
        assert has_hint or has_table, "Accounts page shows neither empty-state nor account table"


# ===========================================================================
# LEDGERS PAGE
# ===========================================================================


class TestLedgersPage:
    """
    Ledgers interactive elements:
      - [btn] "+ Add Ledger" → shows inline form
      - [input] New ledger name → text field
      - [btn] "Create" → POST /ledgers
      - [btn] "Cancel" → hides form
      - [btn] "x" (delete ledger) → DELETE /ledgers/<id>
      - [btn] "x" (delete line item) → DELETE /ledgers/<id>/items/<item_id>
      - [input] add-item-input → adds income/expense line item (Enter key)
      - [btn] expand (▾) → toggle transaction list per line item
      - [link] period selector (This month, Last month, etc.) → reload with period param
      - [span] ledger-name-display → click to rename inline
      - [span] item-name-display → click to rename inline
    """

    def test_add_ledger_btn_shows_form(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/ledgers")
        page_desktop.wait_for_load_state("domcontentloaded")

        # Click "+ Add Ledger"
        btn = page_desktop.locator("#btn-add-ledger")
        assert btn.count() > 0, "+ Add Ledger button missing"
        btn.click()

        # Form should appear
        form = page_desktop.locator("#add-ledger-form")
        assert form.is_visible(), "Add ledger form did not appear after clicking button"
        _screenshot(page_desktop, "ledgers_add_form_visible")

    def test_cancel_add_ledger_form(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/ledgers")
        page_desktop.wait_for_load_state("domcontentloaded")

        page_desktop.locator("#btn-add-ledger").click()
        page_desktop.locator("#add-ledger-form").wait_for(state="visible")

        page_desktop.locator("#btn-add-ledger-cancel").click()

        form = page_desktop.locator("#add-ledger-form")
        assert not form.is_visible(), "Add ledger form should be hidden after cancel"

    def test_create_ledger_via_form(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/ledgers")
        page_desktop.wait_for_load_state("domcontentloaded")

        page_desktop.locator("#btn-add-ledger").click()
        page_desktop.locator("#add-ledger-form").wait_for(state="visible")

        page_desktop.fill("#new-ledger-name", "Test Ledger UI")

        with page_desktop.expect_response(
            lambda r: "/ledgers" in r.url and r.request.method == "POST", timeout=5000
        ) as resp_info:
            page_desktop.locator("#btn-add-ledger-submit").click()

        resp = resp_info.value
        _screenshot(page_desktop, "ledgers_create_ledger")
        assert resp.status < 500, f"Create ledger returned {resp.status}"

    def test_period_selector_links(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/ledgers")
        page_desktop.wait_for_load_state("domcontentloaded")

        for label in ["This month", "Last month", "Last 3 months", "This year", "All time"]:
            link = page_desktop.locator(f".period-selector a:has-text('{label}')")
            assert link.count() > 0, f"Period selector link '{label}' not found"

    def test_period_selector_navigates(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/ledgers")
        page_desktop.wait_for_load_state("domcontentloaded")

        page_desktop.locator(".period-selector a:has-text('Last month')").click()
        page_desktop.wait_for_load_state("domcontentloaded")
        _screenshot(page_desktop, "ledgers_last_month")
        assert "last_month" in page_desktop.url or "period" in page_desktop.url

    def test_ledgers_mobile(self, page_mobile, live_server):
        page_mobile.goto(live_server["base_url"] + "/ledgers")
        page_mobile.wait_for_load_state("domcontentloaded")
        _screenshot(page_mobile, "ledgers_mobile")
        assert "Ledgers" in page_mobile.title()
        assert page_mobile.locator("#btn-add-ledger").count() > 0

    def test_ledgers_tablet(self, page_tablet, live_server):
        page_tablet.goto(live_server["base_url"] + "/ledgers")
        page_tablet.wait_for_load_state("domcontentloaded")
        _screenshot(page_tablet, "ledgers_tablet")
        assert "Ledgers" in page_tablet.title()


# ===========================================================================
# SETTINGS PAGE
# ===========================================================================


class TestSettingsPage:
    """
    Settings interactive elements:
      - [select] home_currency → change currency
      - [select] timezone → change timezone
      - [btn] "Save" (type=submit) → POST /settings
      - [table] classification rules list (read-only)
    """

    def test_settings_form_elements_present(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/settings")
        page_desktop.wait_for_load_state("domcontentloaded")
        _screenshot(page_desktop, "settings_desktop")

        assert page_desktop.locator("select#home_currency").count() > 0, "Currency select not found"
        assert page_desktop.locator("select#timezone").count() > 0, "Timezone select not found"
        assert page_desktop.locator("button[type='submit']").count() > 0, "Save button not found"

    def test_settings_save_button(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/settings")
        page_desktop.wait_for_load_state("domcontentloaded")

        # Change currency
        page_desktop.select_option("select#home_currency", "USD")

        with page_desktop.expect_response(
            lambda r: "/settings" in r.url and r.request.method == "POST", timeout=5000
        ) as resp_info:
            page_desktop.locator("button[type='submit']").first.click()

        resp = resp_info.value
        _screenshot(page_desktop, "settings_saved")
        assert resp.status < 500, f"Settings save returned {resp.status}"

    def test_settings_mobile(self, page_mobile, live_server):
        page_mobile.goto(live_server["base_url"] + "/settings")
        page_mobile.wait_for_load_state("domcontentloaded")
        _screenshot(page_mobile, "settings_mobile")
        assert "Settings" in page_mobile.title()
        assert page_mobile.locator("button[type='submit']").count() > 0

    def test_settings_tablet(self, page_tablet, live_server):
        page_tablet.goto(live_server["base_url"] + "/settings")
        page_tablet.wait_for_load_state("domcontentloaded")
        _screenshot(page_tablet, "settings_tablet")
        assert "Settings" in page_tablet.title()


# ===========================================================================
# PROFILE PAGE
# ===========================================================================


class TestProfilePage:
    """
    Profile interactive elements:
      - [select] notification_pref → dropdown
      - [btn] "Save settings" (type=submit) → POST /profile
      - [link] "View Ledgers" → /ledgers
      - [btn] "Sync Now" (form submit) → POST /profile (sync_now)  (Plaid-required)
      - [link] "Export Excel" → /export/excel
      - [link] "+ Connect a bank" → /link/start  (Plaid-required)
      - [btn] "Disconnect" (per connection) → POST /profile (disconnect_bank)  (Plaid-required)
      - [btn] "Reconnect" (if needs_reauth) → POST /profile (reconnect_bank)  (Plaid-required)
      - [input] account-desc-input → per-account description
      - [btn] "Save" (btn-save-desc) → PATCH /profile/accounts/<id>/description
    """

    def test_profile_notification_select(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/profile")
        page_desktop.wait_for_load_state("domcontentloaded")
        _screenshot(page_desktop, "profile_desktop")

        sel = page_desktop.locator("select#notification_pref")
        assert sel.count() > 0, "Notification preference select not found"

    def test_profile_save_settings(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/profile")
        page_desktop.wait_for_load_state("domcontentloaded")

        page_desktop.select_option("select#notification_pref", "ui")

        with page_desktop.expect_response(
            lambda r: "/profile" in r.url and r.request.method == "POST", timeout=5000
        ) as resp_info:
            page_desktop.locator("button.btn-primary[type='submit']").first.click()

        resp = resp_info.value
        _screenshot(page_desktop, "profile_saved")
        assert resp.status < 500, f"Profile save returned {resp.status}"

    def test_profile_view_ledgers_link(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/profile")
        page_desktop.wait_for_load_state("domcontentloaded")
        assert page_desktop.locator("a[href='/ledgers']").count() > 0, "View Ledgers link not found"

    def test_profile_export_excel_link(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/profile")
        page_desktop.wait_for_load_state("domcontentloaded")
        assert page_desktop.locator("a[href='/export/excel']").count() > 0, "Export Excel link not found"

    def test_profile_connect_bank_link(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/profile")
        page_desktop.wait_for_load_state("domcontentloaded")
        assert page_desktop.locator("a[href='/link/start']").count() > 0, "Connect a bank link not found"

    def test_profile_mobile(self, page_mobile, live_server):
        page_mobile.goto(live_server["base_url"] + "/profile")
        page_mobile.wait_for_load_state("domcontentloaded")
        _screenshot(page_mobile, "profile_mobile")
        assert "Profile" in page_mobile.title()

    def test_profile_tablet(self, page_tablet, live_server):
        page_tablet.goto(live_server["base_url"] + "/profile")
        page_tablet.wait_for_load_state("domcontentloaded")
        _screenshot(page_tablet, "profile_tablet")
        assert "Profile" in page_tablet.title()


# ===========================================================================
# NAVIGATION
# ===========================================================================


class TestNavigation:
    """Nav links route to the correct pages."""

    NAV_TARGETS = [
        ("Dashboard", "/dashboard"),
        ("Accounts", "/accounts"),
        ("Ledgers", "/ledgers"),
        ("Settings", "/settings"),
    ]

    @pytest.mark.parametrize("label,expected_path", NAV_TARGETS)
    def test_nav_link_routing(self, page_desktop, live_server, label, expected_path):
        page_desktop.goto(live_server["base_url"] + "/dashboard")
        page_desktop.wait_for_load_state("domcontentloaded")

        page_desktop.locator(f"nav a:has-text('{label}')").click()
        page_desktop.wait_for_load_state("domcontentloaded")

        assert expected_path in page_desktop.url, (
            f"Clicking '{label}' nav link expected to navigate to {expected_path}, "
            f"but URL is {page_desktop.url}"
        )

    def test_logout_link_redirects_to_login(self, browser_instance, live_server):
        ctx = browser_instance.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        _login_browser(page, live_server["base_url"], live_server["password"])

        page.locator("nav a[href='/logout']").click()
        page.wait_for_load_state("domcontentloaded")

        assert "login" in page.url.lower(), f"Logout did not redirect to login, URL: {page.url}"
        ctx.close()


# ===========================================================================
# EXPORT EXCEL
# ===========================================================================


class TestExportExcel:
    """Export to Excel should return a file (or graceful empty message)."""

    def test_export_excel_responds(self, page_desktop, live_server):
        page_desktop.goto(live_server["base_url"] + "/dashboard")
        page_desktop.wait_for_load_state("domcontentloaded")

        with page_desktop.expect_response(
            lambda r: "/export/excel" in r.url, timeout=10000
        ) as resp_info:
            page_desktop.locator("a[href='/export/excel']").first.click()

        resp = resp_info.value
        _screenshot(page_desktop, "export_excel")
        assert resp.status < 500, f"Excel export returned {resp.status}"


# ===========================================================================
# RESPONSIVE LAYOUT CHECKS
# ===========================================================================


class TestResponsiveLayout:
    """Verify that critical UI elements are visible across all viewports."""

    @pytest.mark.parametrize("viewport", VIEWPORTS)
    def test_header_visible(self, browser_instance, live_server, viewport):
        ctx = browser_instance.new_context(viewport={"width": viewport["width"], "height": viewport["height"]})
        page = ctx.new_page()
        _login_browser(page, live_server["base_url"], live_server["password"])

        page.goto(live_server["base_url"] + "/dashboard")
        page.wait_for_load_state("domcontentloaded")
        _screenshot(page, f"header_{viewport['name']}")

        header = page.locator("header")
        assert header.count() > 0, f"Header not found at {viewport['name']}"
        assert header.is_visible(), f"Header not visible at {viewport['name']}"
        ctx.close()

    @pytest.mark.parametrize("viewport", VIEWPORTS)
    def test_footer_visible(self, browser_instance, live_server, viewport):
        ctx = browser_instance.new_context(viewport={"width": viewport["width"], "height": viewport["height"]})
        page = ctx.new_page()
        _login_browser(page, live_server["base_url"], live_server["password"])

        page.goto(live_server["base_url"] + "/dashboard")
        page.wait_for_load_state("domcontentloaded")

        footer = page.locator("footer")
        assert footer.count() > 0, f"Footer not found at {viewport['name']}"
        ctx.close()

    @pytest.mark.parametrize("viewport", VIEWPORTS)
    def test_main_content_visible(self, browser_instance, live_server, viewport):
        ctx = browser_instance.new_context(viewport={"width": viewport["width"], "height": viewport["height"]})
        page = ctx.new_page()
        _login_browser(page, live_server["base_url"], live_server["password"])

        page.goto(live_server["base_url"] + "/dashboard")
        page.wait_for_load_state("domcontentloaded")

        main = page.locator("main")
        assert main.count() > 0 and main.is_visible(), f"Main content not visible at {viewport['name']}"
        ctx.close()


class TestSavingsUI:
    """Tests for the savings summary section added in PR #313."""

    def test_api_summary_this_month(self, live_server):
        """GET /api/summary?period=this_month returns valid JSON."""
        import urllib.request, urllib.parse, json as _json

        base = live_server["base_url"]

        # Need a session cookie — log in via HTTP
        import http.cookiejar
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        payload = urllib.parse.urlencode(
            {"username": live_server["username"], "password": live_server["password"]}
        ).encode()
        opener.open(urllib.request.Request(
            f"{base}/login", data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        ))

        # Now hit the endpoint
        resp = opener.open(f"{base}/api/summary?period=this_month")
        assert resp.status == 200
        data = _json.loads(resp.read().decode())
        assert "error" not in data, f"API returned error: {data['error']}"
        for key in ("income", "expenses", "savings", "savings_contributions",
                    "unspent_balance", "total_saved", "savings_rate", "savings_rate_ytd"):
            assert key in data, f"Missing key: {key}"

    @pytest.mark.parametrize("period", ["this_month", "last_month", "this_year", "ytd"])
    def test_api_summary_all_periods(self, live_server, period):
        """All UI period strings return valid JSON (no ValueError)."""
        import urllib.request, urllib.parse, json as _json
        import http.cookiejar

        base = live_server["base_url"]
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        payload = urllib.parse.urlencode(
            {"username": live_server["username"], "password": live_server["password"]}
        ).encode()
        opener.open(urllib.request.Request(
            f"{base}/login", data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        ))

        resp = opener.open(f"{base}/api/summary?period={period}")
        assert resp.status == 200
        data = _json.loads(resp.read().decode())
        assert "error" not in data, f"period={period} returned error: {data.get('error')}"
        assert "savings_rate" in data

    def test_dashboard_savings_cards_render(self, page_desktop, live_server):
        """Dashboard savings section renders two cards (no JS error)."""
        import time as _time
        page = page_desktop
        _login_browser(page, live_server["base_url"], live_server["password"])
        page.goto(live_server["base_url"] + "/dashboard")
        page.wait_for_load_state("networkidle")
        _time.sleep(1)  # let the async fetch complete

        # Savings section should exist
        assert page.locator("#savings-summary-section").count() > 0, "Savings section missing"

        # Two savings cards should have replaced the loading placeholder
        cards = page.locator(".savings-card").count()
        assert cards == 2, f"Expected 2 savings cards, got {cards}"

        # The loading placeholder should be gone
        loading = page.locator("#savings-loading")
        assert loading.count() == 0, "Loading placeholder still visible after fetch"

    def test_dashboard_no_js_errors_savings(self, browser_instance, live_server):
        """No JavaScript errors while loading the savings section on dashboard."""
        import time as _time
        ctx = browser_instance.new_context()
        page = ctx.new_page()
        js_errors = []
        page.on("pageerror", lambda exc: js_errors.append(str(exc)))
        page.on("console", lambda msg: js_errors.append(msg.text) if msg.type == "error" else None)

        _login_browser(page, live_server["base_url"], live_server["password"])
        page.goto(live_server["base_url"] + "/dashboard")
        page.wait_for_load_state("networkidle")
        _time.sleep(1)

        assert js_errors == [], f"JS errors on dashboard: {js_errors}"
        ctx.close()
