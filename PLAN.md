# Friday Budgeting Pro — Architecture & Design

## Vision

A **general-purpose financial tracking platform** that works for any kind of
money management: personal finances, rental properties, small businesses,
freelancers, nonprofits — anything. Built as an MCP server so AI agents can
interact with it natively. Plaid-powered for automatic bank syncing. Excel and
other formats as outputs.

The core insight: **budget tracking is money flowing between entities across
time, tagged with meaning.** Everything else is configuration.

---

## Core Abstractions

### 1. Account (User)
A person or organization using the platform. Has credentials, notification
preferences, and owns everything below.

### 2. Bank Connection
A linked financial institution via Plaid. One user can connect many banks.
Each connection contains one or more **Bank Accounts** (checking, savings,
credit card, investment, etc.).

### 3. Ledger
The central abstraction. A **Ledger** is any named context that money flows
through. Think of it as a "view" into your finances.

```
Ledger {
  id, user_id
  name          (e.g. "Personal", "Rental Property A", "My Business")
  type          income_expense | balance_sheet
  currency      default USD/CAD/etc.
  line_items[]  user-defined rows
  color, icon   for UI
}
```

**Examples of Ledgers:**
| User Type | Their Ledgers |
|---|---|
| Individual | Personal Expenses, Savings Goals |
| Landlord | Property A, Property B, Personal |
| Freelancer | Client Income, Business Expenses, Tax Reserve |
| Small Business | Revenue, COGS, Operating Costs, Payroll |
| Nonprofit | Donations, Program Expenses, Admin |

### 4. Line Item
A named row within a Ledger. User-defined. Examples:
- In a "Personal" ledger: Groceries, Dining, Rent, Subscriptions
- In a "Rental Property" ledger: Tenant Rent, Mortgage, Insurance, Utilities

```
LineItem {
  id, ledger_id
  name          (e.g. "Groceries", "Mortgage")
  item_type     income | expense | transfer
  expected_amount   optional budget target
  sort_order
}
```

### 5. Transaction
A single financial event pulled from a bank. Raw — not yet interpreted.

```
Transaction {
  id, user_id, bank_account_id
  plaid_transaction_id
  date, merchant, amount, currency
  plaid_category[]      from Plaid
  pending
  created_at
}
```

### 6. Transaction Entry
The **interpreted** form of a transaction: assigned to a Ledger + Line Item,
with an amount (allows splits). One transaction → one or more entries.

```
TransactionEntry {
  id, transaction_id
  ledger_id, line_item_id
  amount                  (may be fraction if split)
  note
  reviewed_by_user        bool
}
```

This split model is the key to generality. A shared credit card purchase can
route 60% to "Personal / Dining" and 40% to "Business / Client Entertainment"
in one operation.

### 7. Routing Rule
Describes how to automatically classify and route incoming transactions.
Rules are evaluated in priority order; first match wins (or partial matches
accumulate to 100%).

```
RoutingRule {
  id, user_id
  priority
  match {
    merchant_pattern    regex or substring
    amount_min/max      optional range
    account_ids[]       limit to specific bank accounts
    plaid_categories[]  match Plaid category tags
    date_pattern        e.g. "day_of_month:1" for recurring
  }
  actions[] {
    ledger_id, line_item_id
    allocation_pct      (all rules in a transaction must sum to 100%)
    note_template       e.g. "Auto: {{merchant}}"
  }
}
```

### 8. Budget Target (optional)
An expected amount for a Line Item in a given period. Used to show
over/under budget in reports.

```
BudgetTarget {
  id, line_item_id
  period_type   monthly | quarterly | annual | one_time
  amount
  start_date, end_date
}
```

---

## Database Schema

