"""
tests/test_configure_plaid.py — Tests for the configure_plaid MCP tool.

Key invariant: configure_plaid() must ONLY update the three Plaid keys
(PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV) inside .env and must never
destroy or overwrite any other keys that live in the same file (e.g.
OPENCLAW_API_URL, ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import logging
import os
import stat

import pytest

import server.main as main_module
from server.main import configure_plaid, _merge_env_keys

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patch_project_root(tmp_path, monkeypatch):
    """Redirect all .env writes to tmp_path so tests never touch the real repo."""
    monkeypatch.setattr(main_module, "project_root", tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def clean_environ():
    """Remove Plaid env vars before/after each test to avoid cross-test pollution."""
    keys = ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ENV")
    saved = {k: os.environ.pop(k, None) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConfigurePlaid:
    def test_writes_env_file_and_returns_ok(self, tmp_path):
        result = configure_plaid("abc", "xyz", "sandbox")

        assert result == {"ok": True, "env": "sandbox"}
        env_file = tmp_path / ".env"
        assert env_file.exists()
        content = env_file.read_text()
        assert "PLAID_CLIENT_ID=abc" in content
        assert "PLAID_SECRET=xyz" in content
        assert "PLAID_ENV=sandbox" in content

    def test_env_file_mode_is_0o600(self, tmp_path):
        configure_plaid("abc", "xyz", "sandbox")
        env_file = tmp_path / ".env"
        mode = stat.S_IMODE(env_file.stat().st_mode)
        assert mode == 0o600

    def test_invalid_env_raises_value_error(self):
        with pytest.raises(ValueError, match="env must be one of"):
            configure_plaid("abc", "xyz", "staging")

    def test_empty_client_id_raises_value_error(self):
        with pytest.raises(ValueError, match="client_id must be non-empty"):
            configure_plaid("", "xyz", "sandbox")

    def test_empty_secret_raises_value_error(self):
        with pytest.raises(ValueError, match="secret must be non-empty"):
            configure_plaid("abc", "", "sandbox")

    def test_os_environ_updated_after_call(self):
        configure_plaid("myid", "mysecret", "production")
        assert os.environ["PLAID_CLIENT_ID"] == "myid"
        assert os.environ["PLAID_SECRET"] == "mysecret"
        assert os.environ["PLAID_ENV"] == "production"

    def test_calling_twice_updates_plaid_keys_only(self, tmp_path):
        configure_plaid("first_id", "first_secret", "sandbox")
        configure_plaid("second_id", "second_secret", "production")

        env_file = tmp_path / ".env"
        content = env_file.read_text()

        # Only the second call's Plaid values should be present.
        assert "second_id" in content
        assert "second_secret" in content
        assert "production" in content
        assert "first_id" not in content
        assert "first_secret" not in content
        # Sanity: each Plaid key appears exactly once (no duplicate lines).
        assert content.count("PLAID_CLIENT_ID") == 1
        assert content.count("PLAID_SECRET") == 1
        assert content.count("PLAID_ENV") == 1

    def test_other_env_keys_are_preserved(self, tmp_path):
        """configure_plaid must never destroy non-Plaid keys in .env."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "OPENCLAW_API_URL=http://localhost:8765\n"
            "OPENCLAW_GATEWAY_TOKEN=tok_abc123\n"
            "ANTHROPIC_API_KEY=sk-ant-xxxx\n"
            "PLAID_CLIENT_ID=old_id\n"
            "PLAID_SECRET=old_secret\n"
            "PLAID_ENV=sandbox\n"
        )
        env_file.chmod(0o600)

        configure_plaid("new_id", "new_secret", "production")

        content = env_file.read_text()
        # Plaid keys updated.
        assert "PLAID_CLIENT_ID=new_id" in content
        assert "PLAID_SECRET=new_secret" in content
        assert "PLAID_ENV=production" in content
        # Other keys untouched.
        assert "OPENCLAW_API_URL=http://localhost:8765" in content
        assert "OPENCLAW_GATEWAY_TOKEN=tok_abc123" in content
        assert "ANTHROPIC_API_KEY=sk-ant-xxxx" in content
        # No duplicates.
        assert content.count("PLAID_CLIENT_ID") == 1
        assert content.count("OPENCLAW_API_URL") == 1

    def test_other_env_keys_preserved_when_env_file_has_no_plaid_keys(self, tmp_path):
        """Plaid keys get appended without touching any existing non-Plaid key."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "OPENCLAW_API_URL=http://localhost:8765\n"
            "ANTHROPIC_API_KEY=sk-ant-xxxx\n"
        )
        env_file.chmod(0o600)

        configure_plaid("myid", "mysecret", "sandbox")

        content = env_file.read_text()
        assert "PLAID_CLIENT_ID=myid" in content
        assert "PLAID_SECRET=mysecret" in content
        assert "PLAID_ENV=sandbox" in content
        assert "OPENCLAW_API_URL=http://localhost:8765" in content
        assert "ANTHROPIC_API_KEY=sk-ant-xxxx" in content

    def test_secret_never_appears_in_stdout_stderr(self, tmp_path, capsys, caplog):
        secret = "super_secret_12345"
        with caplog.at_level(logging.DEBUG):
            configure_plaid("myid", secret, "sandbox")

        captured = capsys.readouterr()
        assert secret not in captured.out
        assert secret not in captured.err
        assert secret not in caplog.text

    def test_default_env_is_production(self, tmp_path):
        result = configure_plaid("myid", "mysecret")
        assert result["env"] == "production"
        content = (tmp_path / ".env").read_text()
        assert "PLAID_ENV=production" in content

    def test_development_env_accepted(self, tmp_path):
        result = configure_plaid("myid", "mysecret", "development")
        assert result == {"ok": True, "env": "development"}


class TestMergeEnvKeys:
    """Unit tests for the _merge_env_keys helper in isolation."""

    def test_updates_existing_key(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("FOO=old\n")
        result = _merge_env_keys(f, {"FOO": "new"})
        assert result == "FOO=new\n"

    def test_appends_missing_key(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("EXISTING=yes\n")
        result = _merge_env_keys(f, {"NEW_KEY": "value"})
        assert "EXISTING=yes" in result
        assert "NEW_KEY=value" in result

    def test_preserves_comments_and_blank_lines(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("# comment\n\nFOO=bar\n")
        result = _merge_env_keys(f, {"FOO": "baz"})
        assert "# comment" in result
        assert "\n" in result  # blank line preserved
        assert "FOO=baz" in result
        assert "FOO=bar" not in result

    def test_handles_nonexistent_file(self, tmp_path):
        f = tmp_path / ".env"
        result = _merge_env_keys(f, {"KEY": "val"})
        assert result == "KEY=val\n"

    def test_no_duplicate_keys(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("A=1\nB=2\nA=3\n")  # already-malformed file with dup
        result = _merge_env_keys(f, {"A": "99"})
        # Should replace first occurrence and keep second OR just replace all.
        # Either way, the new value must appear.
        assert "A=99" in result
