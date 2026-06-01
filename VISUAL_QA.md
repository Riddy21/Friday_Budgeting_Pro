# Visual QA Checklist — Friday Budgeting Pro

This document defines the standard process for visual quality assurance and
responsive-design review.  Run this checklist after every significant frontend
change and before any release.

---

## Standard Viewport Sizes

| Name    | Resolution  | Device target          |
|---------|-------------|------------------------|
| Mobile  | 390 × 844   | iPhone 14 / modern iOS |
| Tablet  | 768 × 1024  | iPad (portrait)        |
| Desktop | 1440 × 900  | Laptop / 13-inch screen|

---

## How to Run Visual Inspection

### Option A — Browser DevTools (recommended)

1. Start the server:
   ```bash
   cd /Users/hal9000/.openclaw/workspace/bank-transactions
   source .venv/bin/activate
   uvicorn ui.server:app --port 6789
   ```
2. Open Chrome/Firefox → `http://localhost:6789`
3. Open DevTools → Responsive Mode (⌘⇧M in Chrome, ⌘⌥M in Firefox)
4. Set viewport to each size in the table above
5. Walk through every page in the checklist below

### Option B — Peekaboo CLI (automated screenshots)

```bash
# Check if peekaboo is available
peekaboo --help

# Screenshot at each viewport (if supported)
peekaboo screenshot --url http://localhost:6789/dashboard --width 390  --height 844  --out qa/mobile/dashboard.png
peekaboo screenshot --url http://localhost:6789/dashboard --width 768  --height 1024 --out qa/tablet/dashboard.png
peekaboo screenshot --url http://localhost:6789/dashboard --width 1440 --height 900  --out qa/desktop/dashboard.png
```

### Option C — Playwright pytest suite (CI)

```bash
# Install once (if not already in .venv)
source .venv/bin/activate
pip install playwright
playwright install chromium

# Run the full automated functional test suite
pytest tests/ui_functional/test_ui_functional.py -v

# Run a specific viewport class
pytest tests/ui_functional/test_ui_functional.py -v -k "mobile"

# Screenshots saved automatically to tests/ui_functional/screenshots/
```

The suite starts its own server on a random free port, seeds a fresh DB, and
runs all 62 tests.  **No Plaid credentials required.**

---

## Pages Under Test

| # | Route          | Template          | Auth required |
|---|----------------|-------------------|---------------|
| 1 | `/login`       | login.html        | No            |
| 2 | `/setup`       | setup.html        | No            |
| 3 | `/forgot`      | forgot.html       | No            |
| 4 | `/reset`       | reset.html        | No            |
| 5 | `/dashboard`   | dashboard.html    | Yes           |
| 6 | `/accounts`    | accounts.html     | Yes           |
| 7 | `/ledgers`     | ledgers.html      | Yes           |
| 8 | `/settings`    | settings.html     | Yes           |
| 9 | `/profile`     | profile.html      | Yes           |
| 10| `/link`        | link.html         | Yes           |

---

## Per-Page Checklist

For each page × viewport, complete the following checks and mark **Pass ✅ / Needs Work ⚠️ / Broken ❌**.

### Check Categories

| ID  | Category                   | Pass criteria                                                         |
|-----|----------------------------|-----------------------------------------------------------------------|
| L1  | No horizontal overflow     | No scrollbar appears; all content fits within viewport width          |
| L2  | Grid / layout intact       | Columns, cards, and sections stack correctly at smaller widths        |
| L3  | No truncated text          | Labels, headings, values are fully readable; no clipping              |
| T1  | Touch target sizes         | All buttons/links are ≥ 44 × 44 px on mobile (WCAG 2.5.5)            |
| T2  | Tap target spacing         | Adjacent targets have ≥ 8 px gap to prevent mis-taps                 |
| TY1 | Base font size             | Body text ≥ 16 px on mobile (no zoom required to read)               |
| TY2 | Small text readable        | All secondary/muted text ≥ 12 px; data labels legible                |
| N1  | Navigation accessible      | Nav links visible and reachable without scrolling horizontally        |
| N2  | Active page indicated      | Current page is visually marked in the nav                            |
| F1  | Form inputs usable         | Inputs, selects have adequate height (≥ 44 px) and don't clip labels  |
| F2  | Form labels visible        | Labels are not obscured; sit above inputs on mobile                   |
| I1  | No inline-style overrides  | Page uses design tokens / CSS classes, not scattered inline styles    |
| A1  | Focus rings visible        | Keyboard focus is always visible (not removed)                        |
| A2  | Color contrast             | Text has ≥ 4.5:1 contrast ratio on its background (WCAG AA)           |

