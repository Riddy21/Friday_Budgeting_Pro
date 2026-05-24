# Friday Budgeting Pro — Architecture

> **Design principle: AI-powered personal finance, multiple equal ways to interact.**
> The product is a local budgeting tool that uses LLM intelligence for smart
> classification. **There is no primary interface.** The web UI, the MCP
> server (OpenClaw and any other MCP client), the background scheduler,
> and any future adapter (CLI, webhooks, etc.) are equal-peer clients of
> the same core engine. Pick whichever fits the moment.
>
> The web UI in v0.1 is deliberately minimal: just setup and profile.
> Everything else (managing banks, editing ledgers, reviewing classifications,
> exports, queries) happens through MCP/chat or background processes. A
> bigger UI can be added later if useful.

---

## Design Constraints (read this first)

These are hard rules. Don't add features that violate them.

1. **Local profiles (multiple users, one active at a time).** Multiple named profiles can be stored locally — each with their own password, linked bank accounts, ledgers, and transactions. Only one profile can be logged in at a time. No concurrent sessions, no shared data between profiles. This is like switching accounts on a Mac, not a SaaS multi-tenant system.
2. **No primary interface.** The same core engine is reachable via several
   equal-peer adapters:
   - **Web UI** — only for setup + profile in v0.1; not where you manage
     your finances day-to-day
   - **MCP server** — for OpenClaw and other MCP-compatible clients;
     full feature surface
   - **Background scheduler** — daily sync, drift detection, notifications
   - Future paths (CLI, webhooks, etc.) can be added without redesign
   None is special. Removing any one of them doesn't break the others.
3. **AI is a feature, not a path.** Smart classification (LLM-driven) is
   a capability of the core engine. Every adapter that wants to expose it
   can.
4. **Standalone daemon.** The core service runs as a long-lived local
   process (started at user login). The UI, MCP, and scheduler all live
   inside it. The daemon's lifecycle is independent of any single client;
   OpenClaw connecting and disconnecting does not start or stop it.
5. **Minimal questions in setup.** Smart defaults for everything obvious;
   user only edits what's actually different. Setup is a small in-browser
   wizard — not a chat conversation.
6. **Local-network only.** Nothing this product runs is reachable from the
   public internet. No webhooks. No port forwarding. No tunnels. Everything
   binds to `127.0.0.1` only.
7. **Single user, single password.** Authentication is one password set on
   first launch. Sessions persist across restarts.
8. **Secrets never live in plaintext on disk.** Plaid access tokens encrypted
   with Fernet; key in macOS Keychain. Password hashed with argon2id.
9. **No features outside the personal-finance scope.** No nonprofits, no
   business templates, no balance sheets, no multi-currency, no investment
   tracking. Add if/when needed. Not now.
10. **Two notification paths, automatic fallback.** When the user needs to be
    told something (re-auth needed, ambiguous transaction), the system
    notifies via OpenClaw chat if available, otherwise via macOS
    Notification Center, otherwise just shows a banner in the UI.

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

Full schema lives in `db/schema.sql` (source of truth). Summary:

### Key design principles
- `transactions.amount` = original currency, never modified
- `transactions.amount_home` = converted to home currency at sync time
- `transaction_entries.entry_type` drives totals: `spending | income | transfer | savings | skip`
- All timestamps are UTC Unix seconds; display layer converts to user timezone
- `classification_rules` (natural language, priority-ordered) replaces `routing_rules`

### Tables

| Table | Purpose |
|-------|---------|
| `users` | Local profiles (one active at a time) |
| `sessions` | UI session cookies |
| `bank_connections` | One row per Plaid Item (institution) |
| `bank_accounts` | Individual accounts within a connection |
| `ledgers` | Financial entities: personal \| property \| investment |
| `line_items` | Categories within a ledger (income \| expense) |
| `transactions` | Raw from Plaid — never modified after insert |
| `transaction_entries` | Classification result: transaction → line item |
| `classification_rules` | Natural language rules, priority-ordered, evaluated by LLM |
| `classification_hints` | Supplementary LLM context (not priority-ordered) |
| `routing_rules` | Legacy substring rules — kept for compat, no new writes |
| `fx_rates` | Exchange rate cache (base/quote/rate, refresh if >24h old) |
| `sync_cursors` | Plaid incremental sync cursors per connection |
| `app_config` | Single-row: home_currency, timezone, password hash, notification channel |
| `notifications` | In-app notification log |
| `auto_promoted_rules_log` | Audit trail for auto-promoted routing rules |

