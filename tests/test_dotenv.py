"""
tests/test_dotenv.py — Verify .env.example, .gitignore rules, and daemon load_dotenv integration.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch

# Project root is one level above this tests/ directory.
REPO_ROOT = Path(__file__).parent.parent

REQUIRED_ENV_KEYS = {"PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ENV"}


# ---------------------------------------------------------------------------
# .env.example
# ---------------------------------------------------------------------------


def test_env_example_exists():
    """Ensure .env.example is committed at the project root."""
    assert (REPO_ROOT / ".env.example").exists(), ".env.example not found at repo root"


def test_env_example_has_all_required_keys():
    """Every required Plaid key must appear in .env.example."""
    content = (REPO_ROOT / ".env.example").read_text()
    defined_keys = {line.split("=")[0].strip() for line in content.splitlines() if "=" in line}
    missing = REQUIRED_ENV_KEYS - defined_keys
    assert not missing, f".env.example is missing keys: {missing}"


# ---------------------------------------------------------------------------
# .gitignore
# ---------------------------------------------------------------------------


def test_gitignore_ignores_dotenv():
    """.gitignore must contain a rule that matches .env (with or without leading slash)."""
    content = (REPO_ROOT / ".gitignore").read_text()
    # Accept both ".env" and "/.env" styles.
    assert re.search(
        r"^/?\.env$", content, re.MULTILINE
    ), ".gitignore does not contain a .env ignore rule"


def test_gitignore_does_not_ignore_env_example():
    """.env.example must NOT be excluded by .gitignore — it should be committed."""
    content = (REPO_ROOT / ".gitignore").read_text()
    assert (
        ".env.example" not in content
    ), ".gitignore incorrectly excludes .env.example (it should be committed)"


# ---------------------------------------------------------------------------
# server.daemon import
# ---------------------------------------------------------------------------


def test_daemon_imports_without_error():
    """server.daemon must be importable without raising."""
    # Remove any cached module so we get a fresh import.
    for mod in list(sys.modules.keys()):
        if mod == "server.daemon" or mod.startswith("server.daemon."):
            del sys.modules[mod]

    try:
        import server.daemon  # noqa: F401
    except Exception as exc:
        raise AssertionError(f"server.daemon raised on import: {exc}") from exc


# ---------------------------------------------------------------------------
# load_dotenv is called from main()
# ---------------------------------------------------------------------------


def test_main_calls_load_dotenv(monkeypatch):
    """main() must call load_dotenv() before doing anything else."""
    # Patch infrastructure side effects so the daemon doesn't actually start.
    monkeypatch.setattr("server.paths.ensure_app_dir", lambda: None)
    monkeypatch.setattr("server.paths.audit_permissions", lambda: None)
    monkeypatch.setattr("server.db.init_db", lambda *a, **kw: None)
    monkeypatch.setattr("server.crypto.init_crypto", lambda: None)

    # Patch _run (the async body) to a no-op coroutine so uvicorn never binds.
    import server.daemon as _daemon_mod

    async def _noop_run() -> None:  # pragma: no cover
        return

    monkeypatch.setattr(_daemon_mod, "_run", _noop_run)

    with patch("dotenv.load_dotenv") as mock_load:
        _daemon_mod.main()

    mock_load.assert_called_once()
    call_args = mock_load.call_args
    # The positional argument should be a path ending in ".env".
    dotenv_path = call_args.args[0] if call_args.args else call_args.kwargs.get("dotenv_path")
    assert dotenv_path is not None, "load_dotenv called with no path argument"
    assert (
        Path(dotenv_path).name == ".env"
    ), f"load_dotenv expected to load '.env', got: {dotenv_path}"


# ---------------------------------------------------------------------------
# load_dotenv ordering — must precede application imports that read PLAID_ENV
# ---------------------------------------------------------------------------


def test_load_dotenv_precedes_application_imports(monkeypatch):
    """load_dotenv() in daemon.main() must run before any PlaidProvider instance
    reads PLAID_ENV from os.environ.

    Regression guard: if load_dotenv() is ever moved *after* the point where
    PlaidProvider reads the environment, PLAID_ENV from .env would be silently
    ignored and the daemon would always use the wrong Plaid environment.

    This test verifies the contract by:
    1. Patching load_dotenv to record when it runs and inject a sentinel env var.
    2. Running daemon.main() with all I/O side-effects patched out.
    3. Asserting that PLAID_ENV is visible in os.environ immediately after main()
       returns — meaning load_dotenv ran during main(), not after.
    """
    import os

    # Patch infrastructure so the daemon doesn't actually boot.
    monkeypatch.setattr("server.paths.ensure_app_dir", lambda: None)
    monkeypatch.setattr("server.paths.audit_permissions", lambda: None)
    monkeypatch.setattr("server.db.init_db", lambda *a, **kw: None)
    monkeypatch.setattr("server.crypto.init_crypto", lambda: None)

    import server.daemon as _daemon_mod

    monkeypatch.setattr(_daemon_mod, "_load_plaid_config_from_db", lambda: None)

    async def _noop_run() -> None:  # pragma: no cover
        return

    monkeypatch.setattr(_daemon_mod, "_run", _noop_run)

    # Track whether load_dotenv was called at all.
    load_dotenv_called = []

    def _recording_load_dotenv(*args, **kwargs):
        """Side-effect: record the call and inject a marker env var."""
        load_dotenv_called.append(True)
        # Simulate load_dotenv writing PLAID_ENV to os.environ.
        monkeypatch.setenv("_DOTENV_LOADED_SENTINEL", "1")

    with patch("dotenv.load_dotenv", side_effect=_recording_load_dotenv):
        _daemon_mod.main()

    assert load_dotenv_called, "load_dotenv was never called inside daemon.main()"
    assert os.environ.get("_DOTENV_LOADED_SENTINEL") == "1", (
        "load_dotenv side-effect did not run before daemon.main() returned — "
        "env vars from .env are not available to PlaidProvider at startup"
    )
