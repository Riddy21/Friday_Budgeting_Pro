# Contributing — Agent Protocol

This repo is built by an agent swarm. Humans review, agents do most of the work.
This doc is the operating manual for agent contributors.

## The Loop

1. **Pick an issue** labeled `task` that has no assignee.
   - Filter: `is:open is:issue label:task no:assignee`
   - Comment on it: `Claiming this — starting now.`
   - Self-assign.

2. **Branch** off `main`:
   ```
   git checkout -b agent/<issue-num>-<short-slug>
   ```
   Example: `agent/12-fastmcp-skeleton`

3. **Read the dependencies.** Most tickets say "depends on: X". Don't start a
   ticket whose dependencies aren't merged yet.

4. **Implement.** Stay scoped to the ticket. If you find scope creep, open a
   new issue for it instead of expanding the current PR.

5. **Test.** Every ticket has a test requirement in its body. CI runs
   `pytest -q` on every push — green CI is non-negotiable.

6. **Interactive sanity check** (mandatory before opening a PR):
   - **UI tickets:** Start the server and use Peekaboo browser automation to verify the affected page loads and the core flow works.
   - **MCP tickets:** Call the new/changed tools directly in Python and confirm they return real output (not `{'status': 'not_implemented'}` or errors).
   - **Infra/docs/schema tickets:** pytest is sufficient, no interactive check needed.
   - Include a one-line summary of what you tested in the PR body under **Interactive check:**

7. **Open a PR.**
   - Title: `[P<phase>] <ticket title>`
   - Body: must include `Closes #<issue-num>` so the issue auto-closes on merge
   - Use the PR template (auto-populated)

8. **Wait for review.** A human (or a reviewer agent) approves and merges.
   Don't self-merge.

## Code Rules

- **Python 3.11+**
- **No dependencies beyond what's in `requirements.txt`.** Add new ones in a
  separate commit with justification in the PR body.
- **Keep modules small.** ARCHITECTURE.md says the whole codebase fits in
  ~10 files. Respect that.
- **No features outside ARCHITECTURE.md.** If you think something's missing,
  open an issue. Don't sneak it in.
- **Tests live in `tests/`.** Mirror the source layout
  (`server/db.py` → `tests/test_db.py`).

## Anti-Patterns (Don't Do These)

- ❌ Refactoring code outside your ticket
- ❌ Adding multi-user logic ("for future flexibility") — multi-profile is ✅ (see issue #131); concurrent sessions are ❌
- ❌ Building a CLI on top of MCP tools
- ❌ Adding a web UI element beyond the existing Plaid Link page
- ❌ Pulling in ORMs, async frameworks, or "nice to have" libraries
- ❌ Bundling multiple tickets in one PR
- ❌ Committing secrets, real Plaid tokens, or production data

## When in Doubt

- Re-read [ARCHITECTURE.md](./ARCHITECTURE.md) — it's the source of truth.
- Check [PLAN.md](./PLAN.md) for the implementation order.
- Leave a comment on the issue asking for clarification.

## Reviewer Notes

Reviewers (human or agent) should reject PRs that:
- Don't link an issue
- Have no tests for new code
- Touch files outside the ticket scope
- Add scope not covered by the ticket
- Break CI
