# Friday Budgeting Pro — Architecture

## System Overview

```
                    ┌──────────────────────────────────────────────────┐
                    │                       USER                        │
                    │   (interacts via natural language with HAL/LLM)   │
                    └──────────────────────────────────────────────────┘
                                 │                          ▲
                                 │ "Sync my transactions"   │ "Got a $47 Home Depot
                                 │ "Show this month"        │  charge — personal
                                 │ "Export to Excel"        │  or rental?"
                                 ▼                          │
        ┌────────────────────────────────────────────────────────────────┐
        │                      OPENCLAW + HAL                            │
        │                                                                │
        │   ┌──────────┐    ┌──────────┐    ┌──────────────────────┐    │
        │   │ Chat UI  │ ─▶ │   HAL    │ ─▶ │   MCP Client Layer   │    │
        │   │ iMessage │    │  (LLM)   │    │     (mcporter)       │    │
        │   │ Telegram │    └──────────┘    └──────────────────────┘    │
        │   └──────────┘                              │                  │
        └─────────────────────────────────────────────┼──────────────────┘
                                                      │ MCP protocol
                                                      ▼
        ┌──────────────────────────────────────────────────────────────────┐
        │              FRIDAY BUDGETING PRO — MCP SERVER                    │
        │                                                                   │
        │   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌────────┐  │
        │   │   Account   │  │   Ledgers   │  │ Transactions│  │ Export │  │
        │   │    Tools    │  │    Tools    │  │    Tools    │  │ Tools  │  │
        │   └─────────────┘  └─────────────┘  └─────────────┘  └────────┘  │
        │   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌────────┐  │
        │   │    Banks    │  │    Rules    │  │    Hints    │  │ Audit  │  │
        │   │    Tools    │  │    Tools    │  │    Tools    │  │ Tools  │  │
        │   └─────────────┘  └─────────────┘  └─────────────┘  └────────┘  │
        │                                                                   │
        │   ╔═══════════════════════════════════════════════════════════╗   │
        │   ║          CLASSIFICATION ENGINE (3-Tier Cascade)           ║   │
        │   ║                                                           ║   │
        │   ║  ┌─────────┐    ┌─────────────┐    ┌──────────────────┐  ║   │
        │   ║  │ Tier 1: │ ─▶ │  Tier 2:    │ ─▶ │  Tier 3:         │  ║   │
        │   ║  │  Rules  │    │  LLM        │    │  Human Review    │  ║   │
        │   ║  │ Engine  │    │ Classifier  │    │ (asks via HAL)   │  ║   │
        │   ║  └─────────┘    └─────────────┘    └──────────────────┘  ║   │
        │   ║       │                │                    │             ║   │
        │   ║       ▼                ▼                    ▼             ║   │
        │   ║   ┌─────────────────────────────────────────────┐         ║   │
        │   ║   │       Promoter (auto-creates rules)         │         ║   │
        │   ║   └─────────────────────────────────────────────┘         ║   │
        │   ╚═══════════════════════════════════════════════════════════╝   │
        │                                                                   │
        │   ┌────────────────────────────────────────────────────────────┐ │
        │   │                   SQLite DATABASE                          │ │
        │   │  users · bank_connections · bank_accounts · ledgers ·     │ │
        │   │  line_items · transactions · transaction_entries ·         │ │
        │   │  routing_rules · classification_hints · classification_   │ │
        │   │  history · budget_targets · sync_cursors                   │ │
        │   └────────────────────────────────────────────────────────────┘ │
        └──────────────────────────────────────────────────────────────────┘
                                │                      │
                  ┌─────────────┘                      └──────────────┐
                  ▼                                                    ▼
        ┌──────────────────┐                              ┌──────────────────┐
        │   PLAID API      │                              │   LLM PROVIDER   │
        │ ────────────────│                              │ ─────────────────│
        │ • Link tokens    │                              │ • OpenAI         │
        │ • Transactions   │                              │ • Anthropic      │
        │ • Account info   │                              │ • OpenClaw       │
        │ • Update Mode    │                              │ • Local Ollama   │
        └──────────────────┘                              └──────────────────┘
                  │
                  ▼
        ┌──────────────────────────────────────────┐
        │            USER'S BANKS                  │
        │   Chase · BMO · RBC · Amex · etc.        │
        └──────────────────────────────────────────┘
```

---

