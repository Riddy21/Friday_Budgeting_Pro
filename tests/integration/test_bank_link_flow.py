"""
tests/integration/test_bank_link_flow.py — Integration test for Bug #218-B4.

Verifies that complete_link() triggers an initial sync that populates the
bank_accounts table, so the accounts page shows connected accounts immediately
after the Plaid Link flow.

This test uses real Plaid sandbox credentials when available, and is
gracefully skipped when they are absent (so CI always passes without secrets).

To run locally with real Plaid sandbox credentials:
    export PLAID_CLIENT_ID=<your_id>
    export PLAID_SECRET=<your_sandbox_secret>
    export PLAID_ENV=sandbox
    pytest tests/integration/test_bank_link_flow.py -v
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

PLAID_CREDS_AVAILABLE = bool(os.environ.get("PLAID_CLIENT_ID")) and bool(
    os.environ.get("PLAID_SECRET")
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SANDBOX_INSTITUTION_ID = "ins_109508"  # First Platypus Bank


def _init_test_db(db_path: Path) -> str:
    """Initialise a fresh DB and return the user_id of the seeded user."""
    from server.db import init_db

    init_db(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    import time

    conn.execute(
        "INSERT INTO users (id, username, password_hash, created_at) "
        "VALUES ('u-test', 'testuser', 'x', ?)",
        (int(time.time()),),
    )
    conn.commit()
    conn.close()
    return "u-test"


# ---------------------------------------------------------------------------
# Test: complete_link + sync populates bank_accounts (mocked Plaid)
# ---------------------------------------------------------------------------


def test_complete_link_triggers_sync_and_populates_accounts():
    """complete_link() must store the connection AND trigger a sync that
    populates bank_accounts so the UI can show them immediately.

    This test uses a fully mocked PlaidProvider so no real Plaid API calls
    are made.  The assertions mirror what the /accounts UI page expects.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        user_id = _init_test_db(db_path)

        with patch.dict(
            os.environ,
            {
                "PLAID_CLIENT_ID": "test-client-id",
                "PLAID_SECRET": "test-secret",
                "PLAID_ENV": "sandbox",
                "FRIDAY_BP_APP_DIR": tmpdir,
            },
            clear=False,
        ):
            import server.crypto as _crypto
            import server.main as _sm
            import server.paths as _paths

            orig_db = _paths.DB_PATH
            _paths.DB_PATH = db_path

            try:
                # Patch crypto so we don't need a real Keychain.
                with (
                    patch.object(_crypto, "encrypt", side_effect=lambda t: t + "_enc"),
                    patch.object(_crypto, "decrypt", side_effect=lambda t: t.replace("_enc", "")),
                ):
                    # ----------------------------------------------------------
                    # Step 1: Simulate complete_link with a mocked PlaidProvider
                    # ----------------------------------------------------------
                    mock_provider = MagicMock()
                    mock_provider.env = "sandbox"
                    mock_provider.exchange_public_token.return_value = {
                        "access_token": "access-sandbox-abc123",
                        "item_id": "item-sandbox-xyz",
                    }
                    mock_provider.get_institution_name.return_value = "First Platypus Bank"

                    with patch.object(_sm, "PlaidProvider", return_value=mock_provider):
                        result = _sm.complete_link(public_token="public-sandbox-tok")

                    assert "connection_id" in result, "complete_link must return a connection_id"
                    connection_id = result["connection_id"]

                    # Verify the connection was stored in the DB.
                    conn = sqlite3.connect(str(db_path))
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT * FROM bank_connections WHERE id = ?", (connection_id,)
                    ).fetchone()
                    conn.close()

                    assert row is not None, "bank_connections row not found after complete_link"
                    assert row["plaid_env"] == "sandbox"
                    assert row["institution_name"] == "First Platypus Bank"

                    # ----------------------------------------------------------
                    # Step 2: Simulate the post-link sync that populates accounts
                    # ----------------------------------------------------------
                    mock_sync_provider = MagicMock()
                    mock_sync_provider.env = "sandbox"
                    mock_sync_provider.sync_transactions.return_value = {
                        "added": [],
                        "modified": [],
                        "removed": [],
                        "next_cursor": "cursor-v1",
                        "accounts": [
                            {
                                "account_id": "acct-sandbox-chq",
                                "name": "Plaid Checking",
                                "official_name": "Plaid Gold Standard 0% Interest Checking",
                                "type": "depository",
                                "subtype": "checking",
                                "balances": {"current": 1500.0, "available": 1400.0},
                                "mask": "0000",
                            }
                        ],
                    }

                    with patch.object(_sm, "PlaidProvider", return_value=mock_sync_provider):
                        _sm.sync()

                    # Verify bank_accounts was populated.
                    conn = sqlite3.connect(str(db_path))
                    conn.row_factory = sqlite3.Row
                    accounts = conn.execute(
                        "SELECT * FROM bank_accounts WHERE connection_id = ?", (connection_id,)
                    ).fetchall()
                    conn.close()

                    assert len(accounts) > 0, (
                        "bank_accounts is empty after sync — the post-link sync did not "
                        "populate accounts.  The /accounts page would show nothing."
                    )
                    account_names = [a["name"] for a in accounts]
                    assert any(
                        "Plaid" in n or "Checking" in n for n in account_names
                    ), f"Expected an account with Plaid/Checking in bank_accounts, got: {account_names}"

            finally:
                _paths.DB_PATH = orig_db


