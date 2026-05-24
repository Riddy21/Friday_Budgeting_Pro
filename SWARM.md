# SWARM.md — Agent Swarm Protocol

How the Friday Budgeting Pro repo gets built by a swarm of AI agents.
Read this before doing any PM or worker activity.

## Roles

### PM (one persistent Opus agent)
- Long-lived session keyed `session:friday-bp-pm`
- Triggered by a cron job every 30 minutes
- Never writes code. Only: triages tickets, spawns workers, reviews PRs, merges.
- Adheres strictly to [ARCHITECTURE.md](./ARCHITECTURE.md) — if a worker's PR violates it, request changes.

### Workers (up to 3 short-lived agents at a time)
- One per ticket
- Spawned by the PM via `sessions_spawn` (mode=`run`, isolated context)
- Picks the ticket up, branches, implements, tests, pushes, opens PR
- Exits when the PR is open. Does NOT wait for review.
- **Model selection (cost-aware):**
  - **Haiku** (cheapest) for: docs-only tickets, single-file stubs, schema or config-only changes, smoke tests, any `good-first-issue` labeled ticket
  - **Sonnet** for everything else — default worker model
  - **Opus** is reserved for the PM. Workers do not use it.

## PM Tick Loop (every 30 min)

On every tick, the PM does this in order:

### 1. Health check on running workers
- `subagents action=list recentMinutes=60`
- If any worker has been running >60 min with no PR opened, **kill it** (`subagents action=kill`) and post a comment on the issue: "Worker timed out, re-queueing."
- If a worker errored out, same thing.

### 2. Review open PRs
For each open PR (`gh pr list --state open`):
- Read the PR body + the linked issue
- Run `pytest -q` locally (in the worktree if used) — must be green
- Check the diff against:
  - The linked issue's scope (no scope creep)
  - ARCHITECTURE.md (no design violations)
  - CONTRIBUTING.md anti-patterns (no multi-user, no CLI, no over-engineering)
- **Decide:**
  - ✅ Looks good → `gh pr review --approve`, then `gh pr merge --squash --delete-branch`
  - ❌ Issues → `gh pr review --request-changes --body "<specific feedback>"`, leave PR open. Next tick, when worker has fixed it (re-pushed), re-review.
  - ⚠️ Borderline → comment with suggestions but approve if the spirit is right. Workers are AI — don't nitpick.

### 3. Spawn next worker(s) (if room)
- Concurrency: **at most 5 active workers at a time**.
- Workers must work on **non-overlapping tickets** (different files where possible) to avoid merge conflicts. If only conflicting tickets are eligible, run them serially.
- Find eligible tickets:
  - Open, labeled `task`, no assignee, no linked open PR
  - All dependencies (listed in the issue body's "Depends on") are closed/merged
- Prefer tickets in this order: phase-1, phase-2, phase-3, ..., infra, then good-first-issue across phases as filler
- Pick the cheapest model that can do the job (see Model selection above).
- Spawn each worker:
  ```
  sessions_spawn(
    runtime="subagent",
    mode="run",
    model="haiku" | "sonnet",  // never opus
    label=f"friday-bp-worker-{issue_number}",
    task=f"You are a worker agent. Read CONTRIBUTING.md and ARCHITECTURE.md in the repo at /Users/hal9000/.openclaw/workspace/bank-transactions. Implement issue #{n}. Follow the Loop. Open a PR with 'Closes #{n}' in the body. Exit when the PR is open.",
    cwd="/Users/hal9000/.openclaw/workspace/bank-transactions",
    cleanup="keep",
    runTimeoutSeconds=2700  // 45 min hard cap
  )
  ```
- Self-assign each issue: `gh issue edit <n> --add-assignee @me`
- Comment on each issue: "Assigned to worker `friday-bp-worker-<n>`. Status updates as PR progresses."

### 4. Log the tick
- Append a short summary to `~/.friday-bp-swarm/pm.log`:
  ```
  2026-05-23T18:30 — reviewed 2 PRs (merged #11, requested changes on #12). Spawned worker for #13. Workers running: 1.
  ```

### 5. Done
- Yield. Next tick happens via cron in 30 min.

## Worker Loop

Workers receive their initial task and follow CONTRIBUTING.md's "The Loop":

1. Read CONTRIBUTING.md, ARCHITECTURE.md, the linked issue.
2. Check `gh pr list` for an existing PR — if there is one for this issue, abort (PM messed up; don't double-work).
3. `git checkout -b agent/<issue-num>-<slug>` off latest `main`.
4. Implement strictly within the ticket's scope.
5. Run tests locally (`pytest -q`). Must be green.
6. **Interactive sanity check (mandatory for UI and MCP tickets):**
   - **UI tickets:** Start the UI server (`python3 -m uvicorn ui.server:app --host 0.0.0.0 --port 6789`) and use Peekaboo browser automation to verify the affected page loads, key elements are present, and the core flow works. Do not open a PR until this passes.
   - **MCP tickets:** Start the server and call the new/changed MCP tools directly (`python3 -c "from server.main import <tool>; print(<tool>(...))"`) to verify they return expected output, not `{'status': 'not_implemented'}` or errors.
   - **Other tickets (infra, docs, schema):** skip interactive check, pytest is sufficient.
7. `git add . && git commit -m "..."` with a clear message.
8. `git push -u origin <branch>`.
9. `gh pr create --title "[Pn] <ticket title>" --body "Closes #<n>\n\n<short summary>\n\n**Interactive check:** <one line describing what was tested and that it passed>"`.
10. Exit. The PM picks it up from here.

## Communication

- Workers do NOT message the PM directly. All async via GitHub (issues, PR comments, PR status).
- PM does NOT interrupt running workers. If steering is needed, it kills + re-spawns with updated context.
- All decisions visible in GitHub history (PRs, issue comments) — for human auditability.

## Stopping the Swarm

Ridvan stops the swarm with:
```
cron remove friday-bp-pm-tick
subagents action=kill target=<any active worker>
```

Or asks his main HAL session: "pause the friday budgeting swarm."

## Honest Limits

- AI workers will sometimes get stuck or write nonsense. The PM kills + retries; if a ticket fails 3 times in a row, the PM closes the worker, comments on the issue with a `human-needed` label, and moves on.
- Some tickets need real Plaid credentials or real bank flows to test — workers should mock those and the PM should accept reasonable mocks. End-to-end (#28) is for the human to run manually.
- This swarm builds the code. **The human (Ridvan) still does the final manual E2E test, the Plaid Production setup, and the `clawhub publish`.**
