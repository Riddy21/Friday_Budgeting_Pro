"""
tests/ui/_server_runner.py — thin wrapper that starts ui.server via uvicorn
while patching server.paths.DB_PATH from the FRIDAY_BP_DB_PATH env var.

Called by the conftest server_url fixture as a subprocess so that each
Playwright test session gets a fresh, isolated SQLite database.

Usage (automated — do not call directly):
    python -m tests.ui._server_runner --port <PORT>
"""

from __future__ import annotations

import os
from pathlib import Path

# ── Patch DB path BEFORE importing ui.server ───────────────────────────────
_db_path_env = os.environ.get("FRIDAY_BP_DB_PATH")
if _db_path_env:
    import server.paths as _paths
    from server.db import init_db

    _db = Path(_db_path_env)
    _db.parent.mkdir(parents=True, exist_ok=True)
    init_db(_db)
    _paths.DB_PATH = _db
    _paths.APP_DIR = _db.parent

# ── Start uvicorn ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("FRIDAY_BP_UI_PORT", "6789"))
    uvicorn.run("ui.server:app", host="127.0.0.1", port=port, log_level="warning")