---

## Current Audit Results (baseline — 2026-05-31)

> Audit performed via static code review of templates + CSS.
> Visual screenshots pending browser automation setup.

### Summary Grade Table

| Page        | Mobile Layout | Tablet Layout | Desktop Layout | Touch Targets | Typography | Navigation |
|-------------|:-------------:|:-------------:|:--------------:|:-------------:|:----------:|:----------:|
| /login      | ⚠️ Needs Work | ✅ Pass        | ✅ Pass         | ⚠️ Needs Work | ✅ Pass     | ✅ N/A      |
| /setup      | ⚠️ Needs Work | ✅ Pass        | ✅ Pass         | ⚠️ Needs Work | ✅ Pass     | ✅ N/A      |
| /forgot     | ✅ Pass        | ✅ Pass        | ✅ Pass         | ⚠️ Needs Work | ✅ Pass     | ✅ N/A      |
| /reset      | ✅ Pass        | ✅ Pass        | ✅ Pass         | ⚠️ Needs Work | ✅ Pass     | ✅ N/A      |
| /dashboard  | ⚠️ Needs Work | ✅ Pass        | ✅ Pass         | ✅ Pass        | ✅ Pass     | ❌ Broken   |
| /accounts   | ❌ Broken      | ⚠️ Needs Work | ✅ Pass         | ❌ Broken      | ⚠️ Needs Work | ❌ Broken |
| /ledgers    | ❌ Broken      | ⚠️ Needs Work | ✅ Pass         | ❌ Broken      | ⚠️ Needs Work | ❌ Broken |
| /settings   | ⚠️ Needs Work | ✅ Pass        | ✅ Pass         | ⚠️ Needs Work | ✅ Pass     | ❌ Broken   |
| /profile    | ❌ Broken      | ⚠️ Needs Work | ✅ Pass         | ❌ Broken      | ⚠️ Needs Work | ❌ Broken |
| /link       | ⚠️ Needs Work | ✅ Pass        | ✅ Pass         | ⚠️ Needs Work | ✅ Pass     | ✅ N/A      |

### Key Findings

1. **No responsive breakpoints in style.css** — The entire stylesheet has zero `@media` queries. All layout is fixed at `max-width: 860px` with `padding: 0 24px`. On a 390px screen this means content uses the full width but tables and multi-column layouts overflow.

2. **Navigation overflows on mobile** — `header` uses `display: flex` with nav links side-by-side. At 390px the nav bar wraps or clips. No hamburger/drawer menu exists.

3. **Tables not responsive** — `/accounts`, `/ledgers`, `/settings` (rules table), `/profile` (connections table) all use `<table>` with no horizontal scroll wrapper or responsive reflow. On mobile these tables extend beyond the viewport.

4. **Touch targets below 44px minimum** — `.btn-sm { padding: 0.2rem 0.5rem }` renders at ~26×22px. The "x" delete buttons in ledgers have no padding at all. The `rename` / `transactions` inline buttons are similarly tiny.

5. **Mixed / duplicated styling** — `accounts.html` defines an inline `<style>` block with `.btn`, `.btn-primary`, `.btn-danger` that shadow the global stylesheet. This fragmentation means design token changes require updating multiple files.

6. **Settings page submit button unstyled** — `<button type="submit">Save</button>` has no class applied; it will render as a plain browser-default button instead of the design system's `.btn-primary`.

7. **Very small inline font sizes** — Multiple inline styles use `font-size: 0.75rem` (≈12px), `font-size: 0.8rem`, `font-size: 0.78rem` for account masks and table captions. These are at/below the readable minimum on mobile.

