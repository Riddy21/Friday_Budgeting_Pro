# Friday Budgeting Pro — Architecture

> **Design principle: keep it as simple as humanly possible.**
> Single user. Conversational only. No wizards, no CLIs, no separate UIs.
> Just an OpenClaw skill that HAL uses when needed.

---

## Design Constraints (read this first)

These are hard rules. Don't add features that violate them.

1. **Single-user only.** No multi-tenant accounts. The user *is* the system owner.
2. **Conversational always.** Every interaction is a chat message with HAL.
   No CLI prompts. No web wizards. No setup commands the user has to remember.
3. **Lazy invocation.** The skill only runs when HAL decides to use it (because
   the user said something finance-related). Not always-on.
4. **Minimal questions.** Setup asks 2-3 clarifying questions max, then uses
   smart defaults for everything else. Refinement happens through normal chat.
5. **OpenClaw handles scheduling.** The skill auto-registers cron jobs via
   OpenClaw's `cron` tool. No external schedulers, no daemons.
6. **OpenClaw handles notifications.** No separate notification config.
   Messages go through whatever channel the user is already chatting on.
7. **No web UI components** except the one unavoidable case: Plaid Link
   (which requires a browser by Plaid's design — there's no API alternative).
8. **No features that aren't directly useful for personal finance.** No
   nonprofits, no business templates, no balance sheets, no multi-currency,
   no investment tracking. If/when needed later, add it. Not now.
