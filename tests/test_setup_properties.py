"""
tests/test_setup_properties.py — Tests for rental property and investment
account setup flows added in #176.

Covers:
  MCP layer (apply_initial_setup):
    - rental_properties creates ledger + links account
    - investment_account_ids creates investment ledger + links all accounts
    - both at once: properties first, then investments
    - empty lists are no-ops for the new params
    - missing account_id in property dict skips set_account_ledger
  UI wizard:
    - POST /setup/4 renders step 5 (investment step)
    - POST /setup/4 with properties populates wizard state
    - POST /setup/4 skip → investment step with empty rental_properties
    - POST /setup/5 renders step 6 (done)
    - POST /setup/5 skip → step 6
    - POST /setup/6 calls apply_initial_setup with stored properties/investments
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import server.main
import server.paths
from server.db import get_db, init_db

# ---------------------------------------------------------------------------
# Fixtures — MCP layer
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Fresh DB with an active user; patches DB_PATH and cron home."""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(server.paths, "DB_PATH", db_file)
    init_db(db_file)

    # Disable cron registration for all tests.
    monkeypatch.setattr(server.main, "_OPENCLAW_HOME", tmp_path / "dot-openclaw-absent")

    # Insert a user so get_active_user_id returns something.
    from ui.auth import create_user

    create_user(db_file, "testuser", "securepass1")

    return db_file


def _make_bank_account(
    db_file, user_id: str | None = None, acct_type: str = "checking", institution: str = "TD Bank"
) -> str:
    """Insert a minimal bank_connection + bank_account; return account id."""
    conn = get_db(db_file)
    conn_id = str(uuid.uuid4())
    acct_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO bank_connections "
        "(id, plaid_item_id, plaid_access_token_encrypted, user_id, institution_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (conn_id, f"item_{conn_id}", "enc_tok", user_id, institution),
    )
    conn.execute(
        "INSERT INTO bank_accounts (id, connection_id, plaid_account_id, name, type) "
        "VALUES (?, ?, ?, ?, ?)",
        (acct_id, conn_id, f"plaid_{acct_id}", "Chequing", acct_type),
    )
    conn.commit()
    conn.close()
    return acct_id


def _get_user_id(db_file) -> str:
    conn = get_db(db_file)
    row = conn.execute("SELECT id FROM users LIMIT 1").fetchone()
    conn.close()
    return row["id"]


# ---------------------------------------------------------------------------
# MCP tests — apply_initial_setup with rental_properties
# ---------------------------------------------------------------------------