# ---------------------------------------------------------------------------
# Test: complete_link + sync with REAL Plaid sandbox (skipped without creds)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not PLAID_CREDS_AVAILABLE,
    reason=(
        "PLAID_CLIENT_ID and/or PLAID_SECRET not set — real Plaid sandbox test skipped. "
        "Add to GitHub Actions secrets (PLAID_SANDBOX_CLIENT_ID / PLAID_SANDBOX_SECRET) "
        "to enable."
    ),
)
def test_complete_link_triggers_sync_and_populates_accounts_real_plaid():
    """End-to-end: mint a real Plaid sandbox token, call complete_link(),
    then sync(), and verify bank_accounts is populated.

    Requires:
        PLAID_CLIENT_ID, PLAID_SECRET (sandbox credentials)
        PLAID_ENV=sandbox
    """
    from plaid.model.products import Products
    from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest

    from server.providers.plaid import PlaidProvider

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        _init_test_db(db_path)

        with patch.dict(
            os.environ,
            {"PLAID_ENV": "sandbox", "FRIDAY_BP_APP_DIR": tmpdir},
            clear=False,
        ):
            import server.crypto as _crypto
            import server.main as _sm
            import server.paths as _paths

            orig_db = _paths.DB_PATH
            _paths.DB_PATH = db_path

            try:
                with (
                    patch.object(_crypto, "encrypt", side_effect=lambda t: t + "_enc"),
                    patch.object(_crypto, "decrypt", side_effect=lambda t: t.replace("_enc", "")),
                ):
                    provider = PlaidProvider(env="sandbox")
                    api_client = provider._build_client()
                    sandbox_req = SandboxPublicTokenCreateRequest(
                        institution_id=_SANDBOX_INSTITUTION_ID,
                        initial_products=[Products("transactions")],
                    )
                    sandbox_resp = api_client.sandbox_public_token_create(sandbox_req)
                    public_token = sandbox_resp["public_token"]

                    result = _sm.complete_link(public_token=public_token)
                    connection_id = result["connection_id"]

                    _sm.sync()

                    conn = sqlite3.connect(str(db_path))
                    conn.row_factory = sqlite3.Row
                    accounts = conn.execute(
                        "SELECT * FROM bank_accounts WHERE connection_id = ?", (connection_id,)
                    ).fetchall()
                    conn.close()

                    assert len(accounts) > 0, "bank_accounts empty after real Plaid sandbox sync"
            finally:
                _paths.DB_PATH = orig_db