8. **Account descriptions flex layout breaks on mobile** — `/profile` account descriptions use `min-width:14rem` labels in a flex row; on 390px screens the row overflows or forces the input and button off-screen.

9. **No component design tokens** — Colors, radii, and spacing are defined in `:root` in the global CSS but many templates use raw hex codes inline (`#64748b`, `#e2e8f0`, etc.) rather than CSS variables. Future redesigns require grep-and-replace.

10. **Missing pages** — No `/transactions` browser page (only inline per-account expansion), no `/budgets`, no `/reports` / summary view. The dashboard has a "Coming soon" placeholder for charts.

---

## Pass/Fail Criteria

| Grade        | Meaning                                                                  |
|--------------|--------------------------------------------------------------------------|
| ✅ Pass       | Fully meets the check; no action needed                                  |
| ⚠️ Needs Work | Functional but visually degraded or slightly below standard              |
| ❌ Broken     | Non-functional or unusable at this viewport; must be fixed before ship   |

---

## Adding New Pages

When adding a new route, add a row to the **Pages Under Test** table and run all
14 checks in a new column of the grade table before merging.

---

## Interactive Element Catalog (JOB 2 — 2026-05-31)

Audit of every interactive element across all HTML templates.
**Plaid-required** = element triggers a Plaid bank connection action and cannot be fully tested without sandbox credentials.

### base.html

| Element | Type | Label/Text | Action | Plaid-required? |
|---------|------|-----------|--------|-----------------|
| `nav a` (×5) | Link | Dashboard / Accounts / Ledgers / Settings / Log out | Page navigation + logout | No |
| `a.brand` | Link | 📊 Friday Budgeting Pro | Navigate to / | No |

---

### dashboard.html

| Element | Type | Label/Text | Action | Plaid-required? |
|---------|------|-----------|--------|-----------------|
| `#btn-sync-now` | Button | Sync Now | `fetch POST /api/sync` → polls `/api/sync/result` + `/api/classify/status` | Yes (Plaid sync) |
| `a[href='/export/excel']` | Link | Export to Excel | `GET /export/excel` → download .xlsx | No |

---

### accounts.html

| Element | Type | Label/Text | Action | Plaid-required? |
|---------|------|-----------|--------|-----------------|
| `a[href='/link/start']` | Link | + Connect a bank | Opens Plaid Link flow | Yes |
| `.copy-token-btn` | Button | Copy token | `fetch GET /accounts/<conn>/access-token` → clipboard | Yes |
| form `disconnect` | Submit button | Disconnect | `POST /profile` action=disconnect_bank | Yes |
| `.rename-btn` | Button | rename | Inline edit → `fetch PATCH /accounts/<id>/name` | No |
| `.txn-toggle-btn` | Button | transactions / hide | Toggle expand row; `fetch GET /accounts/<id>/transactions` | No |

---

### ledgers.html

| Element | Type | Label/Text | Action | Plaid-required? |
|---------|------|-----------|--------|-----------------|
| `#btn-add-ledger` | Button | + Add Ledger | Shows `#add-ledger-form` | No |
| `#btn-add-ledger-submit` | Button | Create | `fetch POST /ledgers` | No |
| `#btn-add-ledger-cancel` | Button | Cancel | Hides `#add-ledger-form` | No |
| `.btn-delete-ledger` | Button | x | Confirm dialog → `fetch DELETE /ledgers/<id>` | No |
| `.btn-delete-item` | Button | x (per line item) | Confirm / force → `fetch DELETE /ledgers/<id>/items/<item_id>` | No |
| `.add-item-input` | Input (text) | Add item… | Enter key → `fetch POST /ledgers/<id>/items` | No |
| `.btn-expand-item` | Button | ▾ | Toggle `.txn-detail-row` visibility | No |
| `.period-selector a` (×5) | Links | This month / Last month / Last 3 months / This year / All time | Reload `/ledgers?period=<value>` | No |
| `.ledger-name-display` | Span (click) | Ledger name | Inline edit → `fetch PATCH /ledgers/<id>` | No |
| `.ledger-name-input` | Input (text) | (ledger name) | blur/Enter → `fetch PATCH /ledgers/<id>` | No |
| `.item-name-display` | Span (click) | Item name | Inline edit → `fetch PATCH /ledgers/<id>/items/<item_id>` | No |
| `.item-name-input` | Input (text) | (item name) | blur/Enter → `fetch PATCH /ledgers/<id>/items/<item_id>` | No |