### bank_accounts key fields
- `currency` — native currency (from Plaid)
- `balance_current`, `balance_available` — from last sync
- `description` — user-set context for classifier
- `default_ledger_id` — route transactions from this account to this ledger by default

### ledgers key fields
- `type` — `personal | property | investment`
- `description` — optional label

### transaction_entries key fields
- `entry_type` — `spending | income | transfer | savings | skip`
- `source` — `rule | llm | manual | manual_retroactive`
- `uncertain` — 1 if classifier confidence < threshold
- `reasoning` — LLM explanation
- `corrected_from_line_item_id` + `corrected_at` — audit trail for corrections

### classification_rules key fields
- `description` — natural language rule (what it matches and does)
- `rule_type` — `transfer | savings | spending | income | skip`
- `priority` — lower = evaluated first (default rules: 1–50, user rules: 100+)
- `is_default` — 1 = shipped with app, can disable but not delete

### Default classification rules (seeded on setup)
| Priority | Name | Type |
|----------|------|------|
| 1 | Pending skip | skip |
| 10 | Internal transfer | transfer |
| 20 | Investment contribution | savings |
| 30 | Credit card payment | transfer |
| 40 | Salary / payroll | income |
| 50 | Bank fees | spending |

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
- `list_ledgers()` — returns `id`, `name`, `type`, `description`, and `items` for each ledger
- `add_ledger(name)` — creates a generic personal ledger
- `create_property_ledger(name, description?)` — creates a `property` ledger seeded with 6 default line items (Rent income, Mortgage, Property tax, Maintenance & repairs, Insurance, Utilities)
- `create_investment_ledger(name)` — creates an `investment` ledger seeded with 2 default line items (Contributions, Dividends & Returns)
- `add_line_item(ledger_id, name, item_type)` — `item_type` is `income` or `expense`
- `remove_line_item(id)`
- `set_account_ledger(account_id, ledger_id)` — routes transactions from a bank account to a specific ledger by default

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

## Architecture in One Diagram

```
  ┌──────────────────────┐   ┌──────────────────────┐   ┌──────────────────┐
  │   Browser (UI)    │   │  OpenClaw (chat) │   │   Scheduler     │
  │    primary path   │   │   optional path  │   │  (internal cron)│
  └────────┬─────────┘   └────────┬─────────┘   └────────┬─────────┘
           │ HTTP             │ MCP/stdio        │ in-proc
           │                  │                  │
           ▼                  ▼                  ▼
  ┌────────────────────────────────────────────────────────┐
  │         friday-budgeting-pro daemon (long-lived process)         │
  │                                                                  │
  │   ┌──────────┐  ┌──────────┐  ┌────────────┐  ┌────────────┐    │
  │   │  UI app  │  │ MCP app  │  │ Scheduler  │  │ Notifier   │    │
  │   └──────┬───┘  └──────┬───┘  └──────┬─────┘  └──────┬─────┘    │
  │          │             │             │             │           │
  │          └───────────┴────────────┴────────────┴──────────┐│
  │                       Core engine                              ││
  │   - Plaid client       - Classifier (rules/LLM/review)         ││
  │   - Ledger management  - Auth (argon2 + sessions)              ││
  │   - DB access          - Notification routing                  ││
  │   └─────────────────────────────────────────────────────┐ ││
  │                                                              │ ││
  │    SQLite DB (~/.friday-bp/data.db)                          │ ││
  │    Encrypted Plaid tokens + Keychain-stored Fernet key       │ ││
  │    └───────────────────────────────────────────────────┐ │ ││
  └──────────────────────────────────────────────────────────────────┘
```

