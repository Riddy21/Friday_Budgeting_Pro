"""
server/daemon.py — Friday Budgeting Pro standalone daemon entry point.

Run via:
    python3 -m server.daemon

Lifecycle (in order):
  1. Ensure ~/.friday-bp/ exists with correct permissions (server.paths)
  2. Initialise the SQLite database (server.db)
  3. Attempt to initialise encryption via Keychain (server.crypto).
     In headless/CI environments the Keychain may not be available.
     Rather than crash here, we log a clear WARNING and continue booting.
     Trade-off: the daemon boots, and the server is reachable for health
     checks and setup flows — but any operation that calls encrypt() or
     decrypt() will still raise at that point (the guard lives inside those
     functions, not here).  This allows #38's refuse-to-start guard to stay
     at the correct boundary (encrypt/decrypt) while letting daemon startup
     succeed in test/CI environments without a real Keychain.
  4. Start the FastAPI UI app on 127.0.0.1:6789 (overridable via FRIDAY_BP_UI_PORT)
     using uvicorn.
  5. Start the asyncio Scheduler as a background task.
  6. Handle SIGTERM/SIGINT for clean shutdown.

launchd plist installation is OUT OF SCOPE for this module — it lives in
issue #59 (ClawHub installer).  This module is what #59 will hook into.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import uvicorn

import server.crypto as _crypto
import server.db as _db
import server.paths as _paths
from server.scheduler import Scheduler
from ui.server import app  # noqa: F401 — imported so callers can reference it

__all__ = ["main"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"  # localhost-only — Design Constraint #6 (never bind all-interfaces)
_DEFAULT_PORT = 6789


def _get_port() -> int:
    """Return the UI port, honouring the FRIDAY_BP_UI_PORT env override."""
    raw = os.environ.get("FRIDAY_BP_UI_PORT")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            log.warning(
                "FRIDAY_BP_UI_PORT=%r is not a valid integer; using default %d.",
                raw,
                _DEFAULT_PORT,
            )
    return _DEFAULT_PORT


async def _run() -> None:
    """Async main — starts uvicorn server and scheduler together."""
    port = _get_port()

    config = uvicorn.Config(
        app=app,
        host=_DEFAULT_HOST,
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    # Scheduler runs alongside uvicorn as a background asyncio task.
    scheduler = Scheduler()

    loop = asyncio.get_running_loop()

    # --- Shutdown handler ---------------------------------------------------
    shutdown_event = asyncio.Event()

    def _request_shutdown(signum: int, _frame: object) -> None:  # noqa: ANN001
        sig_name = signal.Signals(signum).name
        log.info("Received %s — initiating clean shutdown.", sig_name)
        shutdown_event.set()
        # Ask uvicorn to exit after it finishes in-flight requests.
        server.should_exit = True
        scheduler.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown, sig, None)

    # -----------------------------------------------------------------------

    log.info(
        "Friday Budgeting Pro daemon starting on http://%s:%d",
        _DEFAULT_HOST,
        port,
    )

    await scheduler.run()
    await server.serve()

    log.info("Daemon exited cleanly.")


def main() -> None:
    """Entry point called by ``python3 -m server.daemon``."""
    # 0. Load .env from project root (no-op if file doesn't exist)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")

    # 1. Filesystem setup
    _paths.ensure_app_dir()
    _paths.audit_permissions()

    # 2. Database initialisation
    _db.init_db(_paths.DB_PATH)

    # 3. Crypto initialisation (graceful fallback for headless/CI environments).
    #    See module docstring for the trade-off rationale.
    try:
        _crypto.init_crypto()
        log.info("Crypto initialised — Keychain is available.")
    except RuntimeError as exc:
        # In a fully deployed production environment this would be a hard
        # error (the user should fix their Keychain and restart).  We log at
        # WARNING so that CI pipelines and headless test runners can boot the
        # daemon without a Keychain configured.  Any attempt to encrypt or
        # decrypt actual tokens will still raise at that boundary (#38).
        log.warning(
            "Keychain not available — crypto is NOT initialised.  "
            "Token encryption/decryption will fail until the Keychain is "
            "configured and the daemon is restarted.  Detail: %s",
            exc,
        )

    # 4-6. Start uvicorn + scheduler (asyncio loop)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