## Component Breakdown

### Layer 1 — User Interface
You never call MCP tools directly. You talk to HAL in plain English (or any
MCP-aware AI agent), and it figures out which tools to call.

### Layer 2 — MCP Tools
Grouped by domain:

```
┌────────────────────────────────────────────────────────────────────┐
│ ACCOUNT          BANKS              LEDGERS          TRANSACTIONS  │
│ ───────          ─────              ───────          ────────────  │
│ create_account   create_link_token  create_ledger    sync          │
│ get_account      connect_bank       list_ledgers     list          │
│ update_prefs     list_connections   add_line_item    route         │
│                  refresh_connection list_line_items  split         │
│                  disconnect_bank    delete_ledger    confirm       │
│                                                       get_unrouted  │
├────────────────────────────────────────────────────────────────────┤
│ RULES (Tier 1)   HINTS (Tier 2)     REPORTS          AUDIT         │
│ ──────────────   ──────────────     ───────          ─────         │
│ create_rule      add_hint           get_summary      classification_│
│ list_rules       list_hints         monthly_breakdown   history    │
│ update_rule      update_hint        budget_vs_actual  llm_stats    │
│ delete_rule      delete_hint        export_excel                   │
│ test_rule                           export_csv                     │
└────────────────────────────────────────────────────────────────────┘
```

### Layer 3 — Classification Engine

```
                            New Transaction
                                  │
                                  ▼
                  ┌───────────────────────────────┐
                  │ Tier 1: Rules Engine          │
                  │                               │
                  │ Match in priority order:      │
                  │  1. Exact merchant            │
                  │  2. Plaid category + amount   │
                  │  3. Recurring date pattern    │
                  │  4. Catch-all                 │
                  └───────────────────────────────┘
                                  │
                       ┌──────────┴──────────┐
                       │                     │
                  Full match            No / partial match
                       │                     │
                       ▼                     ▼
              ┌────────────────┐  ┌──────────────────────────────┐
              │ Auto-route     │  │ Tier 2: LLM Classifier       │
              │ → DB           │  │                              │
              └────────────────┘  │ Inputs:                      │
                                  │  • Transaction details       │
                                  │  • All ledgers + line items  │
                                  │  • User's NL hints           │
                                  │  • Recent similar txns       │
                                  │                              │
                                  │ Output:                      │
                                  │  • ledger_id, line_item_id   │
                                  │  • confidence (0-1)          │
                                  │  • reasoning                 │
                                  └──────────────────────────────┘
                                                │
                                ┌───────────────┴──────────────┐
                                │                              │
                       confidence ≥ threshold      confidence < threshold
                                │                              │
                                ▼                              ▼
                       ┌────────────────┐         ┌──────────────────────┐
                       │ Auto-route     │         │ Tier 3: Human Review │
                       │ → DB           │         │                      │
                       │ + flag for     │         │ Notify user via HAL  │
                       │   later review │         │ Wait for response    │
                       └────────────────┘         │ Save as new rule     │
                                                  └──────────────────────┘
                                                              │
                                                              ▼
                                                  ┌──────────────────────┐
                                                  │ Promoter             │
                                                  │                      │
                                                  │ After 3 successful   │
                                                  │ same-merchant LLM    │
                                                  │ decisions → create   │
                                                  │ a Tier 1 rule        │
                                                  └──────────────────────┘
```

### Layer 4 — Database

```
┌──────────┐       ┌────────────────┐       ┌────────────────┐
│  users   │──────▶│bank_connections│──────▶│ bank_accounts  │
└──────────┘       └────────────────┘       └────────────────┘
     │                                              │
     │                                              │
     ├─────────▶┌──────────┐                       │
     │         │ ledgers  │                        │
     │         └──────────┘                        │
     │              │                              │
     │              ▼                              │
     │         ┌────────────┐                      │
     │         │ line_items │                      │
     │         └────────────┘                      │
     │              ▲                              │
     │              │                              │
     │              │                              ▼
     │              │                       ┌──────────────┐
     │              │              ┌────────│ transactions │
     │              │              │        └──────────────┘
     │              │              ▼
     │              │       ┌────────────────────┐
     │              └───────│ transaction_entries│ (the "interpreted" form)
     │                      └────────────────────┘
     │                              ▲
     │                              │
     ├──▶ routing_rules ────────────┘  (Tier 1)
     │
     ├──▶ classification_hints (Tier 2 — LLM prompt context)
     │
     ├──▶ classification_history (audit trail)
     │
     └──▶ budget_targets
```

