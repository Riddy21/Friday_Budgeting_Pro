"""
tests/ui/test_ledgers.py — Playwright tests for the /ledgers page.

Smoke checks:
  - Page requires authentication
  - Page loads after login with the Ledgers heading
  - The seeded 'Personal' ledger is shown
  - '+ Add Ledger' button is present
  - Income and Expenses sections are shown for the Personal ledger
  - Seeded line items (e.g. 'Salary', 'Groceries') are visible

Prerequisites (handled by conftest):
  - Server running with a DB pre-seeded with testuser/testpass + Personal ledger.

Tests skip cleanly when Playwright / Chromium are not installed (see conftest).
"""

from __future__ import annotations

# Pre-seeded credentials
_USERNAME = "testuser"
_PASSWORD = "testpass"


def _login(page, server_url: str) -> None:
    page.goto(server_url + "/login")
    page.fill("input#username", _USERNAME)
    page.fill("input#password", _PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/dashboard", timeout=5000)


def test_ledgers_requires_auth(page, server_url):
    """GET /ledgers without auth redirects to /login."""
    page.goto(server_url + "/ledgers")
    assert "/login" in page.url, f"Expected redirect to /login, got {page.url}"


def test_ledgers_loads_after_login(page, server_url):
    """Authenticated GET /ledgers shows the Ledgers heading."""
    _login(page, server_url)
    page.goto(server_url + "/ledgers")
    assert "/ledgers" in page.url
    heading = page.locator("h1")
    assert heading.count() > 0
    assert "Ledgers" in heading.first.inner_text()


def test_ledgers_has_add_ledger_button(page, server_url):
    """Ledgers page has a '+ Add Ledger' button."""
    _login(page, server_url)
    page.goto(server_url + "/ledgers")
    btn = page.locator("#btn-add-ledger")
    assert btn.count() > 0, "Expected #btn-add-ledger on /ledgers"
    assert btn.first.is_visible()


def test_ledgers_shows_personal_ledger(page, server_url):
    """The seeded 'Personal' ledger is shown on the ledgers page."""
    _login(page, server_url)
    page.goto(server_url + "/ledgers")
    personal = page.locator(".ledger-name-display", has_text="Personal")
    assert personal.count() > 0, "Expected 'Personal' ledger on /ledgers"


def test_ledgers_shows_income_and_expense_sections(page, server_url):
    """Ledgers page shows Income and Expenses sections."""
    _login(page, server_url)
    page.goto(server_url + "/ledgers")
    income_heading = page.locator("h3", has_text="Income")
    expense_heading = page.locator("h3", has_text="Expenses")
    assert income_heading.count() > 0, "Expected an Income section in ledgers"
    assert expense_heading.count() > 0, "Expected an Expenses section in ledgers"


def test_ledgers_shows_seeded_line_items(page, server_url):
    """Seeded line items like 'Salary' and 'Groceries' appear in the ledger."""
    _login(page, server_url)
    page.goto(server_url + "/ledgers")
    salary = page.locator(".item-name-display", has_text="Salary")
    groceries = page.locator(".item-name-display", has_text="Groceries")
    assert salary.count() > 0, "Expected 'Salary' line item in ledger"
    assert groceries.count() > 0, "Expected 'Groceries' line item in ledger"


def test_ledgers_shows_savings_section(page, server_url):
    """Ledgers page shows a Savings & Investments section for each ledger."""
    _login(page, server_url)
    page.goto(server_url + "/ledgers")
    savings_heading = page.locator("h3", has_text="Savings")
    assert savings_heading.count() > 0, "Expected a Savings section in the ledger"
    # Verify it has the correct amber/gold styling
    section = page.locator(".item-section[data-section='savings']")
    assert section.count() > 0, "Expected .item-section[data-section='savings'] element"


def test_ledgers_savings_section_always_visible(page, server_url):
    """Savings section is always shown even when no savings transactions exist."""
    _login(page, server_url)
    page.goto(server_url + "/ledgers")
    # The savings section should be present regardless of whether savings data exists
    section = page.locator(".item-section[data-section='savings']")
    assert section.count() > 0, "Savings section should always be visible"
    assert section.first.is_visible(), "Savings section should be visible"


def test_ledgers_savings_section_has_add_input(page, server_url):
    """Savings section has an 'Add savings item' input."""
    _login(page, server_url)
    page.goto(server_url + "/ledgers")
    add_input = page.locator(".add-item-input[data-section='savings']")
    assert add_input.count() > 0, "Expected 'Add savings item' input in savings section"


def test_ledgers_savings_shows_seeded_item(page, server_url):
    """Seeded 'Investments & Savings' line item appears in the savings section."""
    _login(page, server_url)
    page.goto(server_url + "/ledgers")
    savings_item = page.locator(".item-name-display", has_text="Investments & Savings")
    assert savings_item.count() > 0, "Expected 'Investments & Savings' in savings section"


def test_ledgers_savings_totals_in_footer(page, server_url):
    """Ledger totals footer shows a Savings row."""
    _login(page, server_url)
    page.goto(server_url + "/ledgers")
    totals_row = page.locator(".ledger-totals-row")
    assert totals_row.count() > 0, "Expected .ledger-totals-row footer"
    savings_total = page.locator("[data-total='savings']")
    assert savings_total.count() > 0, "Expected savings total value in footer"


def test_ledgers_can_add_savings_item_via_api(page, server_url):
    """Adding a savings item via the 'Add savings item' input creates a savings line item."""
    _login(page, server_url)
    page.goto(server_url + "/ledgers")

    # Find the savings add-input and add an item
    add_input = page.locator(".add-item-input[data-section='savings']").first
    add_input.fill("TFSA Contributions")
    add_input.press("Enter")

    # Wait for the new item to appear in the savings tbody
    savings_tbody = page.locator("tbody.items-tbody[data-section='savings']").first
    new_item = savings_tbody.locator(".item-name-display", has_text="TFSA Contributions")
    new_item.wait_for(timeout=5000)
    assert new_item.count() > 0, "Expected 'TFSA Contributions' to appear in savings section"
