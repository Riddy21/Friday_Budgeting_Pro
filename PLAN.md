# Friday Budgeting Pro — Architecture & Design

## Vision

A **general-purpose financial tracking platform** designed to handle the real
mess of personal finance: bank accounts that mix personal, rental, and business
spending; shared cards between partners; recurring costs that change names; and
all the edge cases that defeat traditional rules-based budgeting tools.

Built as an **MCP server** with an **LLM-powered classification layer** so the
system can reason about transactions instead of just pattern-matching. Packaged
as a **ClawHub skill** for one-command installation into OpenClaw.

The core insight: **budget tracking is money flowing between entities across
time, tagged with meaning** — and the "tagging with meaning" part is exactly
what LLMs are good at.

---

## The Hard Problem (Why Rules Aren't Enough)

Real bank accounts don't have clean boundaries:
- A single credit card pays for personal, rental property, and small business
- A grocery store charge could be personal OR a rental snack run
- Home improvement stores could be personal OR property maintenance
- Recurring subscriptions sometimes shift between personal/business
- Two partners share one card with overlapping spending
- Cash transactions and Venmo splits muddy the waters

**Static regex rules can't handle this.** The system needs to *understand*
each transaction in context.

---

## Three-Tier Classification Engine

```
Transaction arrives
    │
    ▼
┌─────────────────────────────────────────┐
│ Tier 1: Deterministic Rules             │  Fast, exact, no LLM cost
│   - Exact merchant match                │
│   - Plaid category + amount + account   │
│   - Recurring pattern (1st of month)    │
└─────────────────────────────────────────┘
    │ no match / partial match
    ▼
┌─────────────────────────────────────────┐
│ Tier 2: LLM Classifier                  │  Reasoning over context
│   Inputs:                               │
│   - Transaction details                 │
│   - User's ledgers + line items         │
│   - Recent similar transactions         │
│   - User's natural-language hints       │
│   - Location/merchant metadata          │
│   Output: routing decision + confidence │
└─────────────────────────────────────────┘
    │ confidence < threshold
    ▼
┌─────────────────────────────────────────┐
│ Tier 3: Human-in-the-loop               │  Ambiguous case
│   - Notify user via configured channel  │
│   - Show transaction + LLM's best guess │
│   - User confirms or corrects           │
│   - Decision saved as a new rule        │
└─────────────────────────────────────────┘
```

**Key: every Tier 2 and Tier 3 decision becomes training data.** The LLM has
access to past classifications so it gets sharper over time without retraining.

---

## Natural Language Preferences

Users describe their classification logic in plain English, not regex. These
preferences become the system prompt for the LLM classifier.

Example user preferences:
```
"Grocery stores within 5km of home are personal.
 Grocery stores in London, Ontario are for the rental property in London.

 Home Depot, Rona, Lowe's: usually rental property maintenance.
 If the amount is under $50, probably personal.
 If it's a Saturday, probably personal yard work.

 Tim Hortons is always personal.

 Any transaction from 'TENANT' or with their names in description is rental income.

 Dollar's spending on her credit card is personal/shared.
 Anything labeled 'PROPERTY MGMT' or 'STRATA' is the condo."
```

Stored as `user_preferences.classification_hints` — passed to the LLM with
every classification call.

---

## Core Abstractions

### Ledger
Any named context that money flows through. User-defined.
```
Ledger { id, user_id, name, type, currency, color, icon }
```
Examples: `"Personal"`, `"Rental Property A"`, `"My Business"`, `"Tax Reserve"`

### Line Item
A named row within a ledger.
```
LineItem { id, ledger_id, name, item_type, expected_amount }
```
Examples: `"Groceries"`, `"Mortgage"`, `"Tenant Rent"`, `"Subscriptions"`

### Transaction
Raw event from a bank.
```
Transaction {
  id, user_id, bank_account_id, plaid_transaction_id,
  date, merchant, amount, currency, plaid_category[],
  location { city, lat, lng }, pending
}
```