### Layer 5 — External Integrations
- **Plaid:** transaction sync, bank linking, Update Mode for re-auth
- **LLM provider:** pluggable (OpenAI/Anthropic/OpenClaw/Ollama)
- **Banks:** indirectly via Plaid (Chase, BMO, RBC, Amex, etc.)

---

## Deployment Topology

### Mode A — Standalone MCP server (manual)
```
┌─────────────────────────┐
│   Your Mac              │
│                         │
│  ┌─────────────────┐    │
│  │ MCP Client      │    │
│  │ (Claude Desktop,│────┼──▶ stdio / HTTP ──▶ Friday MCP Server
│  │  mcporter, etc.)│    │                     │
│  └─────────────────┘    │                     ▼
│                         │           ┌─────────────────┐
│                         │           │  SQLite DB      │
│                         │           │  (~/.friday-bp/)│
│                         │           └─────────────────┘
└─────────────────────────┘
```

### Mode B — OpenClaw + ClawHub (recommended)
```
┌──────────────────────────────────────────────────────────────┐
│                       Your Mac                                │
│                                                               │
│  ┌────────────────────────────────────────────────────────┐  │
│  │                    OpenClaw                            │  │
│  │                                                        │  │
│  │  ┌──────┐   ┌───────────────────────────────────────┐  │  │
│  │  │ HAL  │──▶│  MCP Client (built-in via mcporter)   │  │  │
│  │  └──────┘   └───────────────────────────────────────┘  │  │
│  │                          │                             │  │
│  │                          ▼                             │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │  Friday Budgeting Pro (registered MCP server)    │  │  │
│  │  │  + SKILL.md (HAL knows how/when to use it)       │  │  │
│  │  │  + Daily cron job (auto-sync)                    │  │  │
│  │  │  + SQLite DB                                     │  │  │
│  │  │  + Plaid Link UI (served at localhost:3333)      │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────────┘  │
│                          │                                    │
│                          ▼                                    │
│            Notifications via HAL's channels                   │
│            (iMessage, Telegram, etc.)                         │
└──────────────────────────────────────────────────────────────┘
```

---

## Setup Flow (Conversational, MCP-Driven)

The whole setup is just an LLM conversation that calls MCP tools as it goes.
There's no separate wizard — the LLM (HAL or any MCP client) is the wizard.

```
  User                              LLM (HAL)                         Friday Budgeting Pro MCP
  ────                              ─────────                         ────────────────────────────────

  "set me up"  ─────────────────▶  asks "one sentence,
                                       what's your situation?"

  "work + 2 rentals" ───────────▶  ──────────── suggest_setup(description) ─────▶
                                                                              matches templates:
                                                                              - personal_individual
                                                                              - landlord_property ×2
                                       ◄───────────────────────────────────────────── returns proposed
                                                                                       structure
                                       presents proposal:
                                       "3 ledgers: Personal, Rental 1,
                                        Rental 2. Each with default rows."

  "good but rename them" ───────▶  edits the proposal locally
                                       in the conversation

  "confirm" ──────────────────▶  ──────────── apply_setup(ledgers[]) ─────────▶
                                                                              creates ledgers +
                                                                              line items in one call
                                       ◄───────────────────────────────────────────── returns committed IDs

  "connect chase" ─────────────▶  ─────────── create_link_token() ──────────▶
                                                                              returns link token
                                       opens Plaid Link UI in browser
  (completes bank login) ───────────────────── connect_bank(public_token) ───▶
                                                                              exchanges + stores
  "home depot >$50 is rental" ──▶  ──────────── add_classification_hint(text)

  "sync" ──────────────────────▶  ──────────── sync_transactions() ─────────▶
                                                                              pulls + classifies
                                       ◄───────────────────────────────────────────── returns summary
                                       "239 sorted, 8 to review"
```

### Setup-related MCP tools

| Tool | What it does |
|---|---|
| `list_templates()` | Returns all built-in ledger templates with default line items |
| `suggest_setup(description)` | Maps a natural-language description to a proposed ledger structure (no commit) |
| `apply_setup(ledgers[])` | Commits a proposed structure in one call — LLM sends the whole tree |
| `quick_setup(profile)` | One-shot setup for common cases (`individual`, `couple`, `landlord_n`, `freelancer`, `small_business`, `nonprofit`) |

