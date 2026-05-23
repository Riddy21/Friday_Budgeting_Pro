# Friday Budgeting Pro

> AI-powered personal finance, on your own machine.

A local budgeting tool that uses AI to do the boring work for you: connecting
to your banks, classifying every transaction, and keeping a spreadsheet
up to date. It runs as a small daemon on your Mac with **multiple equal
ways to interact with it** — no single one is the "main" way.

**Single user. Local-only. AI does the heavy lifting; you stay in control.**

📐 [Read the architecture](./ARCHITECTURE.md) (this is the source of truth)

---

## How You Use It

The product runs as a small daemon. Three equal-peer ways to interact with
it — pick whichever fits the moment, mix and match freely:

| Adapter | What it covers in v0.1 |
|---|---|
| **🖥️ Web UI** (`127.0.0.1:6789`) | Setup + Profile (settings, password, sync, export, **linked accounts list**) + a minimal **Ledgers** page. |
| **💬 MCP** (OpenClaw, Claude Desktop, any MCP client) | Full feature surface: connect/disconnect banks, edit ledgers, review classifications, query spending, trigger exports. Conversational when paired with an LLM. |
| **⏰ Scheduler** (background) | Daily auto-sync, drift detection, proactive re-auth alerts via your chosen notification channel. |

None of these is "primary." The UI is intentionally small — it handles
setup, the things you tweak occasionally, and bank management. Reviewing
transactions, running queries, and anything fancy still lives in MCP or
the background.

---

## Install

```bash
clawhub install friday-budgeting-pro
```

This installs the daemon and starts it at user login (via launchd). The
first time it boots, open `http://127.0.0.1:6789` in your browser to
finish setup.

---

## First Run

When you visit `http://127.0.0.1:6789` for the first time, you see a
small setup wizard (4 short screens):

1. **Set a password** — protects your local dashboard.
2. **Pick how you want to be notified** about ambiguous transactions:
   - "Through OpenClaw chat" (if you use it)
   - "macOS notifications"
   - "Just show me a banner in the UI"
3. **Connect your first bank** — click **+ Connect a bank** and follow
   the Plaid login.
4. **Done.** Lands on your Profile page.

The system picks sensible defaults for everything else (Personal ledger
with standard rows, daily 6 AM sync, LLM confidence threshold 0.75).
Adjust any of it later from the Profile page or via MCP.

---

## What the UI Looks Like (v0.1)

Three things, kept minimal:

**Setup wizard** — once, on first launch.

**Profile page** — the main ongoing page. Has:
- Display name + notification preference + LLM confidence slider
- Change password
- Log out
- Read-only system info (Plaid env, last sync time, daemon uptime)
- **Sync now** button
- **Export to Excel** button
- **Linked Accounts** — compact list of connected banks with status pills
  and **Reconnect / Disconnect / + Connect a bank** buttons. No fancy
  cards, just a list.

**Ledgers page** — minimal structure editor. List your ledgers (default:
Personal), click into one to add/rename/remove line items, add new
ledgers when you need them (e.g. for a rental property).

Reviewing classifications, running queries, anything beyond the basics —
those still happen through the MCP adapter (your OpenClaw agent or any
other MCP client).

---

## What AI Does for You

Every transaction goes through a three-tier classifier:

1. **Rules** — exact merchant matches you've already confirmed. Free, instant.
2. **LLM** — for new merchants, the LLM reasons about the transaction
   using your hints and recent similar transactions, and auto-routes if
   confident enough.
3. **Review queue** — if it's unsure, the transaction lands in a review
   queue. You'll get a notification through your chosen channel.

After 3 successful classifications of the same merchant, it becomes a
Tier 1 rule automatically. The longer you use it, the less it asks.

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
        — let me know when you're done.

You:    Export this year to Excel
Agent:  ✓ Wrote Personal Finances.xlsx to your Documents folder.

Agent:  Heads up — got a $312 Costco charge from yesterday that I'm
        not sure about. Personal groceries, or something else?
You:    Half personal groceries, half supplies for work
Agent:  ✓ Split 50/50, saved as a hint for similar charges.
```

Same engine, just a different way in. Other MCP clients (Claude Desktop,
Cursor, mcporter on the CLI) work too — anywhere you can call MCP tools.

---

## Privacy & Security

- 🏠 **Local-only.** Nothing this app runs is reachable from the public
  internet. Everything binds to `127.0.0.1`.
- 🔒 **Plaid tokens encrypted at rest** (Fernet, key in macOS Keychain)
- 🔑 **Password hashed with argon2id**, never sent to the browser
- 📁 **Your data lives in `~/.friday-bp/data.db`** (SQLite, yours)
- 🚫 **No telemetry**, no cloud sync, no third parties except Plaid + your
  chosen LLM
- ⏱️ **Sessions persist 7 days idle**, then re-login required
- 🛡️ **Rate-limited logins** (5 failed attempts → lockout)

See [ARCHITECTURE.md § Security](./ARCHITECTURE.md#security) for the full
threat model.

---

## What This Is Not (v0.1)

- Not a full web app. The UI is deliberately small: setup and profile only.
- Not a SaaS. Everything runs on your Mac, no cloud account.
- Not a generalized platform. Personal finances only.
- Not chat-only either. MCP is one of several equal-peer ways in.

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

---

## License

MIT
