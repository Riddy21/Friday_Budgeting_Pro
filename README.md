# Friday Budgeting Pro

> Stop fighting your budget app. Talk to it.

A general-purpose financial tracker that handles the **real mess** of personal
finance — shared credit cards, mixed personal/rental/business spending, and the
edge cases that defeat traditional rules-based tools. Built as an MCP server
with an LLM-powered classification layer, so an AI agent (like OpenClaw's HAL)
can do the smart sorting for you.

📋 [Read the full architecture](./ARCHITECTURE.md) · 🗺️ [Read the design plan](./PLAN.md)

---

## What It Does

- 🏦 **Connects to your banks** via Plaid (Production)
- 🗂️ **Organizes your finances** into user-defined "ledgers" (Personal, Property A, Business — whatever you want)
- 🤖 **Auto-classifies transactions** using a three-tier engine:
  1. Deterministic rules (fast, free)
  2. LLM reasoning with your natural-language hints (smart)
  3. Asks you when ambiguous (only when needed)
- 📊 **Exports to Excel** with one sheet per year, plus summary views
- 💬 **Talks to you** through whatever channel you have HAL on — iMessage, Telegram, etc.
- 🧠 **Learns over time** — after a few corrections, similar transactions just work

---

## What Makes It Different

| Traditional budget apps | Friday Budgeting Pro |
|---|---|
| Regex/keyword rules | LLM reasoning with your hints |
| One transaction = one category | Splits transactions across ledgers |
| Asks the same question forever | Learns from every answer |
| Web UI | Native AI agent integration (MCP) |
| Locked to one structure | You define everything (ledgers, items, rules) |
| Closed ecosystem | Open source, your data in a SQLite file |

---

## Installation

### Option A: ClawHub (recommended)

If you're using OpenClaw:

```bash
clawhub install friday-budgeting-pro
friday-budgeting setup
```

That's it. The setup wizard handles everything: Plaid credentials, database
init, OpenClaw MCP registration, daily cron sync.

### Option B: Manual

```bash
git clone https://github.com/Riddy21/Friday_Budgeting_Pro.git
cd Friday_Budgeting_Pro
pip install -r requirements.txt
cp .env.example .env       # fill in your Plaid + LLM credentials
python -m db.init          # initialize the database
python -m mcp_server.server   # start the MCP server
```

Then point your MCP client at the server (see [docs/mcp-clients.md](./docs/mcp-clients.md)).

---

## Requirements

- **Plaid account** (Production tier for real banks, or Sandbox for testing) — get free at https://dashboard.plaid.com
- **An LLM provider** — pick one:
  - OpenAI API key
  - Anthropic API key
  - OpenClaw routing (if you're already on OpenClaw)
  - Local Ollama (free, runs on your machine)
- **Python 3.11+**
- **A way to receive notifications** — iMessage / Telegram / email (configured via OpenClaw if you use it)

---

## First-Time Setup Walkthrough

After install, the setup wizard walks you through this. But here's the
conceptual flow so you know what to expect:

### Step 1: Create your account

```
> friday-budgeting setup

Welcome to Friday Budgeting Pro 🚀

What's your name? Jamie Smith
Email? jamie@example.com
Notification channel? [imessage / telegram / email / none] imessage
Notification target (phone/chat-id/email)? +15551234567

✓ Account created
```

### Step 2: Configure your LLM provider

```
LLM provider for smart classification? [openai / anthropic / openclaw / ollama]
> openclaw

✓ Using OpenClaw routing (no API key needed)
```

### Step 3: Connect your first bank

```
Let's connect your first bank.

Opening Plaid Link UI at http://localhost:3333 ...
(complete the bank login in your browser)

✓ Connected: Chase (3 accounts: Checking, Credit, Savings)
```

### Step 4: Set up your ledgers

This is where the system becomes yours. Create a ledger for each "context"
you want to track separately.

```
Let's create your first ledger. What should we call it? Personal
What type? [income_expense / balance_sheet] income_expense

✓ Created ledger: Personal

Add some line items (rows in this ledger). Type 'done' when finished.
> Income::Salary (item_type: income)
> Income::Side gigs (income)
> Expense::Groceries
> Expense::Dining
> Expense::Rent
> Expense::Subscriptions
> done

✓ Added 6 line items to Personal

Add another ledger? [Y/n] y
> Rental Property — 103 Ardsley
> Add items: Tenant Rent (income), Mortgage, Property Tax, Insurance,
            Utilities, Maintenance
> done

Add another? [Y/n] n
```

### Step 5: Teach it how you think

Give the LLM natural-language hints about how to classify things.

```
Now teach the system about your habits. Write hints in plain English:

> Tim Hortons is always personal.
> Home Depot under $50 is personal; over $50 is usually rental maintenance.
> Anything with "STRATA" or "PROPERTY MGMT" is the rental.
> Grocery stores in London Ontario are for the rental property snacks.
> done

✓ Saved 4 classification hints
```

### Step 6: Pull your transactions

```
Syncing transactions from all connected banks...

✓ Synced 247 transactions
  - 198 auto-routed by rules
  - 41 auto-routed by LLM (avg confidence 0.89)
  - 8 need your review

You can review the 8 unclear transactions now, or HAL will ask you about
them later. Review now? [Y/n] y
```

### Step 7: Review the ambiguous ones

```
Transaction 1 of 8:
  Date: 2026-05-14
  Merchant: HOME DEPOT #3847
  Amount: $127.45
  Account: Chase Credit
  Plaid category: Hardware / Home Improvement

  My guess: Rental Property — 103 Ardsley / Maintenance (confidence: 0.71)
  Reasoning: "Amount > $50 matches your hint about Home Depot;
              no recent personal projects logged"

  [a] Accept the guess
  [c] Choose a different ledger/item
  [s] Split between multiple ledgers
  [k] Keep unrouted for now

> a
✓ Routed to Rental Property — 103 Ardsley / Maintenance
  Created auto-rule for "HOME DEPOT" → 103 Ardsley / Maintenance
```

### Step 8: Set up auto-sync

```
Schedule daily sync? [Y/n] y
What time? [9am] 6am

✓ Cron job installed. Will sync every day at 6:00 AM.

You're all set! Try asking HAL:
  - "Show me this month's spending"
  - "How much did I spend on dining this year?"
  - "Export my Personal ledger to Excel"
```

---

## Daily Usage

You don't run commands. You talk to HAL.

### Quick balance check
```
You: "What's my spending this month?"

HAL: "Here's the breakdown for May 2026:

      Personal:        $3,247  (under budget by $253)
      103 Ardsley:     $2,890  (income $4,200 — net +$1,310)
      90 Glen Everest: $1,950  (income $2,100 — net +$150)

      Total cash flow: +$1,213 this month"
```

### Categorizing tricky transactions
```
HAL (proactively): "Got a $312 charge at Costco yesterday. My guess is
                    personal/groceries (75% confidence) — but it could
                    also be supplies for one of the rentals. Which is it?"

You: "Half personal, half for 103 Ardsley — bulk paper towels for tenants"

HAL: "Got it — split 50/50. Saved as a hint for next time."
```

### Exporting
```
You: "Export everything to Excel"

HAL: "Done. Created 3 workbooks in your Finances folder:
       - Personal Finances.xlsx
       - 103 Ardsley Finances.xlsx
       - 90 Glen Everest Finances.xlsx

      Each has a sheet per year plus a Summary view."
```

### Querying
```
You: "How much did I spend on dining last year?"

HAL: "In 2025, you spent $4,182 on dining. Highest months were
      December ($612) and August ($573). Average $349/month."
```

### Adding a new bank
```
You: "I want to add Dollar's credit card"

HAL: "Opening Plaid Link... [waits for you to complete the flow]
      Connected Amex (1 account: Cobalt Card).
      Should new transactions from this card route the same way
      as your other personal accounts? [Y/n]"

You: "Yes, but flag anything over $200 for review"

HAL: "Added that as a classification hint. Done."
```

### Re-auth (when a bank disconnects)
```
HAL (alerts you): "Heads up — your BMO connection needs re-authentication.
                   They probably rotated credentials. Want me to open the
                   update flow?"

You: "Yes"

HAL: "Opening update mode at http://localhost:3333. Re-enter your
      credentials and you're back in business."
```

---

## Concepts You Should Know

### Ledger
A "view" into your finances. You decide what these are.

| You might have | Other people might have |
|---|---|
| Personal | Donations |
| Rental Property A | Program Expenses |
| Rental Property B | Admin |
| Business | Client Income |
| Tax Reserve | Operating Costs |

### Line Item
A row inside a ledger. Like "Groceries" or "Mortgage" or "Tenant Rent".

### Routing Rule (Tier 1)
A deterministic rule. Fast, free, exact. Examples:
- "Any transaction with 'NETFLIX' → Personal / Subscriptions"
- "Any deposit > $2000 to checking on the 15th → Personal / Salary"

These get created automatically as the system learns.

### Classification Hint (Tier 2)
Natural language guidance for the LLM. Examples:
- "Home Depot under $50 is personal"
- "Anything in London, Ontario is the rental property"
- "Dollar's spending is always shared/personal"

The LLM reads these every time it has to classify an ambiguous transaction.

### Split Transaction
One transaction can be assigned to multiple ledgers/line items.
Example: a $200 Costco run that's 60% personal groceries, 40% supplies for
the rental — one transaction, two entries.

---

## Excel Output

Each ledger gets its own workbook. Structure:

**Sheet: 2026** (one sheet per year)

|              | Jan   | Feb   | Mar   | ... | Dec   | YTD    |
|--------------|-------|-------|-------|-----|-------|--------|
| **INCOME**   |       |       |       |     |       |        |
| Salary       | 6500  | 6500  | 6500  | ... | 6500  | 78,000 |
| Side gigs    | 200   |       | 450   | ... |       | 1,250  |
| **EXPENSES** |       |       |       |     |       |        |
| Rent         | 2100  | 2100  | 2100  | ... | 2100  | 25,200 |
| Groceries    | 487   | 521   | 463   | ... | 502   | 6,012  |
| Dining       | 312   | 287   | 401   | ... | 348   | 4,182  |
| ...          |       |       |       |     |       |        |
| **NET**      | +2890 | +3100 | +2750 | ... | +3270 | +37,200|

**Sheet: Summary** — same structure but with one column per year side-by-side.

**Sheet: Raw Transactions** — every transaction with its assignment, filterable.

---

## Privacy & Security

- 🔒 **Plaid access tokens encrypted at rest** using Fernet
- 🚫 **No telemetry** — nothing phones home
- 📁 **Your data lives in a SQLite file** you control (`~/.friday-bp/data.db`)
- 🌐 **Pluggable LLM** — pick one that matches your privacy needs:
  - Local Ollama → nothing leaves your machine
  - OpenClaw / OpenAI / Anthropic → standard API privacy terms apply
- 🔐 **Plaid Link UI runs locally** at `localhost:3333` — bank credentials never touch this app
- 🙅 **No personal info in this repo** — `.env`, DB files, tokens all gitignored

---

## Troubleshooting

### "Bank connection broken — needs reauth"
Your bank rotated something. Just say "reconnect my [bank name]" and HAL will
open the Plaid Update Mode flow.

### "LLM keeps asking me about the same merchant"
That shouldn't happen — after 3 correct LLM classifications, the system auto-
promotes the merchant to a Tier 1 rule. If it's still asking, check:
- Are you giving consistent answers?
- Is the merchant name varying (e.g. "WALMART #123" vs "WALMART #456")?
  → Add a hint: "Anything starting with WALMART is personal groceries"

### "Excel export is missing transactions"
Check the date range. Default is YTD. Specify: "Export everything from 2024
through 2026."

### "I want to redo a classification"
Just tell HAL: "Reclassify the Home Depot transaction from May 14 as personal."

### "Where's my data?"
```
~/.friday-bp/data.db           ← your transactions, ledgers, rules
~/.friday-bp/tokens.enc        ← encrypted Plaid tokens
~/.friday-bp/exports/          ← Excel files
```

Back these up. Restore = drop into place.

---

## FAQ

**Q: Can I use this without OpenClaw?**
A: Yes. It's a standard MCP server. Use it with Claude Desktop, Cursor, any
   MCP-compatible client. OpenClaw just makes it nicer because of the
   notification integration.

**Q: Does it work with banks outside the US/Canada?**
A: Wherever Plaid works. Currently 60+ countries with varying coverage. Check
   [plaid.com/global](https://plaid.com/global).

**Q: What about cash transactions?**
A: Use `route_transaction` to add manual entries. They live in the DB just
   like Plaid-sourced ones.

**Q: Can my partner and I share this?**
A: Two ways: (1) one account, connect both your banks to it (recommended for
   couples sharing finances). (2) Two separate accounts on the same instance
   if you want true separation.

**Q: How much does it cost to run?**
A: 
- **Plaid:** Free up to 200 Items in Production (more than enough for individuals)
- **LLM:** ~$0.001 per ambiguous transaction with Anthropic. Maybe $1-2/month for typical use.
- **Server:** Runs on your machine. Free.

**Q: Can I export my data and leave?**
A: Yes. SQLite file is portable, plus you can export CSV/Excel any time.
   Disconnect the Plaid integration when you're done.

**Q: What if I don't want LLM classification?**
A: Set `llm_confidence_threshold = 1.1` and only Tier 1 rules will fire.
   Everything else goes straight to Tier 3 (you).

---

## Roadmap

- [ ] Investment tracking (balance sheet ledgers)
- [ ] Multi-currency with FX conversion
- [ ] Tax categorization reports
- [ ] Mobile-friendly Link UI
- [ ] Non-Plaid sources (Wealthsimple, crypto, manual import)
- [ ] Spending forecasts / budget recommendations
- [ ] Web dashboard (read-only)

---

## Contributing

Issues and PRs welcome at https://github.com/Riddy21/Friday_Budgeting_Pro.

---

## License

MIT
