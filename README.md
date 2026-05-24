# Friday Budgeting Pro

> AI-powered personal finance, on your own machine.

A local budgeting tool that uses AI to do the boring work for you: connecting
to your banks, classifying every transaction, and keeping a spreadsheet
up to date. It runs as a small daemon on your Mac with **multiple equal
ways to interact with it** - no single one is the "main" way.

**Local profiles. Local-only. AI does the heavy lifting; you stay in control.**

Supports multiple named local profiles — each with their own password, linked banks,
ledgers, and transactions. Only one profile can be active at a time (like switching
accounts on a Mac, not a SaaS system).

📐 [Read the architecture](./ARCHITECTURE.md) (this is the source of truth)

---

## How You Use It

The product runs as a small daemon. Three equal-peer ways to interact with
it - pick whichever fits the moment, mix and match freely:

| Adapter | What it covers in v0.1 |
|---|---|
| **🖥️ Web UI** (`127.0.0.1:6789`) | Setup + Profile (settings, password, sync, export, **linked accounts list**) + a minimal **Ledgers** page. |
| **💬 MCP** (OpenClaw, Claude Desktop, any MCP client) | Full feature surface: connect/disconnect banks, edit ledgers, review classifications, query spending, trigger exports. Conversational when paired with an LLM. |
| **⏰ Scheduler** (background) | Daily auto-sync, drift detection, proactive re-auth alerts via your chosen notification channel. |

None of these is "primary." The UI is intentionally small - it handles
setup, the things you tweak occasionally, and bank management. Reviewing
transactions, running queries, and anything fancy still lives in MCP or
the background.

---

## Install

### Production (from registry)

```bash
clawhub install friday-budgeting-pro
```

Installs the latest published version from the ClawHub registry.

### Dev / Local clone

```bash
git clone https://github.com/Riddy21/Friday_Budgeting_Pro bank-transactions
clawhub install ./bank-transactions
# or from anywhere:
clawhub install /path/to/bank-transactions
```

Installs directly from your local clone — skips the registry. Use this when
testing changes before publishing. The same three install hooks fire in both
modes:

| Hook | What it does |
|---|---|
| **pip** | `pip3 install -r requirements.txt` |
| **db-init** | Creates `~/.friday-bp/data.db` (SQLite) |
| **launchd** | Registers the daemon via `server/installer.py`, starts at login |

Either way, once installation completes open `http://127.0.0.1:6789` in your
browser to finish setup.

---

## Connecting to Plaid

1. Create a free account at https://dashboard.plaid.com
2. Team Settings → Keys → copy your **Client ID** and **Production Secret**
3. Ask your OpenClaw agent: `Set up my Plaid credentials — client ID is <your_id>, secret is <your_secret>`
4. Agent writes the config and you're ready to connect banks via the setup wizard.

For sandbox testing, use `env=sandbox` and your sandbox secret instead.

### Supported Banks

The Plaid Link modal supports **any institution Plaid supports in Canada** — there
is no hardcoded allow-list limited to RBC or BMO. You can connect any Canadian bank
or credit union Plaid supports. The integration uses `country_codes=["CA"]` and no
institution filtering.

**Wealthsimple** specifically:

| Product | Plaid support | Notes |
|---|---|---|
| **Wealthsimple Cash** (spending account) | ✅ Supported | Connects through the standard Plaid Link flow, same as any other bank |
| **Wealthsimple Trade / Invest** | ❌ Not via Plaid | Plaid's standard API does not cover Wealthsimple Trade/Invest brokerage accounts. An unofficial API route exists but is not implemented; tracked in issue [#31](https://github.com/Riddy21/Friday_Budgeting_Pro/issues/31) via `server/providers/wealthsimple.py` |

To connect Wealthsimple Cash: use the **+ Connect a bank** button in the UI or
ask your OpenClaw agent to `connect a bank`. Select Wealthsimple in the Plaid
Link modal and authenticate normally.

---

## First Run

