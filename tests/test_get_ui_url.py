"""Tests for the get_ui_url MCP tool."""
import importlib
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Import helper — ensure project root is on the path so server.main resolves.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.main import get_ui_url  # noqa: E402


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_args_returns_base_url():
    """No arguments → base URL on default port."""
    result = get_ui_url()
    assert result == {"url": "http://127.0.0.1:6789"}


def test_empty_string_page_returns_base_url():
    """Explicit empty string → same as no args, no trailing slash."""
    result = get_ui_url(page="")
    assert result == {"url": "http://127.0.0.1:6789"}


def test_known_page_ledgers():
    """page='ledgers' → URL with /ledgers path."""
    result = get_ui_url(page="ledgers")
    assert result == {"url": "http://127.0.0.1:6789/ledgers"}


def test_known_page_accounts():
    result = get_ui_url(page="accounts")
    assert result == {"url": "http://127.0.0.1:6789/accounts"}


def test_known_page_profile():
    result = get_ui_url(page="profile")
    assert result == {"url": "http://127.0.0.1:6789/profile"}


def test_known_page_dashboard():
    result = get_ui_url(page="dashboard")
    assert result == {"url": "http://127.0.0.1:6789/dashboard"}


def test_custom_port_via_env(monkeypatch):
    """FRIDAY_BP_UI_PORT env var overrides the default port."""
    monkeypatch.setenv("FRIDAY_BP_UI_PORT", "9000")
    result = get_ui_url()
    assert result == {"url": "http://127.0.0.1:9000"}


def test_custom_port_with_page(monkeypatch):
    """Custom port is used when a page is also specified."""
    monkeypatch.setenv("FRIDAY_BP_UI_PORT", "8080")
    result = get_ui_url(page="dashboard")
    assert result == {"url": "http://127.0.0.1:8080/dashboard"}


def test_invalid_page_raises_value_error():
    """Unrecognised page name → ValueError."""
    with pytest.raises(ValueError, match="unknown page"):
        get_ui_url(page="settings")


def test_invalid_page_slash_raises_value_error():
    """Leading slash is not a valid page name."""
    with pytest.raises(ValueError, match="unknown page"):
        get_ui_url(page="/ledgers")
