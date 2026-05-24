---
name: friday-budgeting-pro
description: >
  AI-powered personal finance tracker. Connects to your banks via Plaid,
  auto-classifies transactions, syncs daily, and exports to Excel. Ask your
  agent about spending, connect banks, manage ledgers, or trigger exports.
homepage: https://github.com/Riddy21/Friday_Budgeting_Pro
metadata:
  {
    "openclaw":
      {
        "emoji": "💰",
        "os": ["darwin"],
        "requires": { "bins": ["python3"] },
        "mcp":
          {
            "server": "friday-budgeting-pro",
            "transport": "stdio",
            "command": "python3",
            "args": ["-m", "server.main"],
          },
        "install":
          [
            {
              "id": "pip",
              "kind": "shell",
              "command": "pip3 install --break-system-packages -q -r requirements.txt",
              "label": "Install Python dependencies",
            },
            {
              "id": "db-init",
              "kind": "shell",
              "command": "python3 -c \"import server.db as d, server.paths as p; d.init_db(p.DB_PATH)\"",
              "label": "Initialize database",
            },
            {
              "id": "launchd",
              "kind": "shell",
              "command": "python3 -m server.installer",
              "label": "Install daemon (launchd)",
            },
          ],
        "uninstall":
          [
            {
              "id": "launchd-remove",
              "kind": "shell",
              "command": "launchctl bootout gui/$UID/ai.openclaw.friday-budgeting-pro 2>/dev/null; rm -f ~/Library/LaunchAgents/ai.openclaw.friday-budgeting-pro.plist",
              "label": "Remove daemon",
            },
          ],
      },
  }
---

# Friday Budgeting Pro

AI-powered personal finance on your own Mac. Connects to your banks via Plaid,
classifies transactions automatically (and asks when it's unsure), syncs in the
background, and exports to Excel. A small local UI at `http://127.0.0.1:6789`
handles setup and profile management; everything else happens through your agent.

## Setup

After install, open `http://127.0.0.1:6789` in your browser to:
1. Set a password for the local dashboard
2. Connect your first bank via Plaid
3. Done — daily sync scheduled automatically

## When to Use This Skill

Invoke for any personal finance request:

- Spending summaries ("how much did I spend on dining this month?")
- Bank connections ("connect my TD account", "reconnect my BMO")
- Transaction queries ("what was that $47 Amazon charge?")
- Classification ("mark Home Depot as rental property maintenance")
- Exports ("export my finances to Excel")
- Sync ("sync my transactions")
- Ledger management ("add a rental property ledger")

## Available MCP Tools

### Setup
- `setup_status` — check if first-run setup is complete
- `apply_initial_setup` — initialize ledgers, notifications, first sync

### Banks
- `start_link` — generate Plaid Link URL to connect a bank
- `complete_link` — exchange public token after user completes Link
- `list_connections` — list connected banks and their status
- `refresh_connection` — reauth a broken connection (Update Mode)
- `disconnect` — remove a bank connection

### Ledgers
- `list_ledgers` — show all ledgers and line items
- `add_ledger` — create a new ledger
- `add_line_item` — add a line item to a ledger

### Transactions
- `sync` — pull latest transactions from all banks
- `list` — query transactions (date, ledger, category filters)
- `get_needs_review` — transactions awaiting classification
- `route` — assign a transaction to a ledger/line item
- `add_hint` — add a natural-language classification hint

### Reports
- `summary` — spending totals for a period
- `export_excel` — generate Excel workbook(s)
- `get_ui_url` — return the local dashboard URL

## Do / Don't

**Do**
- Always use the MCP tools — never guess from general knowledge
- Call `sync` before answering spending questions if data may be stale
- Use `get_needs_review` periodically and walk the user through classifications
- Open `start_link` when the user wants to connect or reconnect a bank
- Respect that all data is local and private — don't mention paths or internals

**Don't**
- Don't answer "what is inflation?" type questions — invoke for personal accounts only
- Don't store tokens or credentials in plain text
- Don't expose DB paths, encryption keys, or implementation details to the user
- Don't try to open the Plaid UI yourself — return the URL and let the user click