All top-line adapters are **equal-peer citizens** of the core engine. None is
"primary." Adding a new adapter (CLI, webhook, etc.) is just another small
shim against the core engine.

---

## Adapter: Web UI (deliberately minimal)

A small local-only web app. v0.1 scope:

- **First-run setup wizard** — set a password, pick a notification
  preference, connect a first bank via Plaid Link.
- **Profile page** — settings, change password, manual sync, Excel
  export, plus a small **Linked Accounts** section listing connected
  banks with status pills and **Reconnect / Disconnect / + Connect a
  bank** buttons.
- **Ledgers page** — a tiny structure editor: list ledgers, add /
  rename / remove line items inside each, add / remove ledgers.
- **Plaid Link page** — the browser-based bank-connection flow,
  used by the setup wizard, by the Profile Linked Accounts section,
  and by any MCP-issued "add a bank" link.

That's the whole UI. **Still not included** in v0.1: transaction review,
classification rules editor, dashboard with charts. Those live in MCP
(or are future tickets).

Reachable at `http://127.0.0.1:6789` whenever the daemon is running. Set
a password during setup; subsequent visits go through `/login`.

## Adapter: MCP server (OpenClaw and other clients)

A FastMCP endpoint inside the daemon, exposing the engine's **full** action
set as MCP tools. Any MCP-compatible client (OpenClaw, Claude Desktop,
Cursor, CLI-MCP utilities) can connect and drive the system.

This is where almost all day-to-day usage happens in v0.1: connecting and
disconnecting banks, editing ledgers, reviewing ambiguous transactions,
running queries, triggering exports. With an LLM-equipped client like
OpenClaw, these get a conversational feel; with a CLI-MCP utility, they're
just direct tool calls. Same underlying surface.

## Adapter: Background scheduler

A small async loop inside the daemon that triggers periodic actions on the
engine without any user involvement:
- Daily sync (default 6 AM local time)
- Hourly drift check (lightweight connection health probe)
- Notification fan-out when something needs the user's attention

This is the only adapter with no human on the other end. It keeps the
system working while you're not looking.

### Pages (v0.1)

```
  /              →  if no password set: /setup; else /profile
  /setup         →  first-run wizard (one-time, locked after completion)
  /login         →  password login
  /profile       →  settings, password, sync/export actions, Linked Accounts list
  /ledgers       →  minimal ledger / line-item editor
  /link          →  Plaid Link flow (used by setup wizard, profile, MCP links)
```

**Not in v0.1:** transaction review, classification rules editor, dashboard
with charts. Those still live in MCP (or are future tickets).

### What each page does

**Setup wizard** (`/setup`) — one-time, four short steps
1. Welcome + set password
2. Pick notification preference (OpenClaw chat / macOS notifications / in-UI)
3. Connect first bank via Plaid Link
4. Done — link to `/profile`

After completion, `/setup` returns 404. Re-running setup means resetting
the DB (a future operation, not a v0.1 feature).