### Transaction Entry
**The interpreted form** — assigns transaction to ledger/line item, supports
splits. One transaction → one or more entries.
```
TransactionEntry {
  id, transaction_id, ledger_id, line_item_id,
  amount, note, confidence, source, reviewed
}
```
`source` ∈ `{rule, llm, manual}` — tells you how it got there.

### Routing Rule
Deterministic Tier 1 rule.
```
RoutingRule {
  id, user_id, priority,
  match { merchant_pattern, amount_range, account_ids, categories, date_pattern },
  actions [{ ledger_id, line_item_id, allocation_pct }]
}
```

### Classification Hint
Free-form natural language preferences for the LLM.
```
ClassificationHint { id, user_id, text, active }
```

---

## Database Schema

```sql
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT UNIQUE,
  notification_channel TEXT,
  notification_target TEXT,
  llm_confidence_threshold REAL DEFAULT 0.75,
  created_at INTEGER
);

CREATE TABLE bank_connections (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  plaid_item_id TEXT UNIQUE,
  plaid_access_token_encrypted TEXT NOT NULL,
  institution_id TEXT,
  institution_name TEXT,
  status TEXT DEFAULT 'active',
  created_at INTEGER,
  last_synced_at INTEGER
);

CREATE TABLE bank_accounts (
  id TEXT PRIMARY KEY,
  connection_id TEXT REFERENCES bank_connections(id),
  plaid_account_id TEXT UNIQUE,
  name TEXT, official_name TEXT,
  type TEXT, subtype TEXT, mask TEXT,
  currency TEXT DEFAULT 'USD'
);

CREATE TABLE ledgers (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  name TEXT NOT NULL,
  type TEXT DEFAULT 'income_expense',
  currency TEXT DEFAULT 'USD',
  color TEXT, icon TEXT, sort_order INTEGER,
  created_at INTEGER
);

CREATE TABLE line_items (
  id TEXT PRIMARY KEY,
  ledger_id TEXT REFERENCES ledgers(id),
  name TEXT NOT NULL,
  item_type TEXT DEFAULT 'expense',
  expected_amount REAL,
  sort_order INTEGER
);

CREATE TABLE transactions (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  bank_account_id TEXT REFERENCES bank_accounts(id),
  plaid_transaction_id TEXT UNIQUE,
  date TEXT NOT NULL,
  merchant TEXT, amount REAL NOT NULL, currency TEXT DEFAULT 'USD',
  plaid_category TEXT,
  location_city TEXT, location_lat REAL, location_lng REAL,
  pending INTEGER DEFAULT 0,
  created_at INTEGER
);

CREATE TABLE transaction_entries (
  id TEXT PRIMARY KEY,
  transaction_id TEXT REFERENCES transactions(id),
  ledger_id TEXT REFERENCES ledgers(id),
  line_item_id TEXT REFERENCES line_items(id),
  amount REAL NOT NULL,
  note TEXT,
  confidence REAL,
  source TEXT,
  reviewed INTEGER DEFAULT 0,
  created_at INTEGER
);

CREATE TABLE routing_rules (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  priority INTEGER DEFAULT 100,
  merchant_pattern TEXT,
  amount_min REAL, amount_max REAL,
  account_ids TEXT,
  plaid_categories TEXT,
  date_pattern TEXT,
  actions TEXT NOT NULL,
  created_at INTEGER
);

CREATE TABLE classification_hints (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  text TEXT NOT NULL,
  active INTEGER DEFAULT 1,
  created_at INTEGER
);

CREATE TABLE classification_history (
  id TEXT PRIMARY KEY,
  transaction_id TEXT REFERENCES transactions(id),
  source TEXT,
  confidence REAL,
  reasoning TEXT,
  routed_to TEXT,
  user_corrected INTEGER DEFAULT 0,
  created_at INTEGER
);

CREATE TABLE budget_targets (
  id TEXT PRIMARY KEY,
  line_item_id TEXT REFERENCES line_items(id),
  period_type TEXT DEFAULT 'monthly',
  amount REAL NOT NULL,
  start_date TEXT, end_date TEXT
);

CREATE TABLE sync_cursors (
  connection_id TEXT PRIMARY KEY REFERENCES bank_connections(id),
  cursor TEXT,
  last_synced_at INTEGER
);
```