The LLM's job is to:
1. Ask one human-sounding question to understand the situation
2. Call `suggest_setup` with that description
3. Present the proposal naturally ("Here's what I'm thinking...")
4. Iterate verbally on edits ("add X", "drop Y", "rename A to B")
5. Call `apply_setup` to commit

Defaults handle 90% of the structure; the conversation handles the 10% that's
user-specific.

---

## Data Flow Examples

### Example 1: New transaction comes in (typical path)

```
Day 1 — Sync runs
   │
   ├─▶ Plaid: /transactions/sync
   │     returns 47 new transactions
   │
   ├─▶ For each transaction:
   │     │
   │     ├─▶ Insert into `transactions` table
   │     │
   │     ├─▶ Tier 1: Rules Engine
   │     │     ├─ "AMAZON.COM" → matches existing rule
   │     │     │   → entry: Personal / Shopping
   │     │     │   → confidence: 1.0
   │     │     │   → source: rule
   │     │     │
   │     │     └─ "JANE'S COFFEE" → no rule
   │     │         → goes to Tier 2
   │     │
   │     ├─▶ Tier 2: LLM Classifier
   │     │     Prompt includes: hints, ledger tree, similar past txns
   │     │     LLM: "Coffee shop, small amount, recurring vendor →
   │     │           Personal / Dining (confidence 0.92)"
   │     │     → confidence ≥ 0.75 → auto-route
   │     │
   │     └─▶ "HOME DEPOT — $483"
   │           Tier 2 LLM: "Could be personal renovation or rental
   │                        maintenance. Hint says 'over $50 → likely
   │                        rental'. (confidence 0.62)"
   │           → confidence < 0.75 → Tier 3
   │
   └─▶ HAL notifies user:
         "Hey, got a $483 Home Depot charge from Saturday. My best
          guess is rental property maintenance based on your hints,
          but I'm only 62% sure. Was this for one of the rentals,
          or a personal project?"
```

### Example 2: User responds

```
User: "That was for 90 Glen Everest, new flooring"
   │
   └─▶ HAL calls: route_transaction(
         transaction_id="txn_abc123",
         allocations=[{
           ledger_id="ledger_glen_everest",
           line_item_id="li_maintenance",
           amount=483.00,
           note="new flooring"
         }]
       )
   │
   └─▶ System:
         ├─ Insert transaction_entry
         ├─ Save classification decision to history
         └─ Update LLM context for similar future txns
```

### Example 3: Excel export

```
User: "Export my finances to Excel"
   │
   └─▶ HAL calls: export_excel(ledger_ids=null, years=[2025, 2026])
   │
   └─▶ For each ledger:
         ├─ Create workbook: "{ledger_name}.xlsx"
         ├─ For each year:
         │   └─ Create sheet with:
         │      - Income rows (green)
         │      - Expense rows (red)
         │      - Net row (bold)
         │      - YTD column
         ├─ Summary sheet (multi-year)
         └─ Raw transactions sheet
   │
   └─▶ Save to user's configured output directory
```

---

## Why This Architecture

| Decision | Why |
|---|---|
| MCP server (not REST API) | LLM-native interface — agents call it directly |
| 3-tier classification | Fast for known stuff, smart for new stuff, accurate for edge cases |
| Natural-language hints | More expressive than regex; matches how humans think about money |
| Split transactions | Real life has shared expenses; one txn → many entries |
| SQLite | Zero setup; portable; the data is yours, in a file you can back up |
| ClawHub packaging | One-command install; HAL knows how to use it out of the box |
| Encrypted access tokens | Plaid tokens never sit in plaintext on disk |
| Pluggable LLM | Use OpenAI, Anthropic, OpenClaw routing, or local Ollama — your choice |
| Auto-promotion (Tier 2 → 1) | System gets cheaper and faster over time |

---

## What's NOT in scope (yet)

- Investment tracking (assets/liabilities balance sheet) — schema supports it but tools not built
- Multi-currency FX conversion — currency stored but not converted
- Tax categorization for filing — categories exist, but no tax-specific reports
- Mobile app — MCP server is headless; UI is whatever client you use
- Direct integrations with non-Plaid sources (Wealthsimple, crypto) — future extensibility point