---

### settings.html

| Element | Type | Label/Text | Action | Plaid-required? |
|---------|------|-----------|--------|-----------------|
| `select#home_currency` | Select | Home Currency | Form field | No |
| `select#timezone` | Select | Timezone | Form field | No |
| `button[type='submit']` | Button | Save | `POST /settings` | No |

---

### profile.html

| Element | Type | Label/Text | Action | Plaid-required? |
|---------|------|-----------|--------|-----------------|
| `select#notification_pref` | Select | Notification preference | Form field | No |
| `button.btn-primary[type='submit']` | Button | Save settings | `POST /profile` | No |
| `a[href='/ledgers']` | Link | View Ledgers | Navigate to /ledgers | No |
| `button#btn-sync-now` (form) | Submit | Sync Now | `POST /profile` action=sync_now | Yes (Plaid sync) |
| `a[href='/export/excel']` | Link | Export Excel | `GET /export/excel` | No |
| `a[href='/link/start']` | Link | + Connect a bank | Opens Plaid Link flow | Yes |
| `button.btn-danger` (per connection) | Submit | Disconnect | `POST /profile` action=disconnect_bank | Yes |
| `button.btn-warning` (if needs_reauth) | Submit | Reconnect | `POST /profile` action=reconnect_bank | Yes |
| `.account-desc-input` | Input (text) | per-account description | Form field | No |
| `.btn-save-desc` | Button | Save | `fetch PATCH /profile/accounts/<id>/description` | No |

---

### login.html

| Element | Type | Label/Text | Action | Plaid-required? |
|---------|------|-----------|--------|-----------------|
| `input[name='username']` | Input (text) | Username | Form field | No |
| `input[name='password']` | Input (password) | Password | Form field | No |
| `button[type='submit']` | Button | Sign in | `POST /login` | No |
| `a[href='/forgot']` | Link | Forgot your password? | Navigate to /forgot | No |
| `a.btn-secondary` (profile switch, conditional) | Links | profile username(s) | `/login?username=<name>` | No |

---

### forgot.html / reset.html

| Element | Type | Label/Text | Action | Plaid-required? |
|---------|------|-----------|--------|-----------------|
| `input[name='username']` | Input (text) | Username | Form field | No |
| `button[type='submit']` | Button | Send recovery token | `POST /forgot` | No |
| `input[name='token']` | Input (text) | Recovery token | Form field | No |
| `input[name='new_password']` | Input (password) | New password | Form field | No |
| `button[type='submit']` | Button | Reset password | `POST /reset` | No |

---

### setup.html

| Element | Type | Label/Text | Action | Plaid-required? |
|---------|------|-----------|--------|-----------------|
| `input[name='username']` | Input (text) | Your name | Form field | No |
| `input[name='password']` | Input (password) | Password | Form field | No |
| `input[name='password_confirm']` | Input (password) | Confirm password | Form field | No |
| `button.btn-primary` step 1 | Button | Continue → | `POST /setup/1` | No |
| `input[name='notification_channel']` (×3) | Radio | OpenClaw chat / macOS / In-browser | Form field | No |
| `button.btn-primary` step 2 | Button | Continue → | `POST /setup/2` | No |
| `#plaid-link-btn` | Button | Connect a bank → | Opens Plaid.create handler | Yes |
| `button.btn-secondary` (skip) | Submit | Skip for now → | `POST /setup/3 action=skip` | No |

---

### link.html

| Element | Type | Label/Text | Action | Plaid-required? |
|---------|------|-----------|--------|-----------------|
| `a[href='<back_url>']` | Link | Cancel | Navigate back | No |
| Plaid Link iframe (auto-open) | JS widget | (auto-opens) | Plaid flow → `POST <complete_url>` | Yes |

---

## Playwright Functional Test Results (2026-05-31)