9. **Local-network only.** Nothing this skill runs is reachable from the public
   internet. No webhooks. No port forwarding. No tunnels. Everything binds to
   `127.0.0.1` only. See [Security](#security) below for details.
10. **Secrets never live in plaintext on disk.** Plaid access tokens are
    encrypted with Fernet; the encryption key is stored in the macOS Keychain,
    not on the filesystem.

---

## What This Is

An OpenClaw skill that lets the user manage personal finances by chatting with
HAL. It:

- Connects to banks via Plaid
- Auto-classifies transactions using a tiered engine (rules → LLM → ask user)
- Stores everything in a local SQLite file
- Exports to Excel on request
- Runs a daily sync via an OpenClaw cron job
- Pings the user when it needs help classifying something

The user never opens a UI, never runs a command, never edits a config file.
Everything is "hey HAL, ..."

---

## Top-Level Flow

```
User says something finance-related to HAL
        │
        ▼
HAL recognizes the skill applies (via SKILL.md)
        │
        ▼
HAL calls Friday Budgeting Pro MCP tools as needed
        │
        ▼
First time:  triggers conversational setup
Returning:   does the thing the user asked
        │
        ▼
HAL responds in chat with the result
```

That's it. No other entry points.

---

## First-Time Setup (Conversation Only)

When HAL detects this is the first run (DB doesn't exist or is empty), it
asks a few questions and creates the structure. The whole thing is one
conversation.

**Question 1:** "What banks should I connect?"
→ User lists them, HAL opens Plaid Link for each one

**Question 2:** "Anything besides personal finances? Most people just want
one ledger called 'Personal'. Are you tracking anything separately?"
→ User says no / says yes and describes it

**Question 3:** "Any quick rules I should know? For example, are certain
merchants always personal, or always something else?"
→ User describes preferences in plain English, saved as classification hints

Then HAL says: "Great, pulling your last 90 days. Daily sync at 6 AM — I'll
ping you when there's something I'm not sure about."

Done. No other setup.

**Defaults applied automatically:**
- Ledger: "Personal" with standard line items (Salary, Groceries, Dining,
  Transport, Subscriptions, Healthcare, Travel, Shopping, Misc, Other)
- Daily sync at 6 AM via OpenClaw cron
- 90-day initial transaction pull
- LLM confidence threshold: 0.75
- Notification channel: whatever the user is currently chatting on

---

## System Diagram

```
                    ┌──────────────────────────────────────────────┐
                    │                  USER                         │
                    │ (chatting with HAL via iMessage/Telegram/etc.)│
                    └──────────────────────────────────────────────┘
                                       │ ▲
                                       ▼ │
                    ┌──────────────────────────────────────────────┐
                    │              OPENCLAW + HAL                   │
                    │                                               │
                    │   ┌──────────┐    ┌────────────────────┐     │
                    │   │   HAL    │───▶│   MCP Client       │     │
                    │   │  (LLM)   │    │  (mcporter)        │     │
                    │   └──────────┘    └────────────────────┘     │
                    │   ┌────────────────────────────────────┐     │
                    │   │ cron tool (schedules daily sync)   │     │
                    │   └────────────────────────────────────┘     │
                    └────────────────────┬──────────────────────────┘
                                         │ MCP
                                         ▼
                    ┌──────────────────────────────────────────────┐
                    │     Friday Budgeting Pro MCP Server          │
                    │                                              │
                    │   Tools:                                     │
                    │     • Setup     (one-shot, conversational)   │
                    │     • Banks     (Plaid link + sync)          │
                    │     • Ledgers   (read/edit structure)        │
                    │     • Txns      (list, route, split)         │
                    │     • Hints     (NL preferences)             │
                    │     • Export    (Excel)                      │
                    │                                              │
                    │   ╔══════════════════════════════════════╗   │
                    │   ║ Classifier (3-tier)                  ║   │
                    │   ║   1. Rules                           ║   │
                    │   ║   2. LLM (with hints + history)      ║   │
                    │   ║   3. Ask user via HAL                ║   │
                    │   ╚══════════════════════════════════════╝   │
                    │                                              │
                    │   SQLite (~/.friday-bp/data.db)              │
                    └────────────────────┬─────────────────────────┘
                                         │
                  ┌──────────────────────┴────────────────────────┐
                  ▼                                               ▼
        ┌──────────────────┐                          ┌──────────────────┐
        │   Plaid API      │                          │  LLM (via the    │
        │ Transactions +   │                          │  same provider   │
        │ Link UI (local)  │                          │  HAL uses)       │
        └──────────────────┘                          └──────────────────┘
```

---

## Data Model (Minimal)

Stripped down to what's actually needed for personal use:

```sql
-- Plaid bank connections
CREATE TABLE bank_connections (
  id TEXT PRIMARY KEY,
  plaid_item_id TEXT UNIQUE,
  plaid_access_token_encrypted TEXT NOT NULL,
  institution_name TEXT,
  status TEXT DEFAULT 'active',  -- active | needs_reauth
  last_synced_at INTEGER
);

CREATE TABLE bank_accounts (
  id TEXT PRIMARY KEY,
  connection_id TEXT REFERENCES bank_connections(id),
  plaid_account_id TEXT UNIQUE,
  name TEXT, mask TEXT, type TEXT, subtype TEXT
);

-- Tracking structure (usually just one ledger called "Personal")
CREATE TABLE ledgers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE line_items (
  id TEXT PRIMARY KEY,
  ledger_id TEXT REFERENCES ledgers(id),
  name TEXT NOT NULL,
  item_type TEXT DEFAULT 'expense'  -- income | expense
);

-- Raw transactions from Plaid
CREATE TABLE transactions (
  id TEXT PRIMARY KEY,
  bank_account_id TEXT REFERENCES bank_accounts(id),
  plaid_transaction_id TEXT UNIQUE,
  date TEXT NOT NULL,
  merchant TEXT,
  amount REAL NOT NULL,
  plaid_category TEXT,
  pending INTEGER DEFAULT 0
);

-- The classified form (supports splits)
CREATE TABLE transaction_entries (
  id TEXT PRIMARY KEY,
  transaction_id TEXT REFERENCES transactions(id),
  ledger_id TEXT REFERENCES ledgers(id),
  line_item_id TEXT REFERENCES line_items(id),
  amount REAL NOT NULL,
  source TEXT,           -- rule | llm | manual
  confidence REAL,
  reviewed INTEGER DEFAULT 0
);

-- Tier 1: deterministic rules (auto-created over time)
CREATE TABLE routing_rules (
  id TEXT PRIMARY KEY,
  merchant_pattern TEXT,
  line_item_id TEXT REFERENCES line_items(id)
);

-- Tier 2: natural-language hints fed to the LLM
CREATE TABLE classification_hints (
  id TEXT PRIMARY KEY,
  text TEXT NOT NULL
);

-- Plaid sync cursors
CREATE TABLE sync_cursors (
  connection_id TEXT PRIMARY KEY REFERENCES bank_connections(id),
  cursor TEXT,
  last_synced_at INTEGER
);
```

That's all of it. No `users` table (single user). No `budget_targets`,
`classification_history`, `bank_account.currency` — drop them until needed.

---

## MCP Tools (Trimmed)

Only what HAL actually needs to call. Grouped:

### Setup (one-shot)
- `setup_status()` → returns `not_started | in_progress | complete`
- `apply_initial_setup(banks_to_link[], extra_ledgers[], hints[])` → does the
  whole setup in one call. HAL asks 2-3 questions, then calls this.

### Banks
- `start_link()` → returns URL to open Plaid Link
- `complete_link(public_token)` → exchange + store
- `list_connections()`
- `refresh_connection(id)` → Update Mode link
- `disconnect(id)`

### Ledgers (rarely used after setup)
- `list_ledgers()`
- `add_line_item(ledger_id, name, item_type)`
- `add_ledger(name)`
- `remove_line_item(id)`

### Transactions
- `sync()` → pull from Plaid, classify, return summary
- `list(filters)` → query transactions
- `get_needs_review()` → ambiguous ones HAL should ask about
- `route(transaction_id, allocations[])` → manual or HAL-driven routing
- `add_hint(text)` → save a natural-language hint

### Reports
- `summary(period)` → spending totals
- `export_excel(years?)` → generate Excel file(s)

That's the whole API. ~15 tools.

---

## Classification Engine (Unchanged — This Is the Value)

```
New transaction
   │
   ├─▶ Tier 1: Rules
   │     If merchant matches a saved rule → auto-route. Done.
   │
   ├─▶ Tier 2: LLM
   │     Prompt: hints + ledger tree + recent similar txns + this txn
   │     LLM picks ledger/line item with confidence score
   │     If confidence >= 0.75 → auto-route + flag for casual review
   │
   └─▶ Tier 3: Ask user
         HAL sends: "Got a $X charge at Y — my guess is Z (62% sure).
                     Correct, or should it be something else?"
         User replies → save as a new rule for next time
```

After 3 successful LLM classifications of the same merchant, auto-promote
to a Tier 1 rule. System gets cheaper and faster over time.

---

## OpenClaw Integration

### How HAL knows to use the skill (`SKILL.md`)

The skill ships with a SKILL.md telling HAL when to invoke it:

```yaml
name: friday-budgeting-pro
description: Use for personal finance tasks: connecting banks, syncing
             transactions, classifying spending, exporting to Excel,
             showing spending summaries.
```

HAL reads available skills at the start of each turn. When the user says
something finance-y, HAL calls the MCP tools. Otherwise the skill stays idle.

### How daily sync works (OpenClaw `cron` tool)

After initial setup, the skill calls OpenClaw's `cron` tool to register a
daily job:

```js
cron.add({
  name: "friday-budgeting-pro-daily-sync",
  schedule: { kind: "cron", expr: "0 6 * * *", tz: "America/Toronto" },
  payload: {
    kind: "agentTurn",
    message: "Run Friday Budgeting Pro daily sync. Call sync(), then if
              get_needs_review() returns transactions, ask the user about
              them one at a time."
  },
  delivery: { mode: "announce" }   // sends results to user's main channel
})
```

That's the entire scheduling system. OpenClaw owns the timing, HAL owns the
work, the skill provides the tools.

### How notifications work

There's no notification system in the skill. When HAL needs to ask the user
about something, it just sends a chat message through whatever channel the
user is currently on. OpenClaw routes it. Zero config.

---

## Security

> Less surface area is the best security. The whole design is built around
> staying small and offline.

### Threat model

What we defend against:
- **Other devices on the same WiFi/LAN** — they should not see anything.
- **Other macOS users on this machine** — they should not read tokens or DB.
- **Untrusted local processes** — they should not call our MCP tools or POST
  to our Plaid Link page.
- **Stolen disk image / backup** — tokens should be unreadable without
  Keychain access.

What we do *not* try to defend against (out of scope):
- A root-level attacker on the user's machine.
- A compromised OpenClaw or HAL itself (those have legitimate access).
- Plaid or the chosen LLM provider being malicious.

### Defenses (and why each is enough)

| Surface | Defense |
|---|---|
| MCP server transport | **stdio only.** No HTTP listener. Only the parent OpenClaw process can call our tools. |
| Plaid Link UI | Bound to `127.0.0.1:0` (random port). Runs only during active link flow, **auto-shuts down** within 60s of completion. URL includes a single-use random token. |
| Plaid webhooks | **Not used.** All connection health is polled from inside `sync()`. Removes the only would-be public surface. |
| Plaid access tokens | Encrypted with Fernet before write. Key stored in macOS Keychain (`security add-generic-password` / `keyring` lib). DB file alone is useless. |
| SQLite DB | Path `~/.friday-bp/data.db`, permissions `0600` (user only). Parent dir `0700`. |
| Concurrent sync | Single-flight lock file in `~/.friday-bp/sync.lock`. Prevents double-inserts and cursor races. |
| LLM data exposure | Only merchant name + amount + plaid_category + user's own hints are sent. No account numbers, no full transaction IDs. User picks the LLM provider. |
| Auto-promoted rules | Every promotion is logged + reversible. User can say "undo the last rule HAL learned" any time. |
| LLM output validation | Returned `ledger_id` and `line_item_id` are checked against the DB before any routing happens. LLM hallucinations are rejected, not stored. |
| Sandbox vs Production | The Plaid environment is a config flag stored once at setup; tokens from one environment cannot be used in the other (DB tracks env per connection). |

### What this means in practice

- Nothing this skill runs is reachable from the public internet.
- No port forwarding, no ngrok, no Tailscale Funnel, no cloud proxy required.
- A device on the same WiFi as the Mac cannot see the MCP server, the Link
  UI, the DB, or anything else — because nothing listens on a non-loopback
  interface.
- If the Mac's disk is stolen, the encrypted DB + encrypted tokens are
  useless without the Keychain entry (which is itself protected by macOS
  login).

### What we give up by going polling-only

Plaid's webhooks would let us know about `PENDING_EXPIRATION` ~7 days early.
Without them, we learn about an expired connection on the next daily sync
(0-24h after it actually expires). The user is still proactively notified in
chat — just slightly later than ideal. **Acceptable tradeoff for zero internet
exposure.**

---

## Pitfalls We're Explicitly Avoiding

Things that often go wrong in this kind of system, and how this design dodges them:

| Pitfall | How we avoid it |
|---|---|
| Bound the Link UI to `0.0.0.0` by accident → LAN exposure | Explicit `127.0.0.1` bind + integration test that asserts the bind |
| Two sync jobs racing (cron + manual) → duplicate transactions | Lock file + single-flight wrapper around `sync()` |
| LLM returns made-up `ledger_id` → corrupt routing | All returned IDs validated against DB before commit |
| Bad LLM decision gets auto-promoted to a Tier 1 rule | Promotion needs 3 consecutive same-merchant matches + always reversible |
| Token file leaked from a backup | Tokens encrypted, key in Keychain (not on disk) |
| Sandbox token tried against production (or vice versa) | DB tracks Plaid env per connection; mismatch = hard error |
| Connection broken silently → stale spreadsheet for weeks | Health check on every sync; user gets a chat alert within 24h |
| Plaid API down during sync → partial data | Cursor only advances on full success; sync is idempotent on retry |
| User changes ledger structure mid-flight | All entries reference IDs, not names; renames are safe |
| Excel export concurrent with sync | Excel writes go to a temp file then atomic rename |
| Cron job runs while user is mid-classification chat | Sync uses the same lock; classification prompts queue, don't collide |

---

## Installation (One Command)

```bash
clawhub install friday-budgeting-pro
```

This:
1. Drops the MCP server files into `~/.openclaw/skills/friday-budgeting-pro/`
2. Registers it with OpenClaw's MCP client
3. Installs the SKILL.md so HAL knows about it
4. Initializes an empty SQLite DB at `~/.friday-bp/data.db`

Next time the user mentions finances to HAL, the setup conversation starts.

---

## Project Structure (Minimal)

```
friday-budgeting-pro/
├── README.md
├── ARCHITECTURE.md          ← THIS FILE (source of truth)
├── SKILL.md                 ← tells HAL when to use the skill
├── package.json             ← clawhub publish metadata
├── requirements.txt
├── .gitignore
│
├── db/
│   └── schema.sql
│
├── server/
│   ├── main.py              ← FastMCP entry point
│   ├── db.py                ← SQLite helpers
│   ├── plaid_client.py
│   ├── classifier.py        ← 3-tier engine
│   ├── llm.py               ← LLM call wrapper
│   └── excel_export.py
│
└── plaid_link/
    └── index.html           ← only static asset; served at localhost on demand
```

That's the whole codebase. ~10 files.

---

## What's Explicitly Out of Scope

- ❌ Multi-user / multi-tenant
- ❌ Web dashboard
- ❌ Mobile app
- ❌ Multi-currency / FX
- ❌ Investment tracking
- ❌ Tax filing categorization
- ❌ Budget targets / forecasting
- ❌ Non-Plaid integrations
- ❌ Notification channel configuration (uses OpenClaw's)
- ❌ Standalone scheduler (uses OpenClaw's `cron` tool)
- ❌ CLI wizard
- ❌ Manual setup flows that aren't conversational
- ❌ Anything that ships as a "template gallery" or has a config UI

If something here turns out to be needed later, add it then. Not now.

---

## Tech Stack (Minimal)

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| MCP framework | FastMCP |
| Database | SQLite |
| Plaid | plaid-python |
| Excel | openpyxl |
| Link UI | Plain HTML |
| Encryption | cryptography (Fernet, for Plaid tokens) |
| LLM | Whatever HAL is already using — no separate config |
| Scheduling | OpenClaw `cron` tool |
| Notifications | OpenClaw's existing message channels |

---

## Status
- [x] Architecture finalized (this doc — source of truth)
- [ ] DB schema + init
- [ ] MCP server skeleton with the trimmed tool list
- [ ] Plaid Link UI + sync
- [ ] 3-tier classifier
- [ ] Conversational setup tool (`apply_initial_setup`)
- [ ] Excel export
- [ ] SKILL.md
- [ ] OpenClaw cron auto-registration
- [ ] Publish to ClawHub
- [ ] End-to-end test
