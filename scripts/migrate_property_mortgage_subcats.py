#!/usr/bin/env python3
"""One-off migration: add mortgage sub-category line items to EXISTING property
ledgers (#271).

Non-destructive:
  - Only ADDS missing line items; never deletes or renames existing ones.
  - Existing "Mortgage" / "Loan" line items (and any transaction_entries routed
    to them) are left untouched, so historical data is preserved.
  - Idempotent: re-running skips sub-categories that already exist.

The newly-added sub-categories match create_property_ledger's seeding so new
mortgage payments can be split into principal / interest / escrow.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid

import server.paths as paths

_NEW_SUBCATS = [
    ("Mortgage – Principal", "expense"),
    ("Mortgage – Interest", "expense"),
    ("Mortgage – Property tax (escrow)", "expense"),
    ("Mortgage – Insurance (escrow)", "expense"),
]


def main() -> int:
    conn = sqlite3.connect(paths.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        ledgers = conn.execute(
            "SELECT id, name FROM ledgers WHERE type = 'property'"
        ).fetchall()
        if not ledgers:
            print("No property ledgers found — nothing to migrate.")
            return 0

        total_added = 0
        for led in ledgers:
            existing = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM line_items WHERE ledger_id = ?", (led["id"],)
                ).fetchall()
            }
            added_here = []
            for name, item_type in _NEW_SUBCATS:
                if name in existing:
                    continue
                conn.execute(
                    "INSERT INTO line_items (id, ledger_id, name, item_type) "
                    "VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), led["id"], name, item_type),
                )
                added_here.append(name)
                total_added += 1
            if added_here:
                print(f"  {led['name']}: added {len(added_here)} sub-categories")
                for n in added_here:
                    print(f"      + {n}")
            else:
                print(f"  {led['name']}: already up to date")
        conn.commit()
        print(f"\nDone. Added {total_added} line item(s) across "
              f"{len(ledgers)} property ledger(s).")
        print("Existing 'Mortgage'/'Loan' lines and their entries were left intact.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