```sql
-- Users
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT UNIQUE,
  notification_channel TEXT,  -- 'imessage', 'telegram', 'email', etc.
  notification_target TEXT,   -- phone/email/chat_id
  created_at INTEGER
);

-- Plaid bank connections (one per institution login)
CREATE TABLE bank_connections (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  plaid_item_id TEXT UNIQUE,
  plaid_access_token TEXT NOT NULL,  -- stored encrypted
  institution_id TEXT,
  institution_name TEXT,
  status TEXT DEFAULT 'active',      -- active | needs_reauth | disconnected
  created_at INTEGER,
  last_synced_at INTEGER
);

-- Individual accounts within a connection (checking, credit, etc.)
CREATE TABLE bank_accounts (
  id TEXT PRIMARY KEY,
  connection_id TEXT REFERENCES bank_connections(id),
  plaid_account_id TEXT UNIQUE,
  name TEXT,
  official_name TEXT,
  type TEXT,        -- depository | credit | loan | investment
  subtype TEXT,     -- checking | savings | credit card | mortgage | etc.
  mask TEXT,        -- last 4 digits
  currency TEXT DEFAULT 'USD'
);

-- User-defined tracking contexts
CREATE TABLE ledgers (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  name TEXT NOT NULL,
  type TEXT DEFAULT 'income_expense',  -- income_expense | balance_sheet
  currency TEXT DEFAULT 'USD',
  color TEXT,
  icon TEXT,
  sort_order INTEGER,
  created_at INTEGER
);

-- Named rows within a ledger
CREATE TABLE line_items (
  id TEXT PRIMARY KEY,
  ledger_id TEXT REFERENCES ledgers(id),
  name TEXT NOT NULL,
  item_type TEXT DEFAULT 'expense',  -- income | expense | transfer
  expected_amount REAL,
  sort_order INTEGER
);

-- Raw transactions from Plaid (uninterpreted)
CREATE TABLE transactions (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  bank_account_id TEXT REFERENCES bank_accounts(id),
  plaid_transaction_id TEXT UNIQUE,
  date TEXT NOT NULL,
  merchant TEXT,
  amount REAL NOT NULL,
  currency TEXT DEFAULT 'USD',
  plaid_category TEXT,     -- JSON array from Plaid
  pending INTEGER DEFAULT 0,
  created_at INTEGER
);

-- Interpreted + routed form of a transaction (supports splits)
CREATE TABLE transaction_entries (
  id TEXT PRIMARY KEY,
  transaction_id TEXT REFERENCES transactions(id),
  ledger_id TEXT REFERENCES ledgers(id),
  line_item_id TEXT REFERENCES line_items(id),
  amount REAL NOT NULL,
  note TEXT,
  reviewed INTEGER DEFAULT 0,  -- 1 = user confirmed
  created_at INTEGER
);

-- Auto-routing rules
CREATE TABLE routing_rules (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  priority INTEGER DEFAULT 100,
  merchant_pattern TEXT,
  amount_min REAL,
  amount_max REAL,
  account_ids TEXT,       -- JSON array of bank_account ids
  plaid_categories TEXT,  -- JSON array
  date_pattern TEXT,
  actions TEXT NOT NULL,  -- JSON: [{ledger_id, line_item_id, pct, note_template}]
  created_at INTEGER
);

-- Optional budget targets
CREATE TABLE budget_targets (
  id TEXT PRIMARY KEY,
  line_item_id TEXT REFERENCES line_items(id),
  period_type TEXT DEFAULT 'monthly',
  amount REAL NOT NULL,
  start_date TEXT,
  end_date TEXT
);

-- Plaid sync cursors (one per bank connection)
CREATE TABLE sync_cursors (
  connection_id TEXT PRIMARY KEY REFERENCES bank_connections(id),
  cursor TEXT,
  last_synced_at INTEGER
);
```

---

## MCP Server — Full Tool Catalog

### Account & Setup
| Tool | Description |
|---|---|
| `create_account` | Register a new user |
| `get_account` | Get account info |
| `update_notifications` | Set notification channel + target |

### Bank Connections
| Tool | Description |
|---|---|
| `create_link_token` | Start Plaid Link flow → returns link_token |
| `connect_bank` | Exchange public_token → store access_token |
| `list_connections` | All connected banks |
| `get_connection_status` | Check if a connection is healthy |
| `refresh_connection` | Trigger Plaid Update Mode for broken connection |
| `disconnect_bank` | Remove a bank connection |

### Ledgers & Line Items
| Tool | Description |
|---|---|
| `create_ledger` | Create a new ledger |
| `list_ledgers` | All ledgers for user |
| `update_ledger` | Rename/reconfigure a ledger |
| `delete_ledger` | Remove a ledger |
| `add_line_item` | Add a line item to a ledger |
| `list_line_items` | All line items for a ledger |
| `update_line_item` | Rename/reconfigure a line item |
| `remove_line_item` | Remove a line item |

### Routing Rules
| Tool | Description |
|---|---|
| `create_routing_rule` | Define a new auto-routing rule |
| `list_routing_rules` | All rules, ordered by priority |
| `update_routing_rule` | Modify a rule |
| `delete_routing_rule` | Remove a rule |
| `test_routing_rule` | Dry-run a rule against recent transactions |

### Transactions
| Tool | Description |
|---|---|
| `sync_transactions` | Pull latest from all connected banks |
| `list_transactions` | Query transactions (date, ledger, line item, reviewed) |
| `get_unrouted` | Transactions with no entries yet |
| `route_transaction` | Manually assign a transaction to ledger/line item(s) |
| `split_transaction` | Split one transaction across multiple ledger/line items |
| `confirm_routing` | Mark an auto-routed entry as reviewed |

