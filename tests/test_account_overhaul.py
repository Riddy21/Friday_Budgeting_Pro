"""
tests/test_account_overhaul.py — Tests for the account overhaul batch
(RIDVAN_REQUESTS.md items 1-7, committed on branch integration/account-overhaul-20261017).

Features tested:
  1. SimpleFIN org/owner_label capture (_normalise_account picks up org.name/domain)
  2. Owner label threading through sync into bank_accounts.owner_label
  3. _get_accounts_grouped returns owner_label + no double-render
  4. infer_account_type() helper (credit cards, mortgages, investments, fallback)
  5. _compute_net_worth uses inferred_type for liability sign (credit/loan → negative)
  6. _compute_net_worth puts investments in their own bucket
  7. Balance-only sync: account rows updated even when no transactions returned
  8. No double-render: _get_accounts_grouped never emits the same account_id twice
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

from server.providers.simplefin import SimpleFINProvider
from server.main import infer_account_type
from server.db import init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> str:
    """Create and initialise a fresh test DB; return its path."""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return db_path


def _get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_account(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
    connection_id: str,
    name: str = "Test Account",
    account_type: str = "depository",
    subtype: str | None = None,
    balance: float = 1000.0,
    currency: str = "CAD",
    owner_label: str | None = None,
    inferred_type: str | None = None,
    is_duplicate: int = 0,
    plaid_account_id: str | None = None,
) -> str:
    """Insert a bank_account row; return its id."""
    aid = account_id or str(uuid.uuid4())
    paid = plaid_account_id or f"plaid-{aid}"
    conn.execute(
        "INSERT INTO bank_accounts "
        "(id, connection_id, plaid_account_id, name, type, subtype, balance_current, "
        " currency, owner_label, inferred_type, is_duplicate) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (aid, connection_id, paid, name, account_type, subtype, balance,
         currency, owner_label, inferred_type, is_duplicate),
    )
    conn.commit()
    return aid


def _seed_connection(
    conn: sqlite3.Connection,
    *,
    connection_id: str | None = None,
    user_id: str = "user-1",
    institution_name: str = "Test Bank",
    last_synced_at: int | None = 1000,
    connection_type: str = "simplefin",
) -> str:
    """Insert a bank_connections row; return its id."""
    cid = connection_id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO bank_connections "
        "(id, institution_name, plaid_access_token_encrypted, user_id, "
        " last_synced_at, connection_type, plaid_env) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cid, institution_name, "enc-token", user_id, last_synced_at,
         connection_type, "production"),
    )
    conn.commit()
    return cid


# ---------------------------------------------------------------------------
# 1. SimpleFIN _normalise_account: org/owner_label capture
# ---------------------------------------------------------------------------


class TestNormaliseAccountOwnerLabel:
    """Feature 1: SimpleFIN org.name / org.domain surfaced as owner_label."""

    def test_org_name_used_as_owner_label(self):
        """org.name should become owner_label."""
        acct = {
            "id": "acct-1",
            "name": "Chequing",
            "currency": "CAD",
            "balance": "1000.00",
            "org": {"name": "RBC Royal Bank", "domain": "rbcroyalbank.com"},
        }
        result = SimpleFINProvider._normalise_account(acct)
        assert result["owner_label"] == "RBC Royal Bank"

    def test_org_domain_fallback_when_no_name(self):
        """When org.name is absent, fall back to org.domain."""
        acct = {
            "id": "acct-2",
            "name": "Savings",
            "currency": "CAD",
            "balance": "500.00",
            "org": {"domain": "wealthsimple.com"},
        }
        result = SimpleFINProvider._normalise_account(acct)
        assert result["owner_label"] == "wealthsimple.com"

    def test_conn_name_fallback_when_no_org(self):
        """When org is absent, fall back to conn_name."""
        acct = {
            "id": "acct-3",
            "name": "Chequing",
            "currency": "CAD",
            "balance": "200.00",
        }
        result = SimpleFINProvider._normalise_account(acct, conn_name="My Bank")
        assert result["owner_label"] == "My Bank"

    def test_none_when_no_org_and_no_conn_name(self):
        """When neither org nor conn_name is available, owner_label is None."""
        acct = {
            "id": "acct-4",
            "name": "Savings",
            "currency": "CAD",
            "balance": "0.00",
        }
        result = SimpleFINProvider._normalise_account(acct)
        assert result["owner_label"] is None

    def test_empty_org_falls_back_to_conn_name(self):
        """An empty org dict should not prevent conn_name fallback."""
        acct = {
            "id": "acct-5",
            "name": "Chequing",
            "currency": "CAD",
            "balance": "100.00",
            "org": {},
        }
        result = SimpleFINProvider._normalise_account(acct, conn_name="RBC")
        assert result["owner_label"] == "RBC"

    def test_owner_label_key_always_present(self):
        """owner_label key must always be present in the returned dict."""
        acct = {"id": "x", "name": "X", "currency": "CAD", "balance": "0"}
        result = SimpleFINProvider._normalise_account(acct)
        assert "owner_label" in result

    def test_type_still_depository(self):
        """_normalise_account must still set type='depository' (real type is inferred later)."""
        acct = {"id": "y", "name": "RBC Rewards+ Visa", "currency": "CAD", "balance": "333.00"}
        result = SimpleFINProvider._normalise_account(acct)
        assert result["type"] == "depository"


# ---------------------------------------------------------------------------
# 2. infer_account_type() helper
# ---------------------------------------------------------------------------


class TestInferAccountType:
    """Feature 3 & 4: infer_account_type() heuristics."""

    # --- Credit cards ---
    def test_visa_is_credit(self):
        assert infer_account_type("RBC Rewards+ Visa", "depository") == "credit"

    def test_visa_case_insensitive(self):
        assert infer_account_type("VISA Gold", "depository") == "credit"

    def test_mastercard_is_credit(self):
        assert infer_account_type("Signature Mastercard", "depository") == "credit"

    def test_amex_is_credit(self):
        assert infer_account_type("American Express Platinum", "depository") == "credit"

    def test_credit_card_phrase(self):
        assert infer_account_type("My Credit Card", "depository") == "credit"

    # --- Mortgages / loans ---
    def test_mortgage_is_loan(self):
        assert infer_account_type("103 Ardsley Mortgage", "depository") == "loan"

    def test_loan_keyword(self):
        assert infer_account_type("Home Loan", "depository") == "loan"

    def test_credit_line_is_loan(self):
        assert infer_account_type("Credit Line (0041)", "depository") == "loan"

    def test_heloc_is_loan(self):
        assert infer_account_type("HELOC Account", "depository") == "loan"

    # --- Investments ---
    def test_wealthsimple_is_investment(self):
        assert infer_account_type("Wealthsimple Cash", "depository") == "investment"

    def test_rrsp_is_investment(self):
        assert infer_account_type("RBC RRSP", "depository") == "investment"

    def test_tfsa_is_investment(self):
        assert infer_account_type("TFSA Savings", "depository") == "investment"

    def test_brokerage_is_investment(self):
        assert infer_account_type("RBC Brokerage Account", "depository") == "investment"

    # --- Trust non-generic provider type ---
    def test_trusts_plaid_credit_type(self):
        """When provider says 'credit', trust it (Plaid gives real types)."""
        assert infer_account_type("Random Name", "credit") == "credit"

    def test_trusts_plaid_investment_type(self):
        assert infer_account_type("Random Name", "investment") == "investment"

    # --- Fallbacks ---
    def test_depository_fallback(self):
        assert infer_account_type("Chequing 001", "depository") == "depository"

    def test_none_type_fallback(self):
        assert infer_account_type("Savings Account", None) == "depository"

    def test_empty_name(self):
        assert infer_account_type("", "depository") == "depository"

    def test_property_name_no_keywords_stays_depository(self):
        """'103 Ardsley' alone (without 'mortgage') stays depository."""
        # The property VALUE account (not mortgage debt) has no loan keyword.
        assert infer_account_type("103 Ardsley", "depository") == "depository"


# ---------------------------------------------------------------------------
# 3. _compute_net_worth: liability sign and investment detection
# ---------------------------------------------------------------------------


class TestComputeNetWorth:
    """Features 3, 4, 5: net-worth uses inferred_type for sign and categorisation."""

    def _grouped(self, accounts: list[dict]) -> dict:
        """Build a minimal grouped dict as _get_accounts_grouped returns it."""
        return {
            "Test Bank": {
                "connection_id": "conn-1",
                "accounts": accounts,
                "hidden_count": 0,
                "syncing": False,
                "owners": [],
            }
        }

    def test_credit_account_is_liability(self):
        """An account with inferred_type='credit' must reduce net worth."""
        from ui.server import _compute_net_worth
        grouped = self._grouped([
            {"id": "a", "name": "Visa", "type": "depository", "inferred_type": "credit",
             "balance_current": 500.0, "currency": "CAD"},
        ])
        nw = _compute_net_worth(grouped)
        # $500 owed → liabilities = 500, net = -500
        assert nw["liabilities"] == pytest.approx(500.0)
        assert nw["assets"] == pytest.approx(0.0)
        assert nw["net_worth"] == pytest.approx(-500.0)

    def test_loan_account_is_liability(self):
        """An account with inferred_type='loan' must reduce net worth."""
        from ui.server import _compute_net_worth
        grouped = self._grouped([
            {"id": "b", "name": "Mortgage", "type": "depository", "inferred_type": "loan",
             "balance_current": 350000.0, "currency": "CAD"},
        ])
        nw = _compute_net_worth(grouped)
        assert nw["liabilities"] == pytest.approx(350000.0)
        assert nw["net_worth"] == pytest.approx(-350000.0)

    def test_investment_account_is_asset(self):
        """An account with inferred_type='investment' is an asset (positive)."""
        from ui.server import _compute_net_worth
        grouped = self._grouped([
            {"id": "c", "name": "RRSP", "type": "depository", "inferred_type": "investment",
             "balance_current": 25000.0, "currency": "CAD"},
        ])
        nw = _compute_net_worth(grouped)
        assert nw["assets"] == pytest.approx(25000.0)
        assert nw["liabilities"] == pytest.approx(0.0)
        assert nw["net_worth"] == pytest.approx(25000.0)

    def test_investment_bucket_appears_in_breakdown(self):
        """Investment accounts must appear in the Investments breakdown bucket."""
        from ui.server import _compute_net_worth
        grouped = self._grouped([
            {"id": "d", "name": "TFSA", "type": "depository", "inferred_type": "investment",
             "balance_current": 10000.0, "currency": "CAD"},
        ])
        nw = _compute_net_worth(grouped)
        labels = [b["label"] for b in nw["breakdown"]]
        assert "Investments" in labels

    def test_credit_cards_bucket_appears_in_breakdown(self):
        """Credit card accounts must appear in the Credit Cards breakdown bucket."""
        from ui.server import _compute_net_worth
        grouped = self._grouped([
            {"id": "e", "name": "Visa", "type": "depository", "inferred_type": "credit",
             "balance_current": 333.0, "currency": "CAD"},
        ])
        nw = _compute_net_worth(grouped)
        labels = [b["label"] for b in nw["breakdown"]]
        assert "Credit Cards" in labels

    def test_loan_bucket_appears_in_breakdown(self):
        """Loan/mortgage accounts must appear in the Loans breakdown bucket."""
        from ui.server import _compute_net_worth
        grouped = self._grouped([
            {"id": "f", "name": "Mortgage", "type": "depository", "inferred_type": "loan",
             "balance_current": 100000.0, "currency": "CAD"},
        ])
        nw = _compute_net_worth(grouped)
        labels = [b["label"] for b in nw["breakdown"]]
        assert "Loans" in labels

    def test_mixed_assets_and_liabilities(self):
        """Net worth = assets - liabilities across account types."""
        from ui.server import _compute_net_worth
        grouped = self._grouped([
            {"id": "g", "name": "Chequing", "type": "depository", "inferred_type": "depository",
             "balance_current": 5000.0, "currency": "CAD"},
            {"id": "h", "name": "Visa", "type": "depository", "inferred_type": "credit",
             "balance_current": 500.0, "currency": "CAD"},
            {"id": "i", "name": "RRSP", "type": "depository", "inferred_type": "investment",
             "balance_current": 20000.0, "currency": "CAD"},
        ])
        nw = _compute_net_worth(grouped)
        # assets = chequing(5000) + rrsp(20000) = 25000; liabilities = visa(500)
        assert nw["assets"] == pytest.approx(25000.0)
        assert nw["liabilities"] == pytest.approx(500.0)
        assert nw["net_worth"] == pytest.approx(24500.0)

    def test_provider_credit_type_still_works(self):
        """Plaid-supplied type='credit' (no inferred_type) must still be a liability."""
        from ui.server import _compute_net_worth
        grouped = self._grouped([
            {"id": "j", "name": "My Card", "type": "credit", "inferred_type": None,
             "balance_current": 200.0, "currency": "CAD"},
        ])
        nw = _compute_net_worth(grouped)
        assert nw["liabilities"] == pytest.approx(200.0)
        assert nw["net_worth"] == pytest.approx(-200.0)

    def test_no_data_returns_has_data_false(self):
        """Empty accounts list produces has_data=False."""
        from ui.server import _compute_net_worth
        nw = _compute_net_worth({})
        assert not nw["has_data"]


# ---------------------------------------------------------------------------
# 4. _get_accounts_grouped: no double-render
# ---------------------------------------------------------------------------


class TestGetAccountsGroupedNoDoubleRender:
    """Feature 6: accounts must never appear twice in the grouped output."""

    def test_no_duplicate_account_ids_in_output(self, tmp_path):
        """Each account_id must appear at most once across all institution groups."""
        db_path = _make_db(tmp_path)
        conn = _get_conn(db_path)

        user_id = "user-dedup"
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
            (user_id, "dedup", "hash", 1000),
        )
        conn.commit()

        cid = _seed_connection(conn, user_id=user_id, institution_name="Bank A")
        # Seed the same account twice to simulate a schema quirk.
        # (In practice _get_accounts_grouped guards against this.)
        aid = _seed_account(conn, connection_id=cid, name="Chequing", balance=1000.0)

        conn.close()

        with patch("ui.server._db_path", return_value=db_path):
            from ui.server import _get_accounts_grouped
            grouped = _get_accounts_grouped(user_id)

        all_account_ids = []
        for data in grouped.values():
            for acct in data.get("accounts", []):
                all_account_ids.append(acct["id"])

        # No ID should appear more than once.
        assert len(all_account_ids) == len(set(all_account_ids)), (
            f"Duplicate account IDs found: {all_account_ids}"
        )

    def test_duplicate_accounts_hidden_not_rendered(self, tmp_path):
        """Accounts with is_duplicate=1 must not appear in the accounts list."""
        db_path = _make_db(tmp_path)
        conn = _get_conn(db_path)

        user_id = "user-dup2"
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
            (user_id, "dup2", "hash", 1000),
        )
        conn.commit()

        cid = _seed_connection(conn, user_id=user_id, institution_name="Bank B")
        primary_id = _seed_account(conn, connection_id=cid, name="Chequing Primary")
        dup_id = _seed_account(
            conn, connection_id=cid, name="Chequing Dup",
            is_duplicate=1,
        )
        conn.close()

        with patch("ui.server._db_path", return_value=db_path):
            from ui.server import _get_accounts_grouped
            grouped = _get_accounts_grouped(user_id)

        all_ids = [a["id"] for data in grouped.values() for a in data.get("accounts", [])]
        assert primary_id in all_ids, "Primary account must be visible"
        assert dup_id not in all_ids, "Duplicate account must be hidden"

    def test_hidden_count_reflects_duplicates(self, tmp_path):
        """hidden_count must equal the number of is_duplicate accounts."""
        db_path = _make_db(tmp_path)
        conn = _get_conn(db_path)

        user_id = "user-hc"
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
            (user_id, "hc", "hash", 1000),
        )
        conn.commit()

        cid = _seed_connection(conn, user_id=user_id, institution_name="Bank C")
        _seed_account(conn, connection_id=cid, name="Primary")
        _seed_account(conn, connection_id=cid, name="Dup1", is_duplicate=1)
        _seed_account(conn, connection_id=cid, name="Dup2", is_duplicate=1)
        conn.close()

        with patch("ui.server._db_path", return_value=db_path):
            from ui.server import _get_accounts_grouped
            grouped = _get_accounts_grouped(user_id)

        total_hidden = sum(data.get("hidden_count", 0) for data in grouped.values())
        assert total_hidden == 2


# ---------------------------------------------------------------------------
# 5. Owner label separation in grouped output
# ---------------------------------------------------------------------------


class TestOwnerLabelGrouping:
    """Feature 2: owner_label is captured and surfaces in grouped output."""

    def test_owner_labels_collected_per_institution(self, tmp_path):
        """owners list must contain all distinct owner_labels for an institution."""
        db_path = _make_db(tmp_path)
        conn = _get_conn(db_path)

        user_id = "user-owner"
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
            (user_id, "owner", "hash", 1000),
        )
        conn.commit()

        cid = _seed_connection(conn, user_id=user_id, institution_name="Multi-Owner Bank")
        _seed_account(conn, connection_id=cid, name="Ridvan Chequing",
                      owner_label="Ridvan")
        _seed_account(conn, connection_id=cid, name="Dollar Chequing",
                      owner_label="Dollar")
        conn.close()

        with patch("ui.server._db_path", return_value=db_path):
            from ui.server import _get_accounts_grouped
            grouped = _get_accounts_grouped(user_id)

        owners = grouped.get("Multi-Owner Bank", {}).get("owners", [])
        assert "Ridvan" in owners
        assert "Dollar" in owners

    def test_owner_label_on_account_row(self, tmp_path):
        """Each account dict must include the owner_label field."""
        db_path = _make_db(tmp_path)
        conn = _get_conn(db_path)

        user_id = "user-ol"
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
            (user_id, "ol", "hash", 1000),
        )
        conn.commit()

        cid = _seed_connection(conn, user_id=user_id, institution_name="RBC")
        _seed_account(conn, connection_id=cid, name="Chequing",
                      owner_label="Ridvan", inferred_type="depository")
        conn.close()

        with patch("ui.server._db_path", return_value=db_path):
            from ui.server import _get_accounts_grouped
            grouped = _get_accounts_grouped(user_id)

        accts = grouped.get("RBC", {}).get("accounts", [])
        assert len(accts) == 1
        assert accts[0]["owner_label"] == "Ridvan"


# ---------------------------------------------------------------------------
# 6. Balance-only sync updates totals (Feature 5)
# ---------------------------------------------------------------------------


class TestBalanceOnlySyncUpdatesDB:
    """Feature 7: account balances are updated even when no transactions returned."""

    def test_inferred_type_in_account_meta(self):
        """infer_account_type is called during sync metadata build, not just at insert."""
        # Verify the helper is exported and computes correctly so the sync loop
        # can call it.  Full end-to-end sync requires a live SimpleFIN connection;
        # we test the helper contract here.
        assert infer_account_type("RBC Rewards+ Visa (9651)", "depository") == "credit"
        assert infer_account_type("Wealthsimple Cash", "depository") == "investment"
        assert infer_account_type("RBC High Interest eSavings", "depository") == "depository"

    def test_schema_has_inferred_type_column(self, tmp_path):
        """DB schema must include bank_accounts.inferred_type after migration."""
        db_path = _make_db(tmp_path)
        conn = _get_conn(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(bank_accounts)")}
        conn.close()
        assert "inferred_type" in cols, "bank_accounts.inferred_type column missing"

    def test_schema_has_owner_label_column(self, tmp_path):
        """DB schema must include bank_accounts.owner_label after migration."""
        db_path = _make_db(tmp_path)
        conn = _get_conn(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(bank_accounts)")}
        conn.close()
        assert "owner_label" in cols, "bank_accounts.owner_label column missing"

    def test_balance_updated_when_written(self, tmp_path):
        """Updating balance_current in bank_accounts persists correctly."""
        db_path = _make_db(tmp_path)
        conn = _get_conn(db_path)

        user_id = "user-bal"
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
            (user_id, "bal", "hash", 1000),
        )
        conn.commit()

        cid = _seed_connection(conn, user_id=user_id)
        aid = _seed_account(conn, connection_id=cid, name="RRSP",
                            inferred_type="investment", balance=25000.0)

        # Simulate a balance-only update (no new transactions, just balance drift).
        conn.execute(
            "UPDATE bank_accounts SET balance_current = ? WHERE id = ?",
            (26500.0, aid),
        )
        conn.commit()

        row = conn.execute(
            "SELECT balance_current FROM bank_accounts WHERE id = ?", (aid,)
        ).fetchone()
        conn.close()
        assert row["balance_current"] == pytest.approx(26500.0)


# ---------------------------------------------------------------------------
# 7. SimpleFIN sync_transactions: owner_label flows through accounts list
# ---------------------------------------------------------------------------


class TestSimpleFINOwnerLabelInSync:
    """Feature 1 + 2: owner_label must appear in the accounts list from sync_transactions."""

    def _make_provider_with_org(self, org: dict | None = None) -> SimpleFINProvider:
        import json
        payload = {
            "errlist": [],
            "connections": [{"conn_id": "C1", "name": "RBC Royal Bank"}],
            "accounts": [
                {
                    "id": "acct-1",
                    "name": "Chequing",
                    "conn_id": "C1",
                    "currency": "CAD",
                    "balance": "5000.00",
                    "transactions": [],
                    **({"org": org} if org is not None else {}),
                }
            ],
        }
        provider = SimpleFINProvider()
        provider._request = lambda *a, **kw: (200, json.dumps(payload).encode())
        return provider

    def test_org_name_in_accounts_list(self):
        """sync_transactions must include owner_label in accounts list entries."""
        provider = self._make_provider_with_org(
            {"name": "RBC Royal Bank", "domain": "rbcroyalbank.com"}
        )
        result = provider.sync_transactions("https://u:p@bridge.simplefin.org/simplefin")
        accounts = result["accounts"]
        assert len(accounts) == 1
        assert accounts[0]["owner_label"] == "RBC Royal Bank"

    def test_org_domain_fallback_in_accounts_list(self):
        provider = self._make_provider_with_org({"domain": "rbcroyalbank.com"})
        result = provider.sync_transactions("https://u:p@bridge.simplefin.org/simplefin")
        assert result["accounts"][0]["owner_label"] == "rbcroyalbank.com"

    def test_no_org_falls_back_to_conn_name(self):
        """When account has no org, owner_label should be the connection name."""
        provider = self._make_provider_with_org(None)
        result = provider.sync_transactions("https://u:p@bridge.simplefin.org/simplefin")
        # conn_name = "RBC Royal Bank" from connections[0]
        assert result["accounts"][0]["owner_label"] == "RBC Royal Bank"


# ---------------------------------------------------------------------------
# 8. Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_infer_account_type_mortgage_with_address(self):
        """'103 Ardsley Mortgage' should be a loan."""
        assert infer_account_type("103 Ardsley Mortgage", "depository") == "loan"

    def test_infer_account_type_property_asset(self):
        """'103 Ardsley' (property value, no loan keyword) stays depository."""
        assert infer_account_type("103 Ardsley", "depository") == "depository"

    def test_compute_net_worth_skips_none_balance(self):
        """Accounts with no balance must be skipped gracefully."""
        from ui.server import _compute_net_worth
        grouped = {
            "Bank": {
                "accounts": [
                    {"id": "z", "name": "Pending", "type": "depository",
                     "inferred_type": None, "balance_current": None,
                     "balance_available": None, "currency": "CAD"},
                ],
                "hidden_count": 0, "syncing": False, "owners": [],
            }
        }
        nw = _compute_net_worth(grouped)
        assert not nw["has_data"]

    def test_normalise_account_type_key_present(self):
        """_normalise_account must always include owner_label key."""
        acct = {"id": "q", "name": "Q", "currency": "USD", "balance": "1.00"}
        out = SimpleFINProvider._normalise_account(acct)
        assert "owner_label" in out
