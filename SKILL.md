---
name: friday-budgeting-pro
description: >
  AI-powered personal finance tracker. Connects to your banks via Plaid,
  auto-classifies transactions, syncs daily, and exports to Excel. Supports
  multiple ledgers including personal household, rental properties, and
  investments. Ask your agent about spending, connect banks, manage ledgers,
  classify transactions, or trigger exports.
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
              "command": "python3 -m server.installer install",
              "label": "Install daemon (launchd plist + OpenClaw MCP registration)",
            },
          ],
        "uninstall":
          [
            {
              "id": "launchd-remove",
              "kind": "shell",
              "command": "python3 -m server.installer uninstall",
              "label": "Remove daemon",
            },
          ],
      },
  }
---

# Friday Budgeting Pro

AI-powered personal finance on your own Mac. Connects to your banks via Plaid,
classifies transactions automatically (and asks when unsure), syncs in the
background, and exports to Excel. Supports personal, rental property, and
investment ledgers. A small local UI handles setup and management; everything
else happens through your agent.

## Setup

After install, open `http://127.0.0.1:6789` in your browser to:
1. Set a password for the local dashboard
2. Connect your first bank via Plaid
3. Done — daily sync runs automatically via launchd

## When to Use This Skill

Invoke for any personal finance request:

- Spending summaries ("how much did I spend on dining this month?")
- Bank connections ("connect my TD account", "reconnect my BMO")
- Transaction queries ("what was that $47 Amazon charge?")
- Classification ("mark that Home Depot charge as rental property maintenance")
- Corrections ("that Uber on Friday was a work trip")
- Exports ("export my finances to Excel")
- Sync ("sync my transactions")
- Ledger management ("add a rental property ledger", "show my property income")
- Rules ("add a rule that Wealthsimple transfers are savings")
- Settings ("set my home currency to CAD", "what timezone am I using?")

## Available MCP Tools

### Profiles
- `list_profiles` — list all local user profiles (usernames)

### Setup
- `setup_status` — check if first-run setup is complete (`not_started | in_progress | complete`)
- `apply_initial_setup(banks_to_link, rental_properties?, investment_account_ids?, extra_ledgers?, hints?)` — initialize ledgers, notifications, and first sync in one call

### Banks
- `start_link(plaid_env?)` — generate Plaid Link URL to connect a bank
- `complete_link(public_token, plaid_env?)` — exchange public token after user completes Plaid Link
- `list_connections` — list connected banks and their status (`active | needs_reauth`)
- `get_connections_needing_attention` — list connections that need user action (reauth or expiring soon)
- `refresh_connection(id)` — re-authenticate a broken connection (Update Mode)
- `disconnect(id)` — remove a bank connection and its data
- `set_account_description(account_id, description)` — set classifier context for an account (e.g. "Primary spending account")

### Ledgers
- `list_ledgers` — show all ledgers (personal/property/investment) and their line items
- `add_ledger(name)` — create a new ledger
- `add_line_item(ledger_id, name, item_type)` — add a line item (`income | expense`) to a ledger
- `remove_line_item(id)` — remove a line item
- `set_account_ledger(account_id, ledger_id)` — link a bank account to a default ledger for automatic routing
- `create_property_ledger(name, description?)` — create a property ledger with default line items (Rent income, Mortgage, Property tax, Maintenance, Insurance, Utilities)
- `create_investment_ledger(name)` — create an investment ledger (Contributions, Dividends/Returns)

### Transactions
- `sync` — pull latest transactions from all connected banks
- `list(filters?)` — query transactions (supports date, ledger, category, account filters)
- `get_needs_review` — transactions awaiting classification or flagged as uncertain
- `route(transaction_id, allocations)` — manually assign a transaction to a ledger/line item
- `add_hint(text)` — add a natural-language classification hint for the LLM
- `list_hints` — list all classification hints
- `remove_hint(id)` — remove a classification hint

### Classification Rules
- `list_rules` — list all classification rules sorted by priority (lower = evaluated first)
- `add_rule(name, description, rule_type, line_item_id?, priority?)` — add a natural-language rule (`transfer | savings | spending | income | skip`)
- `update_rule(id, **fields)` — update a rule's name, description, type, priority, or enabled state
- `reorder_rules(ids)` — set new priority order by passing an ordered list of rule IDs
- `disable_rule(id)` — disable a rule (skipped during classification)
- `enable_rule(id)` — re-enable a disabled rule
- `delete_rule(id)` — delete a user-created rule (default rules cannot be deleted, only disabled)
- `list_auto_promoted_rules` — list auto-promoted routing rules with audit metadata
- `undo_auto_promoted_rule(rule_id)` — revert an auto-promoted rule and its affected entries

### Reports
- `summary(period)` — spending totals by category for a period (e.g. `this_month`, `last_month`)
- `export_excel(years?)` — generate Excel workbook and return download URL

### Settings
- `get_setting(key)` — get an app setting (e.g. `home_currency`, `timezone`, `notification_channel`)
- `set_setting(key, value)` — update an app setting

### UI & Auth
- `get_ui_url(page?)` — return the local dashboard URL, optionally deep-linked to a page
- `set_ui_password(current_password, new_password)` — change the UI login password
- `reset_ui_password` — generate a password-reset recovery token
- `configure_plaid(client_id, secret, env)` — update Plaid API credentials

## Do / Don't

**Do**
- Always use the MCP tools — never guess from general knowledge
- Call `sync` before answering spending questions if data may be stale
- Use `get_needs_review` periodically and walk the user through uncertain classifications
- Use `list_rules` to show what classification rules are active before adding new ones
- Open `start_link` when the user wants to connect or reconnect a bank
- Use `create_property_ledger` for rental properties — it seeds the right line items automatically
- Respect that all data is local and private

**Don't**
- Don't answer general finance questions ("what is inflation?") — this skill is for personal accounts only
- Don't store tokens or credentials in plain text
- Don't expose DB paths, encryption keys, or internal implementation details
- Don't try to open the Plaid UI yourself — return the URL and let the user click
- Don't call `delete_rule` on default rules — they can only be disabled
