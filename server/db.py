"""
server/db.py — Thin SQLite connection helper for Friday Budgeting Pro.

No ORM. Plain sqlite3 stdlib only.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


def get_db(path: str | Path) -> sqlite3.Connection:
    """Return a sqlite3.Connection with sane defaults.

    - row_factory = sqlite3.Row  (column access by name)
    - PRAGMA foreign_keys = ON   (enforce FK constraints)
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str | Path) -> None:
    """Initialise (or migrate) the database at *path* using db/schema.sql.

    Idempotent — the schema uses IF NOT EXISTS throughout, so calling this
    on an already-initialised database is safe.

    Migrations
    ----------
    After the base schema is applied, any ALTER TABLE migrations needed for
    columns added after the initial schema are run here.  Each migration is
    guarded so it is a no-op if the column already exists.
    """
    path = Path(path)
    schema_path = Path(__file__).parent.parent / "db" / "schema.sql"
    sql = schema_path.read_text()

    conn = get_db(path)
    try:
        conn.executescript(sql)
        conn.commit()

        # Migration: bank_accounts.description (added in #127)
        existing_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(bank_accounts)")
        }
        if "description" not in existing_cols:
            conn.execute(
                "ALTER TABLE bank_accounts ADD COLUMN description TEXT"
            )
            conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Context manager that commits on success and rolls back on any exception.

    Usage::

        with transaction(conn):
            conn.execute("INSERT INTO ...")
    """
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