### Reports & Export
| Tool | Description |
|---|---|
| `get_summary` | Totals by ledger/line item for a period |
| `get_monthly_breakdown` | Month-by-month table for a ledger |
| `get_budget_vs_actual` | Compare spending to budget targets if set |
| `export_excel` | Generate Excel workbook(s) |
| `export_csv` | Export transactions as CSV |

---

## Routing Engine — How It Works

```
New transaction arrives
    │
    ▼
For each RoutingRule (ordered by priority):
    - Does this transaction match? (merchant, amount, account, category, date)
    - If yes: apply actions (allocate % to ledger/line item)
    - If total allocation reaches 100%: stop
    │
    ▼
If 0% matched:    → mark as "unrouted", notify user
If 1-99% matched: → apply partial routing, flag remainder for review
If 100% matched:  → fully auto-routed (may still flag for review if rule is new)
```

**Rule priority cascade:**
1. Exact merchant match (highest confidence)
2. Plaid category match
3. Amount range + account match
4. Date pattern (e.g. recurring on 1st of month)
5. Catch-all rules (lowest priority)

---

## Excel Export Format

`export_excel(ledger_id, years=[])` generates one workbook per ledger:

- **Sheet per year** — rows = line items, columns = Jan–Dec + YTD
  - Income section (green)
  - Expense section (red)
  - Net row (bold)
- **Summary sheet** — all years side by side, YTD column per year
- **Raw Transactions sheet** — every transaction for that ledger, filterable

---

## Re-auth Flow

```
sync_transactions detects ITEM_LOGIN_REQUIRED
    → connection.status = 'needs_reauth'
    → notify user via their configured channel
    → user calls refresh_connection(connection_id)
    → returns new Link token in Update Mode
    → user re-authenticates in Link UI
    → sync resumes, same access token retained
```

---

## Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Best data ecosystem |
| MCP framework | FastMCP | Clean, minimal MCP server in Python |
| Database | SQLite | Zero-config, file-based, portable |
| Plaid | plaid-python | Official SDK |
| Excel | openpyxl | Full xlsx read/write, no Excel required |
| Link UI | Plain HTML + Plaid.js | No framework dependency |
| Token encryption | cryptography (Fernet) | Encrypt access tokens at rest |

---

## Project Structure

```
friday-budgeting-pro/
├── README.md
├── PLAN.md
├── requirements.txt
├── .env.example
├── .gitignore
│
├── db/
│   ├── schema.sql
│   └── database.py          ← connection + helpers
│
├── mcp_server/
│   ├── server.py            ← FastMCP entry point
│   └── tools/
│       ├── account.py
│       ├── banks.py
│       ├── ledgers.py
│       ├── routing.py
│       ├── transactions.py
│       └── export.py
│
├── plaid/
│   ├── client.py            ← Plaid API wrapper
│   ├── link_ui/
│   │   └── index.html       ← Plaid Link web UI
│   └── sync.py              ← transaction sync logic
│
├── engine/
│   ├── router.py            ← routing rules engine
│   └── categorizer.py       ← Plaid category → line item mapping
│
├── export/
│   ├── excel.py             ← Excel workbook generator
│   └── csv_export.py
│
└── notify/
    └── notifier.py          ← pluggable notification system
```

---

## Setup Flow (Generic)

1. `cp .env.example .env` → add your Plaid credentials
2. `python db/schema.sql` → initialize database
3. `python mcp_server/server.py` → start MCP server
4. Via MCP: `create_account` → register
5. Via MCP: `create_link_token` → open Link UI → connect banks
6. Via MCP: `create_ledger` × N → define your tracking contexts
7. Via MCP: `add_line_item` × N → define rows per ledger
8. Via MCP: `create_routing_rule` × N → teach it how to route
9. Via MCP: `sync_transactions` → pull transaction history
10. Via MCP: `get_unrouted` → review anything that didn't auto-route
11. Via MCP: `export_excel` → get your spreadsheet

---

## Extensibility Notes

- **Multiple currencies:** `currency` field on Ledger + Transaction; FX conversion layer can be added later
- **Multiple users:** schema is fully multi-tenant from day one
- **Recurring detection:** `date_pattern` in routing rules handles subscriptions and fixed monthly costs
- **Investments:** `balance_sheet` ledger type can track asset values over time
- **Dollar's account:** add her bank connection to the same account → her transactions route through the same ledgers and rules

---

## Status
- [x] Architecture designed
- [x] Database schema finalized
- [x] MCP tool catalog defined
- [x] Repo: https://github.com/Riddy21/Friday_Budgeting_Pro
- [ ] Initialize DB + schema
- [ ] MCP server skeleton
- [ ] Plaid integration (link UI + sync)
- [ ] Routing engine
- [ ] Excel export
- [ ] Notification system
- [ ] README + setup docs
- [ ] End-to-end test