When you visit `http://127.0.0.1:6789` for the first time, you see a
small setup wizard (4 short screens):

1. **Set a password** - protects your local dashboard.
2. **Pick how you want to be notified** about ambiguous transactions:
   - "Through OpenClaw chat" (if you use it)
   - "macOS notifications"
   - "Just show me a banner in the UI"
3. **Connect your first bank** - click **+ Connect a bank** and follow
   the Plaid login.
4. **Done.** Lands on your Profile page.

The system picks sensible defaults for everything else (Personal ledger
with standard rows, daily 6 AM sync, LLM confidence threshold 0.75).
Adjust any of it later from the Profile page or via MCP.

---

## What the UI Looks Like (v0.1)

Three things, kept minimal:

**Setup wizard** - once, on first launch.

**Profile page** - the main ongoing page. Has:
- Display name + notification preference + LLM confidence slider
- Change password
- Log out
- Read-only system info (Plaid env, last sync time, daemon uptime)
- **Sync now** button
- **Export to Excel** button — available as a browser download at `/export/excel` (streams the workbook directly to your browser without saving to disk)
- **Linked Accounts** - compact list of connected banks with status pills
  and **Reconnect / Disconnect / + Connect a bank** buttons. No fancy
  cards, just a list.

**Ledgers page** - minimal structure editor. List your ledgers (default:
Personal), click into one to add/rename/remove line items, add new
ledgers when you need them (e.g. for a rental property).

Ledgers come in three types:
- **Personal** (default) — standard household budget with line items like Salary, Groceries, Dining, etc.
- **Property** — rental/investment property ledger with pre-seeded items: Rent income, Mortgage, Property tax, Maintenance & repairs, Insurance, Utilities.
- **Investment** — tracks investment accounts with Contributions and Dividends & Returns.

Create typed ledgers via MCP: `create_property_ledger('123 Main St')` or `create_investment_ledger('TFSA')`.
Link a bank account to a ledger so its transactions route there by default: `set_account_ledger(account_id, ledger_id)`.

Reviewing classifications, running queries, anything beyond the basics -
those still happen through the MCP adapter (your OpenClaw agent or any
other MCP client).

---

## What AI Does for You

Every transaction goes through a three-tier classifier:

1. **Rules** - exact merchant matches you've already confirmed. Free, instant.
2. **LLM** - for new merchants, the LLM reasons about the transaction
   using your hints and recent similar transactions, and auto-routes if
   confident enough.
3. **Review queue** - if it's unsure, the transaction lands in a review
   queue. You'll get a notification through your chosen channel.

After 3 successful classifications of the same merchant, it becomes a
Tier 1 rule automatically. The longer you use it, the less it asks.

### Classification Rules

Friday ships with **6 built-in (default) classification rules** that run before
the LLM sees a transaction.  They handle the most common patterns automatically:

| Priority | Rule | What it does |
|----------|------|--------------|
| 1 | **Pending skip** | Skips any transaction still marked pending |
| 10 | **Internal transfer** | Flags same-amount cross-account moves within 3 days as Transfer |
| 20 | **Investment contribution** | Marks outflows to Wealthsimple, Questrade, etc. as Transfer/Savings |
| 30 | **Credit card payment** | Identifies chequing→credit payments as Transfer (charges already tracked) |
| 40 | **Salary / payroll** | Marks bank-tagged payroll transactions as Income |
| 50 | **Bank fees** | Marks monthly account fees as Bank Fees (spending) |

Default rules can be **disabled** but not deleted.  You can add your own rules
(priority 100+) and reorder them via MCP tools: `list_rules`, `add_rule`,
`update_rule`, `reorder_rules`, `disable_rule`, `enable_rule`, `delete_rule`.

---

## With OpenClaw (and any other MCP client)

The MCP adapter exposes the full feature set. Through OpenClaw or any
MCP-capable client:

```
You:    How's this month looking?
Agent:  May 2026 so far:
        Income:    $6,500
        Expenses:  $3,247
        Top: Groceries $487, Dining $312, Subscriptions $89
        Net: +$3,253

You:    Connect another bank
Agent:  Opening Plaid Link at http://127.0.0.1:6789/link?t=...
        - let me know when you're done.

You:    Export this year to Excel
Agent:  ✓ Wrote Personal Finances.xlsx to your Documents folder.

Agent:  Heads up - got a $312 Costco charge from yesterday that I'm
        not sure about. Personal groceries, or something else?
You:    Half personal groceries, half supplies for work
Agent:  ✓ Split 50/50, saved as a hint for similar charges.
```

Same engine, just a different way in. Other MCP clients (Claude Desktop,
Cursor, mcporter on the CLI) work too - anywhere you can call MCP tools.

---

## Privacy & Security

- 🏠 **Local-only.** Nothing this app runs is reachable from the public
  internet. Everything binds to `127.0.0.1`.
- 🔒 **Plaid tokens encrypted at rest** (Fernet, key in macOS Keychain)
- 🔑 **Password hashed with argon2id**, never sent to the browser
- 📁 **Your data lives in `~/.friday-bp/data.db`** (SQLite, yours)
- 🚫 **No telemetry**, no cloud sync, no third parties except Plaid + your
  chosen LLM
- ⏱️ **Sessions persist until you log out** - no idle timeout
- 🔄 **Bank sync runs in the background regardless** of whether you're logged into the UI

See [ARCHITECTURE.md § Security](./ARCHITECTURE.md#security) for the full
threat model.

---

## What This Is Not (v0.1)

- Not a full web app. The UI is deliberately small: setup and profile only.
- Not a SaaS. Everything runs on your Mac, no cloud account.
- Not a generalized platform. Personal finances only.
- Not chat-only either. MCP is one of several equal-peer ways in.

---

## Testing

The regular test suite runs with pytest and needs no extra deps:

```bash
python3 -m pytest -q
```

### Browser tests (optional)

UI tests use Playwright and are **skipped automatically** when Playwright is not
installed, so CI is never broken by a missing dep. To activate them locally:

```bash
pip install playwright
playwright install chromium
python3 -m pytest tests/ui/
```

---

## Troubleshooting

**"Bank connection broken"** → Ask your OpenClaw agent (or any MCP client)
to reconnect that bank; it'll return a Plaid Link URL you click to fix.

**"I forgot my password"** → On the login page, click "Forgot password".
A recovery token is written to `~/.friday-bp/recovery.txt` (only you can
read it). Copy it into the reset page to set a new one.

**"Where's my data?"** → `~/.friday-bp/data.db`. Back it up.

**"The UI isn't loading"** → Check the daemon is running:
`launchctl list | grep friday-budgeting-pro`. Restart with
`launchctl kickstart -k gui/$UID/ai.openclaw.friday-budgeting-pro`.

**"How do I uninstall?"** → `clawhub uninstall friday-budgeting-pro`.
Your data file stays unless you delete it manually.

### Proactive Re-Auth Alerts

Friday will proactively notify you when a bank connection needs attention.
During the daily 06:00 sync, it checks for connections in these states:

- **`needs_reauth`** — Plaid login has expired (e.g. you changed your bank password).
  > ⚠️ Your BMO Bank of Montreal connection needs re-authorization. Say 'reconnect BMO Bank of Montreal' to open the re-auth flow.

- **`pending_expiration`** — Token is about to expire (some institutions rotate tokens).
  > ⚠️ Your TD Bank connection expires soon. Say 'reconnect TD Bank' to refresh it.

- **Never synced** — A connection was added but sync has never run.
  > ⚠️ Your Scotiabank connection needs re-authorization. Say 'reconnect Scotiabank' to open the re-auth flow.

Alerts are throttled to at most once every 24 hours per connection so you
won't be spammed. You can also ask your agent directly:
> "Do any of my bank connections need attention?"

and it will call `get_connections_needing_attention` and report back.

---

## License

MIT