class TestApplyInitialSetupRentalProperties:
    def test_creates_property_ledger(self, tmp_db):
        uid = _get_user_id(tmp_db)
        acct_id = _make_bank_account(tmp_db, uid)

        result = server.main.apply_initial_setup(
            rental_properties=[
                {"name": "123 Main St", "description": "2-bed condo", "account_id": acct_id}
            ],
            investment_account_ids=[],
        )

        assert result["status"] == "ok"
        assert result["properties_created"] == 1
        assert "123 Main St" in result["ledgers_created"]

        conn = get_db(tmp_db)
        ledger = conn.execute("SELECT id, type FROM ledgers WHERE name = '123 Main St'").fetchone()
        conn.close()
        assert ledger is not None
        assert ledger["type"] == "property"

    def test_links_account_to_property_ledger(self, tmp_db):
        uid = _get_user_id(tmp_db)
        acct_id = _make_bank_account(tmp_db, uid)

        server.main.apply_initial_setup(
            rental_properties=[{"name": "Oak Ave", "account_id": acct_id}],
        )

        conn = get_db(tmp_db)
        row = conn.execute(
            "SELECT ba.default_ledger_id, l.name "
            "FROM bank_accounts ba JOIN ledgers l ON l.id = ba.default_ledger_id "
            "WHERE ba.id = ?",
            (acct_id,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["name"] == "Oak Ave"

    def test_no_account_id_skips_link(self, tmp_db):
        result = server.main.apply_initial_setup(
            rental_properties=[{"name": "No Account Prop", "description": "desc"}],
        )
        assert result["status"] == "ok"
        assert result["properties_created"] == 1

    def test_empty_name_skipped(self, tmp_db):
        result = server.main.apply_initial_setup(
            rental_properties=[{"name": "", "account_id": None}],
        )
        assert result["properties_created"] == 0

    def test_empty_list_is_noop(self, tmp_db):
        result = server.main.apply_initial_setup(rental_properties=[])
        assert result["properties_created"] == 0
        assert result["investment_ledger_id"] is None


# ---------------------------------------------------------------------------
# MCP tests — apply_initial_setup with investment_account_ids
# ---------------------------------------------------------------------------


class TestApplyInitialSetupInvestments:
    def test_creates_investment_ledger_and_links_accounts(self, tmp_db):
        uid = _get_user_id(tmp_db)
        acct1 = _make_bank_account(tmp_db, uid, institution="Wealthsimple")
        acct2 = _make_bank_account(tmp_db, uid, institution="Questrade")

        result = server.main.apply_initial_setup(
            investment_account_ids=[acct1, acct2],
        )

        assert result["status"] == "ok"
        assert result["investment_ledger_id"] is not None
        assert "Investments" in result["ledgers_created"]

        conn = get_db(tmp_db)
        # Both accounts should point to the same investment ledger.
        for acct_id in (acct1, acct2):
            row = conn.execute(
                "SELECT default_ledger_id FROM bank_accounts WHERE id = ?", (acct_id,)
            ).fetchone()
            assert row["default_ledger_id"] == result["investment_ledger_id"]
        conn.close()

    def test_empty_list_skips_investment_ledger(self, tmp_db):
        result = server.main.apply_initial_setup(investment_account_ids=[])
        assert result["investment_ledger_id"] is None

        conn = get_db(tmp_db)
        inv = conn.execute("SELECT id FROM ledgers WHERE name = 'Investments'").fetchone()
        conn.close()
        assert inv is None

    def test_none_is_treated_as_empty(self, tmp_db):
        result = server.main.apply_initial_setup(investment_account_ids=None)
        assert result["investment_ledger_id"] is None


# ---------------------------------------------------------------------------
# MCP tests — both at once
# ---------------------------------------------------------------------------


class TestApplyInitialSetupCombined:
    def test_properties_and_investments_together(self, tmp_db):
        uid = _get_user_id(tmp_db)
        prop_acct = _make_bank_account(tmp_db, uid, institution="RBC")
        inv_acct = _make_bank_account(tmp_db, uid, institution="Wealthsimple")

        result = server.main.apply_initial_setup(
            rental_properties=[{"name": "Elm St", "account_id": prop_acct}],
            investment_account_ids=[inv_acct],
        )

        assert result["status"] == "ok"
        assert result["properties_created"] == 1
        assert result["investment_ledger_id"] is not None
        assert "Elm St" in result["ledgers_created"]
        assert "Investments" in result["ledgers_created"]

        conn = get_db(tmp_db)
        # Property account → Elm St ledger
        elm_ledger = conn.execute("SELECT id FROM ledgers WHERE name = 'Elm St'").fetchone()
        assert elm_ledger is not None
        prop_link = conn.execute(
            "SELECT default_ledger_id FROM bank_accounts WHERE id = ?", (prop_acct,)
        ).fetchone()
        assert prop_link["default_ledger_id"] == elm_ledger["id"]

        # Investment account → Investments ledger
        inv_link = conn.execute(
            "SELECT default_ledger_id FROM bank_accounts WHERE id = ?", (inv_acct,)
        ).fetchone()
        assert inv_link["default_ledger_id"] == result["investment_ledger_id"]
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures — UI layer
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch) -> Path:
    import server.paths as paths

    db = tmp_path / "ui_setup_test.db"
    init_db(db)
    monkeypatch.setattr(paths, "DB_PATH", db)
    monkeypatch.setattr(paths, "APP_DIR", tmp_path)
    return db


@pytest.fixture()
def client(db_path: Path) -> TestClient:
    from ui.server import app

    return TestClient(app, follow_redirects=False)


def _wizard_through_step3(client: TestClient) -> None:
    """Drive through steps 1→3 (bank skip)."""
    client.post("/setup/1", data={"password": "securepass1", "password_confirm": "securepass1"})
    client.post("/setup/2", data={"notification_channel": "openclaw_chat"})
    client.post("/setup/3", data={"action": "skip"})


# ---------------------------------------------------------------------------
# UI tests — step 4 (properties)
# ---------------------------------------------------------------------------


class TestSetupWizardStep4Properties:
    def test_step4_renders_after_step3(self, client):
        _wizard_through_step3(client)
        # The last call already returned step 4 HTML; call again from step 3.
        r = client.post("/setup/3", data={"action": "skip"})
        assert r.status_code == 200
        assert b"Rental properties" in r.content or b"rental" in r.content.lower()

    def test_step4_skip_goes_to_step5(self, client):
        _wizard_through_step3(client)
        r = client.post("/setup/4", data={"action": "skip"})
        assert r.status_code == 200
        assert b"Investment" in r.content

    def test_step4_continue_without_checkbox_goes_to_step5(self, client):
        _wizard_through_step3(client)
        r = client.post("/setup/4", data={"action": "continue"})
        assert r.status_code == 200
        assert b"Investment" in r.content

    def test_step4_with_properties_stores_in_wizard_state(self, client):
        _wizard_through_step3(client)
        r = client.post(
            "/setup/4",
            data={
                "action": "continue",
                "has_properties": "yes",
                "property_name[]": "Oak Ave",
                "property_description[]": "Cottage",
                "property_account_id[]": "",
            },
        )
        assert r.status_code == 200
        # Step 5 page rendered
        assert b"Investment" in r.content


# ---------------------------------------------------------------------------
# UI tests — step 5 (investments)
# ---------------------------------------------------------------------------


class TestSetupWizardStep5Investments:
    def _through_step4(self, client):
        _wizard_through_step3(client)
        client.post("/setup/4", data={"action": "skip"})

    def test_step5_skip_goes_to_step6(self, client):
        self._through_step4(client)
        r = client.post("/setup/5", data={"action": "skip"})
        assert r.status_code == 200
        assert b"all set" in r.content.lower() or b"Done" in r.content or b"Dashboard" in r.content

    def test_step5_continue_with_accounts_goes_to_step6(self, client):
        self._through_step4(client)
        r = client.post(
            "/setup/5",
            data={"action": "continue", "investment_account_id": "fake-uuid-123"},
        )
        assert r.status_code == 200
        # Step 6 rendered
        assert b"set" in r.content.lower() or b"Dashboard" in r.content


# ---------------------------------------------------------------------------
# UI tests — step 6 (final — calls apply_initial_setup with stored data)
# ---------------------------------------------------------------------------


class TestSetupWizardStep6Final:
    def _through_step5(self, client):
        _wizard_through_step3(client)
        client.post("/setup/4", data={"action": "skip"})
        client.post("/setup/5", data={"action": "skip"})

    def test_step6_calls_apply_initial_setup(self, client):
        self._through_step5(client)
        with patch("server.main.apply_initial_setup") as mock_setup:
            mock_setup.return_value = {
                "status": "ok",
                "ledgers_created": [],
                "line_items_created": 0,
                "hints_created": 0,
                "banks_to_link": [],
                "properties_created": 0,
                "investment_ledger_id": None,
                "cron_registered": False,
            }
            r = client.post("/setup/6", data={})
        assert r.status_code == 302
        mock_setup.assert_called_once()

    def test_step6_passes_properties_to_apply(self, client):
        """If properties were entered at step 4, step 6 passes them through."""
        _wizard_through_step3(client)
        client.post(
            "/setup/4",
            data={
                "action": "continue",
                "has_properties": "yes",
                "property_name[]": "Maple St",
                "property_description[]": "",
                "property_account_id[]": "",
            },
        )
        client.post("/setup/5", data={"action": "skip"})

        with patch("server.main.apply_initial_setup") as mock_setup:
            mock_setup.return_value = {
                "status": "ok",
                "ledgers_created": ["Personal", "Maple St"],
                "line_items_created": 10,
                "hints_created": 0,
                "banks_to_link": [],
                "properties_created": 1,
                "investment_ledger_id": None,
                "cron_registered": False,
            }
            client.post("/setup/6", data={})

        call_kwargs = mock_setup.call_args[1] if mock_setup.call_args.kwargs else {}
        call_args = mock_setup.call_args
        # Extract rental_properties from positional or keyword args.
        try:
            props = (
                call_args.kwargs.get("rental_properties")
                or call_args[1].get("rental_properties")
                or []
            )
        except Exception:
            props = []
        assert any(p.get("name") == "Maple St" for p in props)
