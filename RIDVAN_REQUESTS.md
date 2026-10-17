# Ridvan's requests (2026-10-17) — Account/dedup/sync overhaul

Captured from iMessage so nothing is lost across session resets.

## 1. True duplicate accounts (same account imported twice under different names)
Confirmed pairs (by last-4 account number):
- `RBC Advantage Banking (3861)` — 4b98466f & 32bce787, both bal=3745.37
- `RBC High Interest eSavings (1822)` — 7c2fd7be & 0f560367, both bal=0.18
- `Chequing 018225247077 (7077)` == `RBC Day to Day Banking (7077)` — same 7077 account, both bal=5399.12
- `Account (0041)` == `Credit Line (0041)` — same 0041 account

Cause: a second RBC sync re-imported accounts under slightly different names.
Action: mark duplicates as is_duplicate/skip so balances AND transactions don't double-count.
=> Must verify TRANSACTIONS are also not duplicated (dedup txns, not just accounts).

## 2. Group/separate by ACCOUNT, not by bank
UI currently groups by institution. Ridvan wants separation by account.

## 3. Separate Dollar vs Ridvan accounts
Add an owner concept (Ridvan / Dollar) and separate them in the UI/reports.
Need the mapping from Ridvan (ask or infer). Candidate list by last-4:
- 9651 — RBC Rewards+ Visa
- 7077 — RBC Day to Day Banking / Chequing (dup)
- 7180 — RBC U.S. High Interest eSavings (USD)
- 1822 — RBC High Interest eSavings (dup)
- 3861 — RBC Advantage Banking (dup)
- 2675 — Signature RBC Rewards Visa (bal -333.52)
- 8922 — Find & Save
- 0041 — Credit Line / "Account" (dup)
- 103 Ardsley ($357,329)
- 90 Glen Everest ($350,745)

## 4. Account types + currencies
Take account type and currency into account everywhere (CAD/USD etc.).
Net worth / totals must respect currency conversion and type.

## 5. Mortgages = DEBTS
- Mortgage/loan balances must be NEGATIVE (liabilities).
- Put them in a fully separate category (Debts/Liabilities), not mixed with assets.

## 6. Investment accounts
- Detect investment accounts (balances change WITHOUT transactions).
- Separate them out into their own category.

## 7. Routine balance sync (no-transaction updates)
- Sync balances routinely and update totals even when there are NO transactions
  (investment + mortgage balances drift without txns).
- Keep net worth / totals current from balance refreshes.

## 8. Transaction pull size
- Make sure the transaction pull isn't too big (90-day window default — DONE in #270,
  verify it's active and applied to the dashboard button).

## Status / done so far
- #270 sync window (90d) + batched classify + cheaper model — COMMITTED (13e749b)
- #271 mortgage sub-categories + liabilities pull + payment split — COMMITTED (cb74828)
- dedup Pass 5 (empty same-conn same-suffix) — COMMITTED (07ee43e)
- test/security fixes (loopback bind, savings math) — COMMITTED (851d514)

## Still TODO (this batch)
- [ ] Accounts page renders each account TWICE (template double-render bug) — even
      non-duplicated accounts (e.g. 7077) show twice. Fix accounts.html loop.
- [ ] Verify + run account dedup on live DB for 7077/0041 cross-name pairs
      (Pass 4 by txn overlap; may need a last-4-suffix cross-name pass).
- [ ] Transaction-level dedup (ensure no double-imported txns inflate totals).
- [ ] Owner field (Ridvan/Dollar) + UI separation by account/owner.
- [ ] Mortgages as negative liabilities in a separate Debts category.
- [ ] Investment account detection + separate category.
- [ ] Routine balance-only sync that updates net worth without transactions.
- [ ] Confirm 90-day pull window is active on the dashboard sync button.
