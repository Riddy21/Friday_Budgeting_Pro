#!/usr/bin/env python3
"""
scripts/wipe.py — Nuclear-option cleanup for Friday Budgeting Pro.

Calls Plaid /item/remove for every linked bank connection (so Plaid stops
billing and invalidates the access tokens on their side), then wipes all
bank data from the local SQLite database.

What is wiped
-------------
  - auto_promoted_rules_log
  - transaction_entries
  - transactions
  - sync_cursors
  - bank_accounts
  - bank_connections

What is preserved
-----------------
  - users / sessions / app_config
  - ledgers / line_items
  - classification_rules / routing_rules
  - classification_hints
  - plaid_config
  - setup_interview

Usage
-----
  python3 scripts/wipe.py               # full wipe (prompts for confirmation)
  python3 scripts/wipe.py --dry-run     # show what would happen, no changes made
  python3 scripts/wipe.py --yes         # skip interactive prompt (for scripts/CI)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Make sure the project root is on sys.path regardless of CWD.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import server.crypto  # noqa: E402
import server.paths  # noqa: E402
from server.db import get_db  # noqa: E402
from server.providers.plaid import PlaidProvider  # noqa: E402

# ---------------------------------------------------------------------------
# Tables to wipe, in deletion order (children before parents).
# ---------------------------------------------------------------------------
WIPE_ORDER: list[str] = [
    "auto_promoted_rules_log",
    "transaction_entries",
    "transactions",
    "sync_cursors",
    "bank_accounts",
    "bank_connections",
]


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


def _load_connections(conn) -> list[dict]:
    # Use plaid_revocation_log (not bank_connections) so that tokens are
    # available for revocation even if the bank_connections rows were already
    # deleted.  Only rows that haven't been successfully revoked yet are
    # returned.  (#265)
    rows = conn.execute(
        "SELECT id, institution_name, access_token_encrypted AS plaid_access_token_encrypted, plaid_env "
        "FROM plaid_revocation_log WHERE revoked=0"
    ).fetchall()
    return [dict(r) for r in rows]


def _revoke_on_plaid(connection: dict, dry_run: bool) -> dict:
    """
    Attempt to call Plaid /item/remove for a single connection.

    Returns a result dict:
        {"id": ..., "institution": ..., "revoked": bool, "error": str|None}
    """
    cid = connection["id"]
    inst = connection["institution_name"] or "(unknown)"

    if dry_run:
        return {"id": cid, "institution": inst, "revoked": False, "dry_run": True, "error": None}

    try:
        access_token = server.crypto.decrypt(connection["plaid_access_token_encrypted"])
        plaid_env = connection["plaid_env"] or os.environ.get("PLAID_ENV", "sandbox")
        provider = PlaidProvider(env=plaid_env)
        result = provider.remove_item(access_token)
        revoked = result.get("revoked", False)
        if revoked:
            # Mark revoked in the log so retry_pending_revocations() skips it
            try:
                _db = get_db(server.paths.DB_PATH)
                _db.execute(
                    "UPDATE plaid_revocation_log "
                    "SET revoked=1, revoked_at=unixepoch() WHERE id=?",
                    (cid,),
                )
                _db.commit()
                _db.close()
            except Exception:  # noqa: BLE001
                pass  # best-effort; don't abort wipe if log update fails
        return {
            "id": cid,
            "institution": inst,
            "revoked": revoked,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"id": cid, "institution": inst, "revoked": False, "error": str(exc)}


def run(dry_run: bool, skip_prompt: bool, db_path=None) -> None:  # noqa: C901
    if db_path is None:
        db_path = server.paths.DB_PATH
    db_path = Path(db_path)

    if not db_path.exists():
        print(f"[wipe] Database not found at {db_path} — nothing to do.")
        sys.exit(0)

    conn = get_db(db_path)

    # ------------------------------------------------------------------ #
    # Gather counts and connection list before touching anything           #
    # ------------------------------------------------------------------ #
    connections = _load_connections(conn)
    counts: dict[str, int] = {table: _count(conn, table) for table in WIPE_ORDER}
    conn.close()

    total_rows = sum(counts.values())

    # ------------------------------------------------------------------ #
    # Print the dry-run / pre-wipe summary                                 #
    # ------------------------------------------------------------------ #
    tag = "[DRY RUN] " if dry_run else ""
    print()
    print(f"{tag}Friday Budgeting Pro — DB Wipe Summary")
    print("=" * 55)
    print(f"  Database: {db_path}")
    print(f"  Bank connections: {len(connections)}")
    print()

    if connections:
        print("  Connections to revoke on Plaid:")
        for c in connections:
            inst = c["institution_name"] or "(unknown institution)"
            env = c["plaid_env"] or "sandbox"
            print(f"    • {inst}  (id={c['id'][:8]}…, env={env})")
        print()

    print("  Rows that will be deleted:")
    for table in WIPE_ORDER:
        print(f"    {table:<30} {counts[table]:>6} rows")
    print(f"    {'TOTAL':<30} {total_rows:>6} rows")
    print()

    if total_rows == 0 and not connections:
        print("Nothing to wipe — database is already clean.")
        return

    # ------------------------------------------------------------------ #
    # Confirm before proceeding (unless --yes or --dry-run)                #
    # ------------------------------------------------------------------ #
    if dry_run:
        print("Dry run complete — no changes were made.")
        return

    if not skip_prompt:
        try:
            answer = input("Type 'yes' to confirm wipe: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(1)
        if answer != "yes":
            print("Aborted.")
            sys.exit(1)

    # ------------------------------------------------------------------ #
    # Step 1: Revoke each connection on Plaid (best-effort)                #
    # ------------------------------------------------------------------ #
    if connections:
        print()
        print("Revoking connections on Plaid…")
        revoke_results = [_revoke_on_plaid(c, dry_run=False) for c in connections]
        for r in revoke_results:
            status = "✓ revoked" if r["revoked"] else ("✗ failed" if r["error"] else "– skipped")
            line = f"  {r['institution']} ({r['id'][:8]}…): {status}"
            if r["error"]:
                line += f"\n      error: {r['error']}"
            print(line)

    # ------------------------------------------------------------------ #
    # Step 2: Wipe local DB tables                                         #
    # ------------------------------------------------------------------ #
    print()
    print("Wiping local database…")
    conn = get_db(db_path)
    try:
        for table in WIPE_ORDER:
            before = _count(conn, table)
            conn.execute(f"DELETE FROM {table}")  # noqa: S608
            print(f"  {table:<30} deleted {before} rows")
        conn.commit()
    finally:
        conn.close()

    print()
    print("Wipe complete.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="wipe.py",
        description=(
            "Revoke all Plaid bank connections and wipe bank/transaction data "
            "from the local Friday Budgeting Pro database."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be removed without making any changes.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Skip the interactive confirmation prompt.",
    )
    args = parser.parse_args()

    # Initialise crypto (needed to decrypt stored tokens).
    # In dry-run mode we still call init_crypto so we can show connection info,
    # but we never actually decrypt or call Plaid.
    try:
        server.crypto.init_crypto()
    except RuntimeError as exc:
        if args.dry_run:
            print(f"[wipe] Warning: crypto unavailable ({exc}). Token decryption skipped.")
        else:
            print(f"[wipe] Error: {exc}")
            sys.exit(1)

    run(dry_run=args.dry_run, skip_prompt=args.yes)


if __name__ == "__main__":
    main()
