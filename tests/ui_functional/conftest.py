"""
tests/ui_functional/conftest.py
================================

Local conftest for the Playwright UI functional tests.

Overrides the parent-level ``_isolated_app_dir`` autouse fixture so that
module-scoped live-server fixtures (which manage their own DB isolation) are
not disrupted by per-test DB-path patching.
"""

import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_app_dir(tmp_path: Path) -> Path:
    """
    No-op override — the live_server module fixture in this package handles
    its own DB isolation via tempfile.TemporaryDirectory.
    We yield tmp_path so callers that reference the fixture still get a path,
    but we do NOT touch os.environ or server.paths module attributes.
    """
    yield tmp_path