---

## MCP Server — Tool Catalog

### Account
- `create_account(name, email, notification_channel?, notification_target?)`
- `get_account()`
- `update_preferences(llm_confidence_threshold?, notifications?)`

### Bank Connections
- `create_link_token()` — start Plaid Link flow
- `connect_bank(public_token)` — exchange + store
- `list_connections()`
- `get_connection_status(connection_id)`
- `refresh_connection(connection_id)` — Update Mode for broken connections
- `disconnect_bank(connection_id)`

### Setup (LLM-driven, defaults-first)
- `list_templates()` — returns all built-in ledger templates with their default line items
- `suggest_setup(description)` — LLM-friendly: takes a natural-language description of the user's finances, returns a proposed structure (list of ledgers + line items) using matched templates. Does **not** commit.
- `apply_setup(ledgers[])` — commits a full proposed structure in one call. Accepts an array of ledgers with their line items so the LLM can build the whole tree from a conversation without N tool calls.
- `quick_setup(profile)` — one-shot setup for the common cases. `profile` is one of: `individual`, `couple`, `landlord_1`, `landlord_2`, `landlord_n`, `freelancer`, `small_business`, `nonprofit`. Creates default ledgers + line items immediately.

### Ledgers & Line Items (low-level editing)
- `create_ledger(name, type?, currency?, color?, icon?, template?)` — if `template` is set, pre-populates line items from a template
- `list_ledgers()` / `update_ledger(id, ...)` / `delete_ledger(id)`
- `add_line_item(ledger_id, name, item_type?, expected_amount?)`
- `add_line_items_batch(ledger_id, items[])` — bulk add
- `list_line_items(ledger_id)` / `update_line_item(id, ...)` / `remove_line_item(id)`

### Classification — Rules (Tier 1)
- `create_routing_rule(...)`
- `list_routing_rules()` / `update_routing_rule(id, ...)` / `delete_routing_rule(id)`
- `test_routing_rule(id, sample_size?)` — dry run

### Classification — Hints (Tier 2)
- `add_classification_hint(text)` — natural language guidance
- `list_classification_hints()`
- `update_classification_hint(id, text?, active?)`
- `delete_classification_hint(id)`

### Transactions
- `sync_transactions()` — pull + auto-classify all pending
- `list_transactions(filters)`
- `get_unrouted()` / `get_needs_review()` — pending human input
- `route_transaction(transaction_id, allocations[])` — manual routing
- `split_transaction(transaction_id, splits[])` — multi-ledger split
- `confirm_routing(entry_id)` — mark auto-routed entry as reviewed
- `reclassify_transaction(transaction_id, reason?)` — force re-classify

### Reports & Export
- `get_summary(ledger_id?, period?)`
- `get_monthly_breakdown(ledger_id, year)`
- `get_budget_vs_actual(ledger_id, period)`
- `export_excel(ledger_ids?, years?)`
- `export_csv(ledger_id?, period?)`

### Audit
- `get_classification_history(transaction_id)` — why was this routed here?
- `get_llm_stats(period?)` — accuracy of auto-classifications

---

## LLM Classifier — Prompt Architecture

When Tier 1 doesn't match, the classifier is called with:

```
SYSTEM PROMPT:
You are a budget classifier for {user_name}. Route each transaction to the
correct ledger and line item. Respond in JSON: {ledger_id, line_item_id,
amount, confidence (0-1), reasoning}.

USER PREFERENCES (free-form hints):
{joined classification_hints}

AVAILABLE LEDGERS + LINE ITEMS:
{tree of all ledgers with their line items}

RECENT SIMILAR TRANSACTIONS (last 30 of similar amount/merchant):
{transaction history with how they were routed}

CURRENT TRANSACTION:
- Merchant: {merchant}
- Amount: {amount}
- Date: {date}
- Plaid category: {plaid_category}
- Account: {account_name}
- Location: {city if available}
```

LLM returns structured output. If `confidence >= threshold` → auto-route. If
lower → flag for review.

**Cost optimization:**
- Batch Tier 2 classification (one LLM call for N transactions)
- Cache LLM decisions by merchant fingerprint
- After 3 successful auto-classifications of the same merchant → auto-promote
  to a Tier 1 rule

---

## OpenClaw Integration

This is designed as a **first-class OpenClaw companion**.

### Two ways to use it:

**1. Direct MCP server (any client)**
```
mcporter call friday-budgeting-pro.sync_transactions
mcporter call friday-budgeting-pro.get_needs_review
```

**2. ClawHub skill (recommended)**
```bash
clawhub install friday-budgeting-pro
```
After install:
- MCP server auto-registered in OpenClaw config
- SKILL.md installed → HAL knows when to use it
- Setup wizard via `friday-budgeting setup`
- Daily cron job auto-installed for transaction sync
- Notifications routed through user's existing OpenClaw channels (iMessage,
  Telegram, etc.) — no separate channel config needed

### Why this matters:
When a transaction is flagged for review, HAL just messages the user:
> "Got a $47 charge at Home Depot in Toronto on Saturday. Based on your hints,
>  I'd guess personal weekend project (75% confidence). Want me to route it
>  there, or is this for one of the rental properties?"

User replies in natural language. HAL handles the rest.

---

## Excel Export Format

`export_excel(ledger_ids?, years?)` generates one workbook per ledger.

Each workbook:
- **Sheet per year** — rows = line items, columns = Jan–Dec + YTD
  - Income section (green header)
  - Expense section (red header)
  - Net row (bold, bordered)
- **Summary sheet** — multi-year comparison
- **Raw Transactions sheet** — every transaction with ledger/line item assigned,
  filterable, includes `source` column showing how each was classified

---

## Templates (Built-in Defaults)

Friday Budgeting Pro ships with a `templates/` directory of pre-built ledger
structures. The LLM picks one based on the user's description, applies it,
and the user only edits what's different.

Each template is a JSON file:

```json
{
  "id": "landlord_property",
  "name": "Rental Property",
  "icon": "🏠",
  "line_items": [
    { "name": "Tenant Rent",      "type": "income" },
    { "name": "Other Income",     "type": "income"  },
    { "name": "Mortgage",         "type": "expense" },
    { "name": "Property Tax",     "type": "expense" },
    { "name": "Insurance",        "type": "expense" },
    { "name": "Utilities",        "type": "expense" },
    { "name": "Maintenance",      "type": "expense" },
    { "name": "Management Fee",   "type": "expense" }
  ]
}
```

Built-in templates:
- `personal_individual` — Salary, Side Income, Groceries, Dining, Transport, etc.
- `personal_couple` — Same as individual but income includes both partners
- `landlord_property` — Tenant Rent + standard property expenses
- `landlord_condo` — Adds Strata/HOA Fee
- `freelancer` — Client Income + Business Expenses + Tax Reserve
- `small_business` — Revenue, COGS, Operating Costs, Payroll
- `nonprofit` — Donations, Program Expenses, Admin
- `savings_goals` — Flexible, user fills in specific goals

The `suggest_setup` MCP tool uses the LLM to map a user description to one
or more templates, then returns the proposed structure for confirmation
before committing.

---

## Project Structure

