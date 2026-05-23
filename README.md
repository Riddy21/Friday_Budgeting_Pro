# Friday Budgeting Pro

> Personal finance, by chat. That's the whole pitch.

A minimal [OpenClaw](https://openclaw.ai) skill that lets you manage your
money by chatting with your OpenClaw agent. It connects to your banks,
classifies your spending, asks you when it's unsure, and exports to Excel
when you want it to.

**Single user. Conversational only. No UI, no commands, no config files.**

📐 [Read the architecture](./ARCHITECTURE.md) (this is the source of truth)

---

## What You Can Do

Once installed, just say things to your OpenClaw agent like:

- *"Connect my Chase account"*
- *"How much did I spend on groceries this month?"*
- *"Show me my spending for May"*
- *"Export my finances to Excel"*
- *"Reclassify that Home Depot charge as personal"*

The agent calls Friday Budgeting Pro's MCP tools and handles the rest.

---

## Install

```bash
clawhub install friday-budgeting-pro
```

That's it. Next time you mention finances to your agent, the setup
conversation starts automatically.

---

## First Conversation (Setup)

```
You:    Set me up with Friday Budgeting Pro

Agent:  Quick setup — three questions.

        1) What banks should I connect? Just list them and I'll open
           Plaid for each.

You:    Chase and Amex

Agent:  [opens Plaid for Chase, waits, then Amex]
        ✓ Both connected.

        2) Tracking anything besides personal? Most people just have one
           "Personal" ledger. Are you keeping anything separate (like a
           rental property or business)?

You:    Just personal

Agent:  ✓ Created Personal ledger with the standard rows.

        3) Any quick rules I should know? For example: "Home Depot is
           always personal", "Uber Eats is dining not transport", that
           kind of thing. Skip if you don't have any in mind.

You:    Tim Hortons is always personal, and treat my partner's spending
        the same as mine

Agent:  ✓ Saved 2 hints. Scheduled daily sync at 6 AM (OpenClaw cron).
        Pulling 90 days of history now...

        ✓ 247 transactions. 239 sorted, 8 I want to ask about. I'll ping
        you about them one at a time. Ready to start?

You:    Yes

Agent:  Saturday May 14, $127 at HOME DEPOT. My guess: personal home
        stuff (78% sure). Correct?

You:    Yes

Agent:  ✓ Got it. Next: Tuesday May 17, $89 at COSTCO. Personal groceries?

[... and so on, 8 times total]

Agent:  All sorted. Daily sync is on. Just ask me anything anytime.
```

That's the whole setup. Three questions, a few classification check-ins,
done.

---

## Daily Use

You don't run anything. Your OpenClaw agent pings you when there's something
to review, otherwise it's silent.

```
Agent:  Morning — synced overnight. Found one transaction I'm unsure about:
        $312 at COSTCO yesterday. Personal groceries, or something else?

You:    Half personal groceries, half supplies for work

Agent:  ✓ Split 50/50, saved as a hint for similar future Costco charges.
```

Want a summary?

```
You:    How's this month looking?

Agent:  May 2026 so far:

        Income:    $6,500
        Expenses:  $3,247

        Top categories:
          Groceries:      $487
          Dining:         $312
          Subscriptions:  $89

        Net: +$3,253
```

Want Excel?

```
You:    Export this year to Excel

Agent:  ✓ Wrote Personal Finances.xlsx to your Documents folder.
        One sheet per year, rows for each category, monthly columns.
```

---

## How Classification Works

Three tiers, fastest first:

1. **Rules** — exact merchant matches you've already confirmed. Free, instant.
2. **LLM** — if no rule matches, the LLM reasons about it using your hints
   and recent transactions. Auto-routes if it's confident enough.
3. **You** — if it's unsure, your agent pings you in chat. Your answer is
   saved as a new rule.

After the same merchant gets classified correctly 3 times in a row, it
becomes a Tier 1 rule automatically. The longer you use it, the less it asks.

---

## Privacy

- Everything lives in `~/.friday-bp/data.db` (SQLite, yours)
- Plaid tokens encrypted with Fernet
- LLM calls go through whatever provider OpenClaw is configured with —
  no separate API key needed
- No telemetry, no analytics, no cloud sync
- Nothing on this skill is reachable from the public internet — see
  [ARCHITECTURE.md § Security](./ARCHITECTURE.md#security)

---

## What This Is Not

This is not a generalized budgeting platform. It's specifically:

- Single user (you)
- Personal finances (no business/nonprofit/multi-property by default)
- Conversational only (no UI, no CLI)
- OpenClaw-native (uses your agent's chat, OpenClaw cron, OpenClaw
  notifications)

If you outgrow it later, you can add ledgers/templates over time, but the
defaults assume "just personal finance, just for me."

---

## Troubleshooting

**"Bank connection broken"** → Just tell your agent "reconnect my [bank]".
It'll open Plaid Update Mode.

**"I want to redo a classification"** → "Reclassify the X charge from
[date] as Y."

**"Where's my data?"** → `~/.friday-bp/data.db`. Back this up.

**"How do I uninstall?"** → `clawhub uninstall friday-budgeting-pro`. Your
data file stays unless you delete it manually.

---

## License

MIT
