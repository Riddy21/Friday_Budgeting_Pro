"""
tests/test_setup_tool.py — Tests for setup_status and apply_initial_setup MCP tools.
"""

from __future__ import annotations

import json
import uuid

import pytest

from server.db import get_db, init_db
from server.main import apply_initial_setup, setup_status

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Create a fresh DB at tmp_path and monkeypatch server.paths.DB_PATH.

    Also points _OPENCLAW_HOME at a non-existent subdirectory so cron
    registration is skipped (cron_registered=False) for all tests that don't
    explicitly need it.  Tests that want to verify cron registration should
    create the directory themselves.
    """
    import server.main
    import server.paths

    db_file = tmp_path / "test.db"
    monkeypatch.setattr(server.paths, "DB_PATH", db_file)
    init_db(db_file)

    # Redirect OpenClaw home to an isolated, initially-absent directory so
    # tests don't pollute the real ~/.openclaw/cron/ and run hermetically.
    monkeypatch.setattr(server.main, "_OPENCLAW_HOME", tmp_path / "dot-openclaw")

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
    assert result["line_items_created"] == 11  # 10 expense/income + 1 savings
    assert result["hints_created"] == 1  # 1 default investment savings hint always seeded
    assert result["banks_to_link"] == []

    # Verify in DB
    conn = get_db(tmp_db)
    ledgers = conn.execute("SELECT name FROM ledgers").fetchall()
    assert len(ledgers) == 1
    assert ledgers[0]["name"] == "Personal"

    line_items = conn.execute("SELECT name, item_type FROM line_items").fetchall()
    assert len(line_items) == 11

    # Check standard items are present
    item_pairs = {(r["name"], r["item_type"]) for r in line_items}
    assert ("Salary", "income") in item_pairs
    assert ("Groceries", "expense") in item_pairs
    assert ("Dining", "expense") in item_pairs
    assert ("Investments & Savings", "savings") in item_pairs
    conn.close()


def test_apply_with_extra_ledger_creates_personal_and_business(tmp_db):
    extra = [{"name": "Business", "line_items": [{"name": "Office", "type": "expense"}]}]
    result = apply_initial_setup(banks_to_link=[], extra_ledgers=extra, hints=[])

    assert result["status"] == "ok"
    assert set(result["ledgers_created"]) == {"Personal", "Business"}
    # 11 personal + 1 business
    assert result["line_items_created"] == 12

    conn = get_db(tmp_db)
    ledger_names = {r["name"] for r in conn.execute("SELECT name FROM ledgers").fetchall()}
    assert ledger_names == {"Personal", "Business"}
    conn.close()


def test_apply_with_hints_creates_classification_hints(tmp_db):
    hints = ["Amazon is usually Shopping", "Starbucks is Dining"]
    result = apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=hints)

    assert result["hints_created"] == 3  # 1 default + 2 user-supplied

    conn = get_db(tmp_db)
    rows = conn.execute("SELECT text FROM classification_hints ORDER BY text").fetchall()
    texts = [r["text"] for r in rows]
    assert "Amazon is usually Shopping" in texts
    assert "Starbucks is Dining" in texts
    assert any("TFSA" in t or "investment" in t.lower() for t in texts), "Default savings hint missing"
    conn.close()


def test_apply_banks_to_link_returned_not_stored(tmp_db):
    """banks_to_link are acknowledged but NOT stored — Plaid Link happens separately."""
    result = apply_initial_setup(banks_to_link=["TD Bank", "RBC"], extra_ledgers=[], hints=[])
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

    assert ledger_count == 2  # Personal + Business
    assert line_item_count == 12  # 11 personal + 1 business
    assert hint_count == 2  # 1 default savings hint + 1 user hint


# ---------------------------------------------------------------------------
# cron_registered key
# ---------------------------------------------------------------------------


def test_apply_cron_registered_false_when_openclaw_absent(tmp_db):
    """cron_registered should be False when ~/.openclaw/ does not exist.

    The tmp_db fixture points _OPENCLAW_HOME at a non-existent directory, so
    no cron file is written and the result includes cron_registered=False.
    """
    result = apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])
    assert result["cron_registered"] is False


def test_apply_cron_registered_false_regardless_of_openclaw_dir(tmp_db, monkeypatch, tmp_path):
    """cron_registered is always False from apply_initial_setup.

    The cron file approach is deprecated — scheduled syncs are now handled by
    the daemon's internal background loop (see daemon.py).  apply_initial_setup
    no longer calls _register_openclaw_cron_file(), so cron_registered is
    always False regardless of whether ~/.openclaw/ exists.
    """
    import server.main

    oc_dir = tmp_path / "dot-openclaw-exists"
    oc_dir.mkdir()
    monkeypatch.setattr(server.main, "_OPENCLAW_HOME", oc_dir)

    result = apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])
    assert result["cron_registered"] is False

    # The cron file should NOT be written by apply_initial_setup anymore.
    cron_file = oc_dir / "cron" / "friday-budgeting-pro-sync.json"
    assert not cron_file.exists(), (
        "apply_initial_setup should not write the cron file (deprecated approach)"
    )


def test_apply_cron_no_file_on_second_call(tmp_db, monkeypatch, tmp_path):
    """Re-running apply_initial_setup never writes a cron file (deprecated).

    Scheduled sync is now handled by the daemon's internal background loop,
    so apply_initial_setup should not write cron files regardless of how many
    times it is called.
    """
    import server.main

    oc_dir = tmp_path / "dot-openclaw-overwrite"
    oc_dir.mkdir()
    monkeypatch.setattr(server.main, "_OPENCLAW_HOME", oc_dir)

    result1 = apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])
    result2 = apply_initial_setup(banks_to_link=[], extra_ledgers=[], hints=[])

    assert result1["cron_registered"] is False
    assert result2["cron_registered"] is False

    cron_dir = oc_dir / "cron"
    assert not cron_dir.exists(), "cron/ dir should not be created by apply_initial_setup"
