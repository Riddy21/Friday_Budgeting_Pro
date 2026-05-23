---
name: friday-budgeting-pro
description: >
  An OpenClaw skill that lets the user manage personal finances by chatting with
  HAL. Connects to banks via Plaid, auto-classifies transactions using a tiered
  engine (rules → LLM → ask user), stores everything in a local SQLite file,
  exports to Excel on request, and runs a daily sync. The user interacts
  entirely through natural-language chat — no UI, no commands, no config files.
version: "0.1"
---

# Friday Budgeting Pro

## When to Use This Skill

Invoke this skill when the user's message contains finance-related intent. Key
trigger keywords include:

**Banking / Accounts**
- banks, bank account, banking, connect bank, add bank, Plaid, institution
- balance, account balance, checking, savings, credit card

**Transactions / Spending**
- transactions, transaction, spending, charges, purchases
- expenses, expense, expenditure
- income, salary, paycheck, deposit

**Budgeting / Tracking**
- budget, budgeting, budget report
- ledger, line item, category, categorize, classify
- monthly report, weekly summary, spending summary

**Actions / Exports**
- export, Excel, spreadsheet, download
- sync, refresh, update transactions
- re-auth, reconnect bank

## Example Trigger Phrases

- "How much did I spend on groceries this month?"
- "Connect my TD Bank account."
- "Show me my transactions from last week."
- "What's my budget looking like?"
- "Export my expenses to Excel."
- "Sync my bank transactions."
- "I spent a lot on dining out — can you show me a breakdown?"
- "What was that $47 charge from Amazon?"
- "Add a rule: Starbucks is always Dining."
- "How much income did I receive in April?"
- "My Plaid connection needs re-authorization."
- "Show me my top spending categories."

## Do / Don't

### ✅ Do

- Invoke this skill for any question about personal finances, spending, or
  banking.
- Call the Friday Budgeting Pro MCP tools to answer finance questions.
- Trigger conversational setup if this is the user's first time (DB empty).
- Notify the user when a bank connection needs re-authorization.
- Use the MCP `export` tool when the user asks for an Excel file.
- Classify ambiguous transactions by asking the user directly.
- Schedule or confirm the daily 6 AM sync via OpenClaw cron when requested.

### ❌ Don't

- Don't try to answer finance questions from general knowledge — always go
  through the MCP tools which read the local DB.
- Don't store or log Plaid tokens in plain text; the server handles encryption.
- Don't expose internal implementation details (DB paths, encryption keys) to
  the user.
- Don't handle multi-user or business finance scenarios — this is personal
  finance only.
- Don't open a web browser or direct the user to any external URL except via
  the Plaid Link flow managed by the MCP server.
- Don't invoke this skill for generic money questions unrelated to the user's
  own accounts (e.g., "what is inflation?").
