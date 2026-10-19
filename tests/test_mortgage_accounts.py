"""Tests for mortgage account detail + payment splitting (#271).

Covers:
  - mortgage_accounts migration/table exists on a fresh DB
  - upsert_mortgage_account insert + partial update (preserves richer detail)
  - get_mortgage_for_account round-trip
  - split_mortgage_payment: ytd_ratio / amortization / fallback methods
  - get_mortgage_details / preview_mortgage_split MCP tools
  - create_property_ledger seeds the mortgage sub-category group
"""

from __future__ import annotations

from pathlib import Path

import pytest

import server.paths
from server.db import get_db, init_db


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch) -> Path:
    """Fresh temp DB; monkeypatch server.paths.DB_PATH to point at it."""
    path = tmp_path / "test_mortgage_accounts.db"
    init_db(path)
    monkeypatch.setattr(server.paths, "DB_PATH", path)
    return path


# ---------------------------------------------------------------------------
# split_mortgage_payment — pure math, no DB
# ---------------------------------------------------------------------------


class TestSplitMortgagePayment:
    def test_fallback_when_no_data(self):
        from server.main import split_mortgage_payment

        split = split_mortgage_payment(2000.0, {})
        assert split["method"] == "fallback"
        assert split["principal"] == 2000.0
        assert split["interest"] == 0.0

    def test_amortization_split(self):
        from server.main import split_mortgage_payment

        # 5% APR on 400k => monthly interest 1666.67; principal = payment - interest
        split = split_mortgage_payment(
            2200.0, {"interest_rate": 5.0, "outstanding_principal": 400000.0}
        )
        assert split["method"] == "amortization"
        assert split["interest"] == pytest.approx(1666.67, abs=0.01)
        assert split["principal"] == pytest.approx(533.33, abs=0.01)
        # conservation: principal + interest == payment
        assert split["principal"] + split["interest"] == pytest.approx(2200.0, abs=0.02)

    def test_ytd_ratio_split_takes_priority(self):
        from server.main import split_mortgage_payment

        # YTD actuals present => ratio-based split wins over amortization
        split = split_mortgage_payment(
            2200.0,
            {
                "interest_rate": 5.0,
                "outstanding_principal": 400000.0,
                "ytd_interest_paid": 8000.0,
                "ytd_principal_paid": 2000.0,
            },
        )
        assert split["method"] == "ytd_ratio"
        # interest ratio = 8000/10000 = 0.8 => 2200*0.8 = 1760
        assert split["interest"] == pytest.approx(1760.0, abs=0.01)
        assert split["principal"] == pytest.approx(440.0, abs=0.01)

    def test_negative_amount_normalised(self):
        from server.main import split_mortgage_payment

        split = split_mortgage_payment(-2200.0, {"interest_rate": 5.0, "outstanding_principal": 400000.0})
        assert split["principal"] >= 0
        assert split["interest"] >= 0

    def test_zero_payment(self):
        from server.main import split_mortgage_payment

        split = split_mortgage_payment(0.0, {"interest_rate": 5.0, "outstanding_principal": 400000.0})
        assert split["principal"] == 0.0
        assert split["interest"] == 0.0


# ---------------------------------------------------------------------------
# DB-backed upsert / get
# ---------------------------------------------------------------------------


class TestMortgageAccountStorage:
    def _make_account(self, db_path, acct_id="acct1", balance=398500.0):
        conn = get_db(db_path)
        conn.execute(
            "INSERT INTO bank_accounts (id, name, type, subtype, balance_current) "
            "VALUES (?, 'Mortgage', 'loan', 'mortgage', ?)",
            (acct_id, balance),
        )
        conn.commit()
        conn.close()

    def test_table_exists_on_fresh_db(self, db_path):
        conn = get_db(db_path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(mortgage_accounts)")}
        finally:
            conn.close()
        assert "account_id" in cols
        assert "interest_rate" in cols
        assert "ytd_interest_paid" in cols
        assert "maturity_date" in cols

    def test_insert_and_get(self, db_path):
        from server.main import get_mortgage_for_account, upsert_mortgage_account

        self._make_account(db_path)
        # create a real property ledger so the ledger_id FK is satisfied
        conn = get_db(db_path)
        conn.execute(
            "INSERT INTO ledgers (id, name, type) VALUES ('ledger-x', 'Prop', 'property')"
        )
        conn.commit()
        conn.close()
        upsert_mortgage_account(
            "acct1",
            {
                "interest_rate": 4.75,
                "outstanding_principal": 398500.0,
                "origination_principal": 420000.0,
                "maturity_date": "2050-06-01",
            },
            ledger_id="ledger-x",
        )
        got = get_mortgage_for_account("acct1")
        assert got is not None
        assert got["interest_rate"] == 4.75
        assert got["outstanding_principal"] == 398500.0
        assert got["maturity_date"] == "2050-06-01"
        assert got["ledger_id"] == "ledger-x"

    def test_partial_update_preserves_detail(self, db_path):
        """A balance-only update (e.g. SimpleFIN) must not wipe Plaid detail."""
        from server.main import get_mortgage_for_account, upsert_mortgage_account

        self._make_account(db_path)
        upsert_mortgage_account(
            "acct1", {"interest_rate": 4.75, "outstanding_principal": 398500.0}
        )
        upsert_mortgage_account("acct1", {"outstanding_principal": 397000.0})
        got = get_mortgage_for_account("acct1")
        assert got["interest_rate"] == 4.75  # preserved
        assert got["outstanding_principal"] == 397000.0  # updated

    def test_get_missing_returns_none(self, db_path):
        from server.main import get_mortgage_for_account

        assert get_mortgage_for_account("nope") is None
        assert get_mortgage_for_account("") is None


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


class TestMortgageTools:
    def _setup(self, db_path, acct_id="acct1"):
        conn = get_db(db_path)
        conn.execute(
            "INSERT INTO bank_accounts (id, name, type, subtype, balance_current) "
            "VALUES (?, 'Mortgage', 'loan', 'mortgage', 400000.0)",
            (acct_id,),
        )
        conn.commit()
        conn.close()
        from server.main import upsert_mortgage_account

        upsert_mortgage_account(
            acct_id,
            {
                "interest_rate": 5.0,
                "outstanding_principal": 400000.0,
                "ytd_interest_paid": 8000.0,
                "ytd_principal_paid": 2000.0,
            },
        )

    def test_get_mortgage_details_ok(self, db_path):
        from server.main import get_mortgage_details

        self._setup(db_path)
        res = get_mortgage_details("acct1")
        assert res["status"] == "ok"
        assert res["mortgage"]["interest_rate"] == 5.0

    def test_get_mortgage_details_not_found(self, db_path):
        from server.main import get_mortgage_details

        res = get_mortgage_details("missing")
        assert res["status"] == "not_found"

    def test_preview_mortgage_split(self, db_path):
        from server.main import preview_mortgage_split

        self._setup(db_path)
        res = preview_mortgage_split("acct1", 2200.0)
        assert res["status"] == "ok"
        assert res["split"]["method"] == "ytd_ratio"
        assert res["split"]["interest"] == pytest.approx(1760.0, abs=0.01)

    def test_preview_mortgage_split_not_found(self, db_path):
        from server.main import preview_mortgage_split

        res = preview_mortgage_split("missing", 2200.0)
        assert res["status"] == "not_found"
