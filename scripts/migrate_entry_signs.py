#!/usr/bin/env python3
"""
Migration: Align transaction_entries.amount signs with transactions.amount.

After PR #365, entry amounts preserve the raw transaction sign (negative = income,
positive = expense). Old entries had signs flipped. This migration corrects them.

Usage:
    python3 scripts/migrate_entry_signs.py [--dry-run] [--db PATH]

Options:
    --dry-run   Preview changes without modifying the database
    --db PATH   Path to database (default: ~/.friday-bp/data.db)
"""

import argparse
import sqlite3
from pathlib import Path


def get_mismatched_entries(conn: sqlite3.Connection) -> list[dict]:
    """Find entries where the sign doesn't match the raw transaction."""
    cursor = conn.execute("""
        SELECT
            te.id AS entry_id,
            te.amount AS entry_amount,
            te.amount_home AS entry_amount_home,
            t.id AS transaction_id,
            t.amount AS raw_amount,
            t.merchant,
            t.date,
            te.entry_type,
            te.source
        FROM transaction_entries te
        JOIN transactions t ON te.transaction_id = t.id
        WHERE (t.amount < 0 AND te.amount > 0)
           OR (t.amount > 0 AND te.amount < 0)
    """)

    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


def fix_entry_signs(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    """
    Fix mismatched entry signs by setting te.amount = t.amount.

    Also fixes amount_home to match the sign of the new amount.
    """
    mismatched = get_mismatched_entries(conn)

    if not mismatched:
        print("✅ No mismatched entries found. Database is consistent.")
        return 0

    print(f"Found {len(mismatched)} entries with mismatched signs:\n")

    # Group by entry_type for summary
    by_type: dict[str, int] = {}
    for entry in mismatched:
        t = entry["entry_type"] or "unknown"
        by_type[t] = by_type.get(t, 0) + 1

    print("By entry_type:")
    for entry_type, count in sorted(by_type.items()):
        print(f"  {entry_type}: {count}")
    print()

    # Show sample entries
    print("Sample entries to fix:")
    for entry in mismatched[:5]:
        print(f"  {entry['date']} | {entry['merchant'][:40]:<40} | "
              f"raw={entry['raw_amount']:>10.2f} → entry={entry['entry_amount']:>10.2f}")
    if len(mismatched) > 5:
        print(f"  ... and {len(mismatched) - 5} more")
    print()

    if dry_run:
        print("🔍 DRY RUN — no changes made.")
        return len(mismatched)

    # Apply fixes
    print("Applying fixes...")

    # Update amount to match raw transaction
    conn.execute("""
        UPDATE transaction_entries
        SET amount = (
            SELECT t.amount
            FROM transactions t
            WHERE t.id = transaction_entries.transaction_id
        )
        WHERE id IN (
            SELECT te.id
            FROM transaction_entries te
            JOIN transactions t ON te.transaction_id = t.id
            WHERE (t.amount < 0 AND te.amount > 0)
               OR (t.amount > 0 AND te.amount < 0)
        )
    """)

    # Also fix amount_home to have consistent sign
    conn.execute("""
        UPDATE transaction_entries
        SET amount_home = CASE
            WHEN amount > 0 THEN ABS(amount_home)
            WHEN amount < 0 THEN -ABS(amount_home)
            ELSE amount_home
        END
        WHERE amount_home IS NOT NULL
          AND ((amount > 0 AND amount_home < 0)
            OR (amount < 0 AND amount_home > 0))
    """)

    conn.commit()

    # Verify
    remaining = get_mismatched_entries(conn)
    if remaining:
        print(f"⚠️  Warning: {len(remaining)} entries still mismatched after fix")
    else:
        print(f"✅ Fixed {len(mismatched)} entries. Database is now consistent.")

    return len(mismatched)


def main():
    parser = argparse.ArgumentParser(description="Migrate entry signs to match raw transactions")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    parser.add_argument("--db", type=Path, default=Path.home() / ".friday-bp" / "data.db",
                        help="Database path")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"❌ Database not found: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    try:
        fixed = fix_entry_signs(conn, dry_run=args.dry_run)
        return 0 if fixed >= 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    exit(main())
