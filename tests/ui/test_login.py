"""
tests/ui/test_login.py — Playwright tests for the login / logout flow.

Prerequisites (handled by conftest):
  - Server running with a fresh DB.
  - The session-scoped `server_url` fixture is shared — tests run after
    test_setup_flow has already completed setup (same server process).
    If tests run in isolation the fixture boots a fresh server, so we
    create a user via the setup wizard before testing login.

Tests skip cleanly when Playwright / Chromium are not installed (see conftest).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Module-level helper: ensure the server has a user we can log in with.
# We use a session-scoped fixture so the wizard runs only once per session.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registered_user(server_url):
    """Ensure a test user exists by completing the setup wizard if needed.

    Returns (username, password).
    """
    from playwright.sync_api import sync_playwright

    username = "logintest"
    password = "hunter2abc"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context()
        pg = ctx.new_page()

        pg.goto(server_url + "/")
        if "/setup" in pg.url:
            # Complete the wizard
            pg.fill("input#username", username)
            pg.fill("input#password", password)
            pg.fill("input#password_confirm", password)
            pg.click("button[type=submit]")
            # Step 2 — submit defaults
            pg.click("button[type=submit]")
            # Step 3 — skip bank
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


def test_login_page_renders(page, server_url, registered_user):
    """GET /login shows username and password fields."""
    page.goto(server_url + "/login")
    assert page.locator("input#username").is_visible()
    assert page.locator("input#password").is_visible()


def test_login_wrong_password_shows_error(page, server_url, registered_user):
    """Wrong password keeps the user on /login and shows an error."""
    username, _ = registered_user
    page.goto(server_url + "/login")
    page.fill("input#username", username)
    page.fill("input#password", "wrongpassword!")
    page.click("button[type=submit]")

    # Still on /login
    assert "/login" in page.url
    # Error message visible somewhere on the page
    assert page.locator(".alert-error").is_visible()


def test_login_correct_password_redirects_to_profile(page, server_url, registered_user):
    """Correct credentials redirect to /profile."""
    username, password = registered_user
    page.goto(server_url + "/login")
    page.fill("input#username", username)
    page.fill("input#password", password)
    page.click("button[type=submit]")

    page.wait_for_url("**/profile", timeout=5000)
    assert "/profile" in page.url


def test_logout_redirects_to_login(page, server_url, registered_user):
    """Clicking Sign out lands back on /login."""
    username, password = registered_user

    # Log in first
    page.goto(server_url + "/login")
    page.fill("input#username", username)
    page.fill("input#password", password)
    page.click("button[type=submit]")
    page.wait_for_url("**/profile", timeout=5000)

    # Submit logout form (POST /logout)
    page.click("button[type=submit]:has-text('Sign out')")
    page.wait_for_url("**/login", timeout=5000)
    assert "/login" in page.url
