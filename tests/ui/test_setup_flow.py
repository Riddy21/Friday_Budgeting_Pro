"""
tests/ui/test_setup_flow.py — Playwright tests for the first-run setup wizard.

Route shape (from ui/server.py):
  GET  /setup        → shows step 1 (password)
  POST /setup/1      → validates & creates user, renders step 2 (notifications)
  POST /setup/2      → saves notification pref, renders step 3 (bank)
  POST /setup/3      → bank or skip (action=skip), renders step 4 (done)
  POST /setup/4      → finalises setup, redirects to /profile

Tests skip cleanly when Playwright / Chromium are not installed (see conftest).
"""

from __future__ import annotations


def test_root_redirects_to_setup(page, server_url):
    """GET / → /setup when no user exists yet."""
    response = page.goto(server_url + "/")
    # After redirect chain we should land on /setup
    assert "/setup" in page.url


def test_setup_step1_form_visible(page, server_url):
    """Setup page shows username and password fields."""
    page.goto(server_url + "/setup")
    assert page.locator("input#username").is_visible()
    assert page.locator("input#password").is_visible()
    assert page.locator("input#password_confirm").is_visible()


def test_setup_wizard_full_flow(page, server_url):
    """Walk through all four steps of the setup wizard and land on /profile."""
    # ── Step 1: username + password ─────────────────────────────────────────
    page.goto(server_url + "/setup")
    page.fill("input#username", "testuser")
    page.fill("input#password", "supersecret1")
    page.fill("input#password_confirm", "supersecret1")
    page.click("button[type=submit]")

    # Should now show step 2 (notification preference)
    assert (
        page.locator("input[name=notification_channel]").count() > 0
    ), "Expected notification channel radio buttons on step 2"

    # ── Step 2: pick default notification channel ────────────────────────────
    # The first radio is already checked; just submit.
    page.click("button[type=submit]")

    # Should now show step 3 (connect bank / skip)
    skip_btn = page.locator("button[type=submit]", has_text="Skip")
    assert skip_btn.count() > 0, "Expected a Skip button on step 3"

    # ── Step 3: skip bank ────────────────────────────────────────────────────
    skip_btn.first.click()

    # Should now show step 4 (finish)
    finish_btn = page.locator("button[type=submit]")
    assert finish_btn.count() > 0, "Expected a finish button on step 4"
    # Step 4 heading should mention "all set" or similar
    assert page.locator("h2").count() > 0

    # ── Step 4: complete setup ───────────────────────────────────────────────
    finish_btn.first.click()

    # Should redirect to /profile
    page.wait_for_url("**/profile", timeout=5000)
    assert "/profile" in page.url