**Login** (`/login`) — just the password form
- POST validates, sets HttpOnly + SameSite=Strict session cookie, redirects to `/profile`
- Rate-limited (5 failed attempts in 5 min → lockout)
- Has a **Forgot password** link → recovery-file flow (#60)

**Profile** (`/profile`) — the main page
- **Account:** display name (editable)
- **Notifications:** chosen channel (radio: OpenClaw chat / macOS notifications / in-UI only)
- **Classifier:** LLM confidence threshold (slider 0.5–0.95, default 0.75)
- **Security:** change password (asks for old + new), **Log out** button
- **System (read-only):** Plaid env, DB path, daemon uptime, last sync time
- **Quick actions:** **Sync now** button, **Export to Excel** button
- **Linked Accounts** (compact list):
  - One row per connected bank: name · status pill (🟢 / 🟡 / 🔴) ·
    last sync · **Reconnect** (when needed) · **Disconnect**
  - **+ Connect a bank** button at the bottom of the section
  - Minimal layout — no logos, no expandable per-account breakdown

**Ledgers** (`/ledgers`) — minimal structure editor
- One row per ledger (default: Personal). Click a ledger to view items.
- Inside a ledger: simple list of line items with inline rename + a
  small × to remove
- One small text input at the bottom of each ledger to add a new line item
- Top-right **+ Add Ledger** button (e.g. for a rental property)
- Plain HTML table, no animations or fancy interactions

**Plaid Link** (`/link`) — the Plaid-required browser flow
- Used by the setup wizard for the first bank
- Used by the Profile page's Linked Accounts section when the user clicks
  **+ Connect a bank** or **Reconnect**
- Used by MCP when the user (or the agent) initiates an "add a bank" or
  "reconnect" action — the MCP tool returns a `/link` URL with a one-time
  token, the user opens it, the flow completes, the page closes

### Look and feel

- Plain HTML, a tiny bit of vanilla JS. **No React, no build step, no
  framework.**
- A simple top nav: **Profile** · **Ledgers** · **Log out**. That's it.
- Minimal styling. Looks fine on mobile but no mobile-specific features.

### Lifecycle: long-lived daemon

The service is a long-lived background process, started at user login (via
launchd on macOS). It is independent of OpenClaw — it runs whether or not
OpenClaw is running.

- Default UI URL: `http://127.0.0.1:6789` (configurable via env var)
- Implementation: a single Python process running:
  - The FastAPI UI app (always listening)
  - The FastMCP MCP endpoint (stdio interface; used when OpenClaw spawns a
    connection)
  - The internal scheduler loop (daily sync at 6 AM by default)
- Installed via ClawHub (preferred) or manually; installation writes a
  launchd plist so the daemon starts at login and restarts if it crashes.
- The MCP endpoint exists inside the daemon, but OpenClaw spawning an MCP
  connection is a *connection event*, not a lifecycle event — the daemon
  was already running.

### Authentication: set in the UI, log in in the UI

No chat involvement, no launch tokens. A standard login page with a password
you set on first launch.

- **First-run flow:** when the UI sees that `app_config.ui_password_hash`
  is empty, every route except `/setup` redirects there. The `/setup`
  wizard collects: new password, confirm password, optional notification
  preference ("send chat notifications via OpenClaw if available"), then
  prompts to connect a first bank.
- **Password storage:** argon2id hash in `app_config.ui_password_hash`.
  Never sent back to the browser.
- **Login flow:** GET `/login` → POST with password → server validates →
  sets HttpOnly + SameSite=Strict session cookie → redirects to `/profile`.
- **Session lifetime:** permanent until you explicitly log out. No idle
  timeout. The daemon syncs in the background whether or not you have a
  browser open — the session state is irrelevant to syncing.
- **No rate limiting.** This is a local app on your own machine.
  Anyone who can reach 127.0.0.1 already has access to your Mac.
- **Password reset (forgotten password):** in-UI "forgot password" link
  generates a recovery token written to `~/.friday-bp/recovery.txt` (file
  perms 0600). User opens that file from a terminal, copies the token,
  pastes into `/reset?t=...` to set a new password. This works because
  the user has shell access to their own machine; an attacker who has
  shell access has already lost.
- **Optional: reset via chat.** If OpenClaw is configured, the user can
  also say "reset my finance dashboard password" — the agent calls
  `reset_ui_password()` and gets the same recovery token via MCP. This
  is a convenience, not the primary path.

### Security (same rules as the rest of the system)

- Bound to `127.0.0.1:6789` only. Refuses to start on any other interface.
- All routes (except `/login` and `/static/*`) require a valid session
  cookie. Without one, every route returns 401 or redirects to `/login`.
- Sensitive values (Plaid access tokens) are never sent to the browser —
  only metadata (status, last synced, institution name).
- All state writes go through the same MCP tool layer the chat path uses;
  no separate code path means no separate set of vulnerabilities.
- Login attempts are rate-limited (above).

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

## Database Migration Strategy

### Rules (mandatory for all schema changes)
1. **All migrations are additive** — `ADD COLUMN` only. Never `DROP` or rename a column.
2. **New columns must have a `DEFAULT`** or be nullable — existing rows have no value.
3. **`init_db()` does not migrate** — it only creates tables that don't exist. Migrations live in `migrate_db()` in `server/db.py`.
4. **`migrate_db()` is idempotent** — safe to run on every startup. Pattern: `PRAGMA table_info(…)` to check if column exists before `ALTER TABLE`.
5. **Code handles `NULL` gracefully** — any column added post-release may be `NULL` in old DBs. Use `COALESCE(col, default)` in queries, not bare column reads.
6. **FK columns are nullable** — new FK columns added via migration cannot be enforced on existing rows.
7. **Never rename** — add the new name, deprecate the old, remove old only after confirmed no readers remain.

### Backward compatibility
Old app version reading a DB with new columns: **ignores them** — safe.
New app version reading an old DB with missing columns: **sees NULL** — must handle gracefully.

### Forward compatibility
If a migration adds columns that a rollback would not know about, the rollback sees unknown columns in `PRAGMA table_info` but does not fail — SQLite allows this. Document rollback steps in the migration comment.

### Migration version tracking
Add a row to `app_config` comments (no separate migrations table needed — `migrate_db()` is self-describing via its idempotency checks).

| Version | Date | Changes |
|---------|------|---------|
| v1 | 2026-05-23 | Initial schema |
| v2 | 2026-05-24 | `bank_accounts`: currency, balance_current, balance_available, description, default_ledger_id. `ledgers`: type, description. `transactions`: currency, amount_home, plaid_category_detailed. `transaction_entries`: amount_home, entry_type, uncertain, reasoning, corrected_from_line_item_id, corrected_at. `app_config`: home_currency, timezone. New tables: classification_rules, fx_rates |

### Pitfalls to avoid in migrations
- ❌ `NOT NULL` without `DEFAULT` on a new column — breaks existing rows
- ❌ Forgetting `migrate_db()` call in startup — new columns never added to existing DBs
- ❌ Assuming a column exists — always `COALESCE` or check `PRAGMA table_info` in code
- ❌ Non-idempotent migration — if it runs twice it must be safe
- ❌ Migrating data in the same step as schema change — do schema first, backfill as a separate step

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
├── SKILL.md                 ← tells your OpenClaw agent when to use the skill
├── package.json             ← clawhub publish metadata
├── requirements.txt
├── .gitignore
│
├── db/
│   └── schema.sql
│
├── server/
│   ├── main.py              ← FastMCP entry point
│   ├── db.py                ← SQLite helpers (shared by MCP + UI)
│   ├── plaid_client.py
│   ├── classifier.py        ← 3-tier engine
│   ├── llm.py               ← LLM call wrapper
│   └── excel_export.py
│
└── ui/
    ├── server.py            ← FastAPI app, mounted in the daemon, 127.0.0.1:6789
    ├── auth.py              ← argon2 + session cookies + rate limit
    ├── templates/
    │   ├── base.html
    │   ├── setup.html           ← first-run wizard (4 steps)
    │   ├── login.html           ← password login + forgot link
    │   ├── profile.html         ← settings + sync/export + linked accounts
    │   ├── ledgers.html         ← minimal ledger / line-item editor
    │   └── link.html            ← Plaid Link flow
    └── static/
        └── style.css            ← minimal styles
```

That's the whole codebase. ~10 files.

---

## What's Explicitly Out of Scope (v0.1)

- ✅ Local profiles (multiple users, one active at a time) — see users table above
- ❌ Concurrent sessions / multi-tenant SaaS
- ❌ Web UI for reviewing/classifying transactions (done via MCP/chat in v0.1)
- ❌ Web dashboard with charts/analytics
  - These two roll up into the "bigger UI" future ticket (#55).
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
- ❌ Anything that ships as a "template gallery"

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
