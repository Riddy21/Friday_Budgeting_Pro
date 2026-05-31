"""
tests/test_savings_classifier_rule.py — Tests for savings classifier rule (#235).

Covers:
  - Default 'Investment contribution' rule has rule_type='savings' (not 'transfer')
  - Default investment savings hint is always seeded on apply_initial_setup
  - Migration updates existing 'transfer' Investment contribution rule to 'savings'
"""

from __future__ import annotations

import uuid

import pytest

import server.paths
from server.db import get_db, init_db


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "friday-bp" / "data.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server.paths, "DB_PATH", db_path)
    monkeypatch.setattr(server.paths, "APP_DIR", db_path.parent)
    init_db(db_path)
    return db_path


def test_investment_contribution_rule_is_savings_type(tmp_db, monkeypatch):
    """The default 'Investment contribution' rule is seeded with rule_type='savings'."""
    from server.main import apply_initial_setup
    from ui.auth import create_user

    monkeypatch.setenv("OPENCLAW_DIR", str(tmp_db.parent.parent))
    create_user(tmp_db, "testuser", "testpass123")
    apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])

    conn = get_db(tmp_db)
    rule = conn.execute(
        "SELECT name, rule_type, is_default FROM classification_rules WHERE name = 'Investment contribution'"
    ).fetchone()
    conn.close()

    assert rule is not None, "Investment contribution rule not found"
    assert rule["rule_type"] == "savings", f"Expected savings, got {rule['rule_type']}"
    assert rule["is_default"] == 1, "Investment contribution rule should be is_default=1"


def test_investment_contribution_rule_description_mentions_rrsp_tfsa(tmp_db, monkeypatch):
    """The investment rule description mentions RRSP, TFSA, Wealthsimple, Questrade."""
    from server.main import apply_initial_setup
    from ui.auth import create_user

    monkeypatch.setenv("OPENCLAW_DIR", str(tmp_db.parent.parent))
    create_user(tmp_db, "testuser2", "testpass123")
    apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])

    conn = get_db(tmp_db)
    rule = conn.execute(
        "SELECT description FROM classification_rules WHERE name = 'Investment contribution'"
    ).fetchone()
    conn.close()

    desc = rule["description"].lower()
    assert "wealthsimple" in desc, "Rule description should mention Wealthsimple"
    assert "questrade" in desc, "Rule description should mention Questrade"
    assert "rrsp" in desc, "Rule description should mention RRSP"
    assert "tfsa" in desc, "Rule description should mention TFSA"


def test_default_savings_hint_seeded_on_setup(tmp_db, monkeypatch):
    """apply_initial_setup always seeds the investment savings classification hint."""
    from server.main import apply_initial_setup
    from ui.auth import create_user

    monkeypatch.setenv("OPENCLAW_DIR", str(tmp_db.parent.parent))
    create_user(tmp_db, "testuser3", "testpass123")
    result = apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])

    # The default hint should always be seeded
    assert result["hints_created"] >= 1, "Expected at least 1 hint created"

    conn = get_db(tmp_db)
    hints = conn.execute("SELECT text FROM classification_hints").fetchall()
    conn.close()

    hint_texts = [h["text"].lower() for h in hints]
    assert any(
        "tfsa" in t or "rrsp" in t or "investment" in t for t in hint_texts
    ), f"Expected investment savings hint, got: {hint_texts}"


def test_migration_updates_transfer_rule_to_savings(tmp_db, monkeypatch):
    """migrate_db() updates existing 'transfer' Investment contribution rule to 'savings'."""
    # Manually insert the old-style rule (transfer type)
    conn = get_db(tmp_db)
    conn.execute(
        "INSERT INTO classification_rules (id, name, description, rule_type, priority, is_default, enabled, created_at) "
        "VALUES (?, 'Investment contribution', 'Old transfer rule', 'transfer', 20, 1, 1, unixepoch())",
        (_uid(),),
    )
    conn.commit()
    conn.close()

    # Re-run init_db (which includes migrations) — should update the rule_type
    from server.db import init_db

    init_db(tmp_db)

    conn = get_db(tmp_db)
    rule = conn.execute(
        "SELECT rule_type FROM classification_rules WHERE name = 'Investment contribution' AND is_default = 1"
    ).fetchone()
    conn.close()

    assert rule is not None
    assert rule["rule_type"] == "savings", f"Expected savings after migration, got {rule['rule_type']}"
