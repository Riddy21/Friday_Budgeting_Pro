"""
server/flinks_credentials.py — Per-user Flinks credential resolution.

Loads Flinks API credentials for a given user from the ``flinks_config`` table,
falling back to environment variables when the DB has no entry.

Mirrors server.plaid_credentials / server.rbc_credentials so it can be used by
both server.main and server.health_monitor without circular imports.
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_INSTANCE = "toolbox"


def get_flinks_credentials(
    user_id: str | None,
    db_path: str | Path | None = None,
) -> tuple[str | None, str | None, str | None, str]:
    """Load Flinks credentials for *user_id* from the DB, falling back to env vars.

    Priority order:
      1. ``flinks_config`` table row for the given ``user_id``
      2. ``FLINKS_CUSTOMER_ID`` / ``FLINKS_SECRET_KEY`` / ``FLINKS_X_API_KEY`` /
         ``FLINKS_INSTANCE`` environment variables

    Parameters
    ----------
    user_id : str or None
        The user whose credentials to load.  When ``None``, only env vars are
        consulted.
    db_path : str or Path or None
        Path to the SQLite database.  Defaults to ``server.paths.DB_PATH``.

    Returns
    -------
    tuple[customer_id, secret_key, x_api_key, instance]
        ``customer_id`` / ``secret_key`` / ``x_api_key`` may be ``None`` when
        unconfigured; ``instance`` always falls back to ``'toolbox'`` (the
        public Flinks sandbox instance).
    """
    if db_path is None:
        import server.paths

        db_path = server.paths.DB_PATH

    if user_id:
        try:
            import sqlite3

            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT customer_id, secret_key, x_api_key, instance "
                    "FROM flinks_config WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if row:
                    return (
                        row["customer_id"],
                        row["secret_key"],
                        row["x_api_key"],
                        row["instance"] or _DEFAULT_INSTANCE,
                    )
            finally:
                conn.close()
        except Exception:
            pass  # fall through to env vars

    return (
        os.environ.get("FLINKS_CUSTOMER_ID"),
        os.environ.get("FLINKS_SECRET_KEY"),
        os.environ.get("FLINKS_X_API_KEY"),
        os.environ.get("FLINKS_INSTANCE", _DEFAULT_INSTANCE),
    )
