"""
tests/test_setup_tool.py — Tests for setup_status and apply_initial_setup MCP tools.
"""

from __future__ import annotations

import uuid
import pytest

from server.db import get_db, init_db
from server.main import setup_status, apply_initial_setup


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Create a fresh DB at tmp_path and monkeypatch server.paths.DB_PATH."""
    import server.paths

    db_file = tmp_path / "test.db"
    monkeypatch.setattr(server.paths, "DB_PATH", db_file)
    init_db(db_file)
    return db_file


# ---------------------------------------------------------------------------
# setup_status tests
# ---------------------------------------------------------------------------


def test_setup_status_empty_is_not_started(tmp_db):
    result = setup_status()
    assert result == {"status": "not_started"}


def test_setup_status_ledger_only_is_in_progress(tmp_db):
    conn = get_db(tmp_db)
    conn.execute("INSERT INTO ledgers (id, name) VALUES (?, ?)", (str(uuid.uuid4()), "Personal"))
    conn.commit()
    conn.close()

    result = setup_status()
    assert result == {"status": "in_progress"}


def test_setup_status_ledger_and_bank_is_complete(tmp_db):
    conn = get_db(tmp_db)
    conn.execute("INSERT INTO ledgers (id, name) VALUES (?, ?)", (str(uuid.uuid4()), "Personal"))
    conn.execute(
        "INSERT INTO bank_connections (id, plaid_item_id, plaid_access_token_encrypted) "
        "VALUES (?, ?, ?)",
        (str(uuid.uuid4()), "item_abc", "enc_token"),
    )
    conn.commit()
    conn.close()

    result = setup_status()
    assert result == {"status": "complete"}


# ---------------------------------------------------------------------------
# apply_initial_setup tests
# ---------------------------------------------------------------------------


def test_apply_minimal_creates_personal_ledger_with_10_items(tmp_db):
    result = apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])

    assert result["status"] == "ok"
    assert result["ledgers_created"] == ["Personal"]
    assert result["line_items_created"] == 10
    assert result["hints_created"] == 0
    assert result["banks_to_link"] == []

    # Verify in DB
    conn = get_db(tmp_db)
    ledgers = conn.execute("SELECT name FROM ledgers").fetchall()
    assert len(ledgers) == 1
    assert ledgers[0]["name"] == "Personal"

    line_items = conn.execute("SELECT name, item_type FROM line_items").fetchall()
    assert len(line_items) == 10

    # Check standard items are present
    item_pairs = {(r["name"], r["item_type"]) for r in line_items}
    assert ("Salary", "income") in item_pairs
    assert ("Groceries", "expense") in item_pairs
    assert ("Dining", "expense") in item_pairs
    conn.close()


def test_apply_with_extra_ledger_creates_personal_and_business(tmp_db):
    extra = [{"name": "Business", "line_items": [{"name": "Office", "type": "expense"}]}]
    result = apply_initial_setup(banks_to_link=[], extra_ledgers=extra, hints=[])

    assert result["status"] == "ok"
    assert set(result["ledgers_created"]) == {"Personal", "Business"}
    # 10 personal + 1 business
    assert result["line_items_created"] == 11

    conn = get_db(tmp_db)
    ledger_names = {r["name"] for r in conn.execute("SELECT name FROM ledgers").fetchall()}
    assert ledger_names == {"Personal", "Business"}
    conn.close()


def test_apply_with_hints_creates_classification_hints(tmp_db):
    hints = ["Amazon is usually Shopping", "Starbucks is Dining"]
    result = apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=hints)

    assert result["hints_created"] == 2

    conn = get_db(tmp_db)
    rows = conn.execute("SELECT text FROM classification_hints ORDER BY text").fetchall()
    texts = [r["text"] for r in rows]
    assert "Amazon is usually Shopping" in texts
    assert "Starbucks is Dining" in texts
    conn.close()


def test_apply_banks_to_link_returned_not_stored(tmp_db):
    """banks_to_link are acknowledged but NOT stored — Plaid Link happens separately."""
    result = apply_initial_setup(
        banks_to_link=["TD Bank", "RBC"], extra_ledgers=[], hints=[]
    )
    assert set(result["banks_to_link"]) == {"TD Bank", "RBC"}

    # Nothing should be written to bank_connections
    conn = get_db(tmp_db)
    count = conn.execute("SELECT COUNT(*) FROM bank_connections").fetchone()[0]
    conn.close()
    assert count == 0


def test_apply_is_idempotent(tmp_db):
    """Calling apply_initial_setup twice must not duplicate any rows."""
    hints = ["Amazon is usually Shopping"]
    extra = [{"name": "Business", "line_items": [{"name": "Office", "type": "expense"}]}]

    result1 = apply_initial_setup(banks_to_link=[], extra_ledgers=extra, hints=hints)
    result2 = apply_initial_setup(banks_to_link=[], extra_ledgers=extra, hints=hints)

    # Second call creates nothing new
    assert result2["ledgers_created"] == []
    assert result2["line_items_created"] == 0
    assert result2["hints_created"] == 0

    conn = get_db(tmp_db)
    ledger_count = conn.execute("SELECT COUNT(*) FROM ledgers").fetchone()[0]
    line_item_count = conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0]
    hint_count = conn.execute("SELECT COUNT(*) FROM classification_hints").fetchone()[0]
    conn.close()

    assert ledger_count == 2          # Personal + Business
    assert line_item_count == 11      # 10 personal + 1 business
    assert hint_count == 1
