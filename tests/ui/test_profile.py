"""
tests/ui/test_profile.py — Playwright tests for the /profile page.

Route shape (from ui/server.py and ui/templates/profile.html):
  GET /profile    → settings + Linked Accounts section (auth required)
  /link/start     → Plaid Link initiation (linked via "+ Connect a bank" button)

Tests skip cleanly when Playwright / Chromium are not installed (see conftest).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(page, server_url: str, username: str, password: str) -> None:
    """Log in via the /login form."""
    page.goto(server_url + "/login")
    page.fill("input#username", username)
    page.fill("input#password", password)
    page.click("button[type=submit]")
    page.wait_for_url("**/profile", timeout=5000)


# ---------------------------------------------------------------------------
# Module-scoped fixture: ensure a user exists for this test module.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def profile_user(server_url):
    """Create a user via setup wizard (if the server is fresh) and return creds."""
    from playwright.sync_api import sync_playwright

    username = "profiletest"
    password = "profile_pw_99"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context()
        pg = ctx.new_page()

        pg.goto(server_url + "/")
        if "/setup" in pg.url:
            pg.fill("input#username", username)
            pg.fill("input#password", password)
            pg.fill("input#password_confirm", password)
            pg.click("button[type=submit]")
            # Step 2
            pg.click("button[type=submit]")
            # Step 3 — skip
            pg.locator("button[type=submit]", has_text="Skip").first.click()
            # Step 4 — finish
            pg.click("button[type=submit]")
            pg.wait_for_url("**/profile", timeout=5000)

        ctx.close()
        browser.close()

    return username, password


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_profile_shows_linked_accounts_section(page, server_url, profile_user):
    """Authenticated /profile includes the 'Linked Accounts' section."""
    username, password = profile_user
    _login(page, server_url, username, password)

    # The <section id="linked-accounts"> is present and has a heading.
    heading = page.locator("#linked-accounts h2")
    assert heading.count() > 0
    assert "Linked Accounts" in heading.first.inner_text()


def test_profile_has_connect_bank_button(page, server_url, profile_user):
    """Profile page shows a '+ Connect a bank' button."""
    username, password = profile_user
    _login(page, server_url, username, password)

    btn = page.locator("a", has_text="Connect a bank")
    assert btn.count() > 0, "Expected '+ Connect a bank' link on /profile"
    assert btn.first.is_visible()