```
friday-budgeting-pro/
├── README.md
├── SKILL.md                  ← ClawHub skill manifest
├── ARCHITECTURE.md
├── PLAN.md
├── package.json              ← npm metadata for clawhub publish
├── requirements.txt
├── .env.example
├── .gitignore
│
├── templates/                ← built-in ledger templates
│   ├── personal_individual.json
│   ├── personal_couple.json
│   ├── landlord_property.json
│   ├── landlord_condo.json
│   ├── freelancer.json
│   ├── small_business.json
│   ├── nonprofit.json
│   └── savings_goals.json
│
├── db/
│   ├── schema.sql
│   └── database.py
│
├── mcp_server/
│   ├── server.py             ← FastMCP entry point
│   └── tools/
│       ├── account.py
│       ├── banks.py
│       ├── setup.py              ← suggest_setup, apply_setup, templates
│       ├── ledgers.py
│       ├── routing.py
│       ├── hints.py
│       ├── transactions.py
│       ├── export.py
│       └── audit.py
│
├── plaid/
│   ├── client.py
│   ├── link_ui/
│   │   └── index.html
│   └── sync.py
│
├── engine/
│   ├── router.py             ← Tier 1: deterministic rules
│   ├── llm_classifier.py     ← Tier 2: LLM classification
│   ├── reviewer.py           ← Tier 3: human-in-the-loop
│   └── promoter.py           ← auto-promote LLM decisions → Tier 1 rules
│
├── export/
│   ├── excel.py
│   └── csv_export.py
│
├── notify/
│   └── notifier.py           ← pluggable: imessage / telegram / openclaw-native
│
└── cli/
    └── friday_budgeting.py   ← setup wizard, manual tools
```

---

## Setup Flow

### Via ClawHub (one command)
```bash
clawhub install friday-budgeting-pro
friday-budgeting setup
```

The setup wizard:
1. Asks for Plaid credentials (or uses dev sandbox)
2. Initializes the database
3. Registers MCP server with OpenClaw
4. Walks through creating first account + ledgers
5. Opens Plaid Link UI to connect first bank
6. Installs daily cron job for sync
7. Done

### Manual setup
1. `git clone https://github.com/Riddy21/Friday_Budgeting_Pro`
2. `cp .env.example .env` → fill in Plaid + LLM credentials
3. `python -m db.init` → initialize schema
4. `python -m mcp_server.server` → start server
5. Use any MCP client to call the tools

---

## Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Best data + LLM ecosystem |
| MCP framework | FastMCP | Clean Python MCP implementation |
| Database | SQLite | Zero-config, portable, multi-user-ready |
| Plaid | plaid-python | Official SDK |
| LLM | Pluggable: OpenAI / Anthropic / OpenClaw-routed | User choice |
| Excel | openpyxl | Full xlsx without Excel binary |
| Link UI | HTML + Plaid.js | No framework dependency |
| Encryption | cryptography (Fernet) | Encrypt access tokens at rest |
| Packaging | ClawHub | One-command install into OpenClaw |

---

## Privacy & Security

- All Plaid access tokens encrypted at rest (Fernet)
- `.env` and `*.db` files in `.gitignore` — never committed
- LLM calls send transaction data — users can choose their LLM provider
  (local Ollama, OpenClaw-routed, or external API)
- No telemetry, no analytics phoning home
- Multi-tenant schema means even on a shared instance, users only see their own data

---

## Status
- [x] Architecture designed
- [x] Database schema finalized
- [x] MCP tool catalog defined
- [x] Three-tier classification engine designed
- [x] OpenClaw / ClawHub integration planned
- [x] Repo: https://github.com/Riddy21/Friday_Budgeting_Pro
- [ ] Initialize DB + schema
- [ ] MCP server skeleton + tool implementations
- [ ] Plaid integration (link UI + sync)
- [ ] Tier 1 routing engine
- [ ] Tier 2 LLM classifier
- [ ] Tier 3 review loop
- [ ] Excel export
- [ ] Notification system
- [ ] SKILL.md + ClawHub package metadata
- [ ] README + setup docs
- [ ] End-to-end test
- [ ] Publish to ClawHub registry