**Test file:** `tests/ui_functional/test_ui_functional.py`
**Run command:** `pytest tests/ui_functional/test_ui_functional.py -v`
**Result: 62 passed, 2 warnings in 44.59s**

| Test Class | Tests | Pass | Fail | Notes |
|------------|-------|------|------|-------|
| TestPageLoads | 15 | 15 | 0 | All pages × 3 viewports |
| TestAuthPages | 4 | 4 | 0 | Login, forgot, reset, invalid login |
| TestDashboardPage | 6 | 6 | 0 | Sync btn, Export link, Nav, Logout, Mobile, Tablet |
| TestAccountsPage | 4 | 4 | 0 | Connect-bank link, mobile, tablet, empty-state |
| TestLedgersPage | 7 | 7 | 0 | Add form, cancel, create, period selector, nav, mobile, tablet |
| TestSettingsPage | 4 | 4 | 0 | Form elements, save, mobile, tablet |
| TestProfilePage | 7 | 7 | 0 | Select, save, links, mobile, tablet |
| TestNavigation | 5 | 5 | 0 | All nav links + logout redirect |
| TestExportExcel | 1 | 1 | 0 | Excel export endpoint responds |
| TestResponsiveLayout | 9 | 9 | 0 | Header/footer/main across all 3 viewports |

All Plaid-dependent actions (Sync Now, Disconnect, Connect bank) were verified
to respond without 5xx errors.  The Sync Now button returns `200 OK` with a
`status: already_running` or `status: ok` response even without Plaid credentials.

Screenshots saved to: `tests/ui_functional/screenshots/`

---

## Test Data — Seed Script (added 2026-05-31)

**Script:** `tests/seed_test_data.py`  
**Purpose:** Seeds a standalone sandbox SQLite DB with realistic Canadian personal finance transactions for visual/functional testing without touching the production DB.

### How to Reseed
```bash
# Default path: /tmp/friday-test-seed.db
TEST_DB=/tmp/friday-test-seed.db python3 tests/seed_test_data.py

# Start test server on port 7894
mkdir -p /tmp/friday-test-bp && cp /tmp/friday-test-seed.db /tmp/friday-test-bp/data.db
FRIDAY_BP_APP_DIR=/tmp/friday-test-bp python3 -m uvicorn ui.server:app --port 7894
```

### Expected Totals (May 2026)
| Metric | Value |
|--------|-------|
| Income | $7,600.00 (2× bi-weekly Tenstorrent paycheques) |
| Expenses | $1,997.90 (groceries + dining + transport + shopping + subscriptions + utilities) |
| Savings | $800.00 (Wealthsimple TFSA $500 + RRSP $300) |
| **Net** | **$5,602.10** (income − expenses) |
| Unspent | $4,802.10 (net − savings) |
| Net Rate | 73.7% |
| Internal transfers | $1,000.00 (excluded from totals) |

### Transaction Categories in Seed Data
- **Income (2):** Tenstorrent payroll bi-weekly
- **Groceries (8):** Loblaws, Metro, Costco, FreshCo, No Frills
- **Dining (6):** Various restaurants (Banjara, Tim Hortons, Mandarin, etc.)
- **Transport (4):** TTC pass, Uber, gas, parking
- **Shopping (5):** Amazon, Best Buy, H&M, Canadian Tire, IKEA
- **Subscriptions (4):** Netflix, Spotify, iCloud, GoodLife Fitness
- **Utilities (3):** Toronto Hydro, Rogers Internet, Fizz Mobile
- **Savings (2):** Wealthsimple TFSA + RRSP
- **Transfer (1):** Internal chequing-to-chequing (entry_type='transfer', excluded)

### Visual Screenshots (1440×900 and 390×844)
Screenshots from the seeded test server are saved to `tests/screenshots/`:
- `dashboard-desktop.png` — Dashboard savings section showing Net (Income − Expenses)
- `ledgers-desktop.png` — Ledger footer with 3 columns: Total income, Total expenses, Net
- `dashboard-mobile.png` — Mobile dashboard
- `ledgers-mobile-bottom.png` — Mobile ledger totals footer

