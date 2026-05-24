"""
tests/test_ledgers_page.py — Tests for the /ledgers editor (issue #48).

Uses fastapi.testclient.TestClient with a tmp_path DB so tests are fully
isolated.  server.paths.DB_PATH is monkeypatched so all route helpers use
the temp database.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch) -> Path:
    """Initialise a fresh SQLite DB in tmp_path and monkeypatch DB_PATH."""
    from server.db import init_db
    import server.paths as paths

    db = tmp_path / "test.db"
    init_db(db)

    monkeypatch.setattr(paths, "DB_PATH", db)
    monkeypatch.setattr(paths, "APP_DIR", tmp_path)
    return db


@pytest.fixture()
def seeded_db(db_path: Path):
    """Seed: one TestLedger with two line items.

    Returns (db_path, ledger_id, item1_id, item2_id).
    """
    from server.db import get_db

    ledger_id = str(uuid.uuid4())
    item1_id = str(uuid.uuid4())
    item2_id = str(uuid.uuid4())

    conn = get_db(db_path)
    try:
        conn.execute(
            "INSERT INTO ledgers (id, name) VALUES (?, ?)",
            (ledger_id, "TestLedger"),
        )
        conn.execute(
            "INSERT INTO line_items (id, ledger_id, name, item_type) VALUES (?, ?, ?, ?)",
            (item1_id, ledger_id, "Groceries", "expense"),
        )
        conn.execute(
            "INSERT INTO line_items (id, ledger_id, name, item_type) VALUES (?, ?, ?, ?)",
            (item2_id, ledger_id, "Salary", "income"),
        )
        conn.commit()
    finally:
        conn.close()

    return db_path, ledger_id, item1_id, item2_id


@pytest.fixture()
def client(seeded_db):
    """TestClient (unauthenticated) backed by the seeded DB."""
    from ui.server import app

    return TestClient(app, follow_redirects=False)


@pytest.fixture()
def authed_client(seeded_db):
    """TestClient with a valid session cookie.

    Drives the setup wizard to completion (sets password) then logs in.
    """
    from ui.server import app

    c = TestClient(app, follow_redirects=False)
    _complete_setup(c)
    _login(c)
    return c, seeded_db


# ---------------------------------------------------------------------------
# Helpers (mirrors test_ui_routes.py)
# ---------------------------------------------------------------------------


def _complete_setup(client: TestClient, password: str = "testpassword123") -> None:
    r = client.post("/setup/1", data={"password": password, "password_confirm": password})
    assert r.status_code == 200, f"Setup step 1 failed: {r.status_code}"
    r = client.post("/setup/2", data={"notification_pref": "openclaw"})
    assert r.status_code == 200, f"Setup step 2 failed: {r.status_code}"
    r = client.post("/setup/3", data={"ledger_name": "Personal"})
    assert r.status_code == 200, f"Setup step 3 failed: {r.status_code}"
    r = client.post("/setup/4", data={})
    assert r.status_code == 302, f"Setup step 4 failed: {r.status_code}"


def _login(client: TestClient, password: str = "testpassword123") -> TestClient:
    r = client.post("/login", data={"password": password})
    assert r.status_code == 302, f"Login failed: {r.status_code}"
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLedgersAuth:
    """Authentication guard tests."""

    def test_get_without_session_redirects_to_login(self, client):
        r = client.get("/ledgers")
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_post_without_session_redirects_to_login(self, client):
        r = client.post("/ledgers", data={"action": "add_ledger", "name": "Test"})
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


class TestLedgersGet:
    """GET /ledgers shows the ledger tree."""

    def test_shows_seeded_ledger(self, authed_client):
        c, _ = authed_client
        r = c.get("/ledgers")
        assert r.status_code == 200
        assert "TestLedger" in r.text

    def test_shows_line_items(self, authed_client):
        c, _ = authed_client
        r = c.get("/ledgers")
        assert r.status_code == 200
        assert "Groceries" in r.text
        assert "Salary" in r.text

    def test_shows_item_types(self, authed_client):
        c, _ = authed_client
        r = c.get("/ledgers")
        assert "expense" in r.text
        assert "income" in r.text


class TestAddLineItem:
    """POST /ledgers action=add_line_item."""

    def test_adds_new_line_item(self, authed_client):
        c, (db_path, ledger_id, _, _) = authed_client
        r = c.post(
            "/ledgers",
            data={
                "action": "add_line_item",
                "ledger_id": ledger_id,
                "name": "Rent",
                "item_type": "expense",
            },
        )
        assert r.status_code == 302
        assert "/ledgers" in r.headers["location"]

        from server.db import get_db

        conn = get_db(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM line_items WHERE ledger_id = ? AND name = ?",
                (ledger_id, "Rent"),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["item_type"] == "expense"

    def test_adds_income_line_item(self, authed_client):
        c, (db_path, ledger_id, _, _) = authed_client
        r = c.post(
            "/ledgers",
            data={
                "action": "add_line_item",
                "ledger_id": ledger_id,
                "name": "Freelance",
                "item_type": "income",
            },
        )
        assert r.status_code == 302

        from server.db import get_db

        conn = get_db(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM line_items WHERE ledger_id = ? AND name = ?",
                (ledger_id, "Freelance"),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["item_type"] == "income"


class TestAddLedger:
    """POST /ledgers action=add_ledger."""

    def test_creates_new_ledger(self, authed_client):
        c, (db_path, _, _, _) = authed_client
        r = c.post("/ledgers", data={"action": "add_ledger", "name": "Business"})
        assert r.status_code == 302
        assert "/ledgers" in r.headers["location"]

        from server.db import get_db

        conn = get_db(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM ledgers WHERE name = ?", ("Business",)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None

    def test_new_ledger_visible_on_get(self, authed_client):
        c, _ = authed_client
        c.post("/ledgers", data={"action": "add_ledger", "name": "Travel"})
        r = c.get("/ledgers")
        assert r.status_code == 200
        assert "Travel" in r.text


class TestDeleteLineItem:
    """POST /ledgers action=delete_line_item."""

    def test_removes_line_item(self, authed_client):
        c, (db_path, _, item1_id, _) = authed_client
        r = c.post(
            "/ledgers",
            data={"action": "delete_line_item", "line_item_id": item1_id},
        )
        assert r.status_code == 302

        from server.db import get_db

        conn = get_db(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM line_items WHERE id = ?", (item1_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row is None

    def test_other_items_unaffected(self, authed_client):
        c, (db_path, _, item1_id, item2_id) = authed_client
        c.post("/ledgers", data={"action": "delete_line_item", "line_item_id": item1_id})

        from server.db import get_db

        conn = get_db(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM line_items WHERE id = ?", (item2_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None


class TestDeleteLedger:
    """POST /ledgers action=delete_ledger."""

    def test_removes_ledger(self, authed_client):
        c, (db_path, ledger_id, _, _) = authed_client
        r = c.post(
            "/ledgers",
            data={"action": "delete_ledger", "ledger_id": ledger_id},
        )
        assert r.status_code == 302

        from server.db import get_db

        conn = get_db(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM ledgers WHERE id = ?", (ledger_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row is None

    def test_cascade_deletes_line_items(self, authed_client):
        c, (db_path, ledger_id, item1_id, item2_id) = authed_client
        c.post("/ledgers", data={"action": "delete_ledger", "ledger_id": ledger_id})

        from server.db import get_db

        conn = get_db(db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM line_items WHERE ledger_id = ?", (ledger_id,)
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 0

    def test_deleted_ledger_not_shown(self, authed_client):
        c, (_, ledger_id, _, _) = authed_client
        c.post("/ledgers", data={"action": "delete_ledger", "ledger_id": ledger_id})
        r = c.get("/ledgers")
        assert r.status_code == 200
        # Seeded "TestLedger" ledger should be gone
        assert "TestLedger" not in r.text
