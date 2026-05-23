# Friday Budgeting Pro — Project Plan

## Overview
A reusable, multi-user personal finance tracking platform with:
- **Plaid integration** for automatic bank transaction syncing
- **MCP server** so AI agents can interact with financial data via tools
- **Vertical tracking** — user-defined buckets (e.g. Personal, Rental Property, Business)
- **Auto-categorization** with a learning rules engine
- **Multiple output formats** including Excel, CSV, and API queries

Designed to be generic and reusable — anyone can spin up an instance, create an account, connect their banks, and start tracking.

---

## Core Concepts

| Concept | Description |
|---|---|
| **Account** | A user on the platform |
| **Bank Connection** | A Plaid Item linked to an account (multiple per user) |
| **Vertical** | A user-defined tracking bucket (e.g. "Personal", "Rental Property A", "Business") |
| **Transaction** | A bank transaction, auto-categorized and assigned to a vertical |
| **Category** | A label on a transaction (groceries, dining, rent, income, etc.) |
| **Category Rule** | A saved merchant → category mapping so the same merchant is never asked about twice |

---

## Database Schema (SQLite)

```
users
  id, name, email, created_at

bank_connections
  id, user_id, plaid_access_token, plaid_item_id, institution_name, status, created_at

bank_accounts
  id, connection_id, plaid_account_id, name, type, subtype, mask

verticals
  id, user_id, name, description, color, created_at

transactions
  id, user_id, account_id, plaid_transaction_id, date, merchant,
  amount, plaid_category, category, vertical_id, notes, pending, created_at

category_rules
  id, user_id, merchant_pattern, category, vertical_id, created_at

sync_cursors
  id, connection_id, cursor, last_synced_at
```

---

## MCP Server — Tools

The MCP server exposes the following tools to any connected AI agent:

### Setup & Auth
| Tool | Description |
|---|---|
| `create_account` | Create a new user account |
| `start_bank_link` | Generate a Plaid Link token to connect a bank |
| `complete_bank_link` | Exchange public token → access token, store connection |
| `list_bank_connections` | List all connected banks for a user |
| `refresh_bank_connection` | Trigger Plaid Update Mode for a broken connection |
| `remove_bank_connection` | Disconnect a bank |

### Verticals
| Tool | Description |
|---|---|
| `create_vertical` | Create a new tracking vertical |
| `list_verticals` | List all verticals for a user |
| `update_vertical` | Rename or update a vertical |
| `delete_vertical` | Remove a vertical |

### Transactions
| Tool | Description |
|---|---|
| `sync_transactions` | Pull latest transactions from all connected banks |
| `list_transactions` | Query transactions (by date, vertical, category, account) |
| `categorize_transaction` | Manually set category + vertical on a transaction |
| `get_uncategorized` | List transactions that need human review |
| `get_summary` | Spending summary by category/vertical/month |

### Export
| Tool | Description |
|---|---|
| `export_excel` | Generate Excel workbook with one sheet per vertical per year |
| `export_csv` | Export transactions as CSV |

---

## Auto-Categorization Engine

1. Check `category_rules` for a matching merchant pattern → apply rule
2. Fall back to Plaid's built-in category tags
3. If still ambiguous → flag as `needs_review`
4. Agent calls `get_uncategorized` and asks the user to clarify
5. User reply → `categorize_transaction` → rule saved → never asked again

### Built-in Categories
`income` `rent_income` `rent_expense` `groceries` `dining` `transport`
`travel` `shopping` `subscriptions` `healthcare` `utilities`
`property_expense` `insurance` `mortgage` `tax` `misc`

---

## Excel Export Format

One workbook per vertical (e.g. `Personal Finances.xlsx`, `Rental Property A.xlsx`)

Each workbook:
- **One sheet per year** (e.g. 2024, 2025, 2026)
- **Summary sheet** — all years side by side
- Rows = categories | Columns = Jan–Dec + YTD Total
- Bottom rows: Total In, Total Out, Net

---

## Re-auth Strategy

When Plaid returns `ITEM_LOGIN_REQUIRED`:
1. Mark connection status as `needs_reauth` in DB
2. Notify user (via whatever channel they have configured)
3. User calls `refresh_bank_connection` → generates Update Mode Link token
4. User re-authenticates → connection restored, same access token

---

## Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| Language | Python | Best data/Excel tooling |
| Database | SQLite | Zero-config, portable, file-based |
| MCP framework | FastMCP | Clean Python MCP server |
| Plaid | plaid-python | Official SDK |
| Excel | openpyxl | Full xlsx read/write |
| Link UI | HTML + Plaid.js | Simple setup page |

---

## Project Structure

```
friday-budgeting-pro/
├── README.md
├── PLAN.md
├── requirements.txt
├── .env.example             ← template: PLAID_CLIENT_ID, PLAID_SECRET, etc.
├── .gitignore               ← ignores .env, *.db, tokens
│
├── db/
│   ├── schema.sql           ← database schema
│   └── migrations/          ← future schema changes
│
├── mcp_server/
│   ├── server.py            ← FastMCP server entry point
│   ├── tools/
│   │   ├── auth.py          ← create_account, bank link tools
│   │   ├── verticals.py     ← vertical CRUD tools
│   │   ├── transactions.py  ← sync, list, categorize tools
│   │   └── export.py        ← Excel/CSV export tools
│   └── plaid_client.py      ← Plaid API wrapper
│
├── categorizer/
│   ├── engine.py            ← auto-categorization logic
│   └── rules.py             ← rule matching + saving
│
├── sync/
│   └── sync.py              ← standalone sync runner (for cron)
│
└── link_ui/
    └── index.html           ← Plaid Link web UI for bank setup
```

---

## Setup Flow (for any user)

1. Clone repo, copy `.env.example` → `.env`, add Plaid credentials
2. Run `python db/schema.sql` to initialize the database
3. Start the MCP server: `python mcp_server/server.py`
4. Call `create_account` tool to register
5. Call `start_bank_link` → open the link UI → connect banks
6. Call `sync_transactions` to pull initial history
7. Create verticals to match your life (Personal, Rental, Business, etc.)
8. Let auto-categorization run; answer any `get_uncategorized` prompts
9. Call `export_excel` whenever you want a spreadsheet

---

## Status
- [x] Concept + architecture designed
- [x] Repo created: https://github.com/Riddy21/Friday_Budgeting_Pro
- [ ] Initialize database schema
- [ ] Build MCP server skeleton + tools
- [ ] Build Plaid integration (link UI + sync)
- [ ] Build auto-categorization engine
- [ ] Build Excel export
- [ ] Write README + setup docs
- [ ] End-to-end test
