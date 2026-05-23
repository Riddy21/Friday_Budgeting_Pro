"""
Tests for server/plaid_client.py

All Plaid API calls are mocked — no network access required.
"""

import os
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(extra: dict | None = None) -> dict:
    """Return a minimal valid env dict, optionally overriding keys."""
    base = {
        "PLAID_ENV": "sandbox",
        "PLAID_CLIENT_ID": "test-client-id",
        "PLAID_SECRET": "test-secret",
    }
    if extra:
        base.update(extra)
    return base


def _mock_plaid_api(mock_api_cls: MagicMock) -> MagicMock:
    """Return the PlaidApi instance mock from the class mock."""
    return mock_api_cls.return_value


# ---------------------------------------------------------------------------
# create_link_token
# ---------------------------------------------------------------------------

class TestCreateLinkToken(unittest.TestCase):

    @patch("server.plaid_client.plaid_api.PlaidApi")
    def test_returns_link_token(self, mock_api_cls):
        api = _mock_plaid_api(mock_api_cls)
        api.link_token_create.return_value = {"link_token": "link-sandbox-abc123"}

        with patch.dict(os.environ, _env(), clear=False):
            from server import plaid_client
            result = plaid_client.create_link_token()

        self.assertEqual(result, "link-sandbox-abc123")
        api.link_token_create.assert_called_once()

    @patch("server.plaid_client.plaid_api.PlaidApi")
    def test_custom_user_id(self, mock_api_cls):
        api = _mock_plaid_api(mock_api_cls)
        api.link_token_create.return_value = {"link_token": "link-sandbox-xyz"}

        with patch.dict(os.environ, _env(), clear=False):
            from server import plaid_client
            result = plaid_client.create_link_token(user_id="user-42")

        self.assertEqual(result, "link-sandbox-xyz")
        call_args = api.link_token_create.call_args
        # The request should have been built with user_id="user-42"
        request = call_args[0][0]
        self.assertEqual(request.user.client_user_id, "user-42")


# ---------------------------------------------------------------------------
# exchange_public_token
# ---------------------------------------------------------------------------

class TestExchangePublicToken(unittest.TestCase):

    @patch("server.plaid_client.plaid_api.PlaidApi")
    def test_returns_access_token_and_item_id(self, mock_api_cls):
        api = _mock_plaid_api(mock_api_cls)
        api.item_public_token_exchange.return_value = {
            "access_token": "access-sandbox-token-abc",
            "item_id": "item-id-xyz",
        }

        with patch.dict(os.environ, _env(), clear=False):
            from server import plaid_client
            result = plaid_client.exchange_public_token("public-sandbox-abc")

        self.assertEqual(result["access_token"], "access-sandbox-token-abc")
        self.assertEqual(result["item_id"], "item-id-xyz")
        api.item_public_token_exchange.assert_called_once()


# ---------------------------------------------------------------------------
# sync_transactions
# ---------------------------------------------------------------------------

class TestSyncTransactions(unittest.TestCase):

    @patch("server.plaid_client.plaid_api.PlaidApi")
    def test_sync_without_cursor(self, mock_api_cls):
        api = _mock_plaid_api(mock_api_cls)
        api.transactions_sync.return_value = {
            "added": [{"transaction_id": "tx1"}],
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-v2",
        }

        with patch.dict(os.environ, _env(), clear=False):
            from server import plaid_client
            result = plaid_client.sync_transactions("access-sandbox-token")

        self.assertEqual(len(result["added"]), 1)
        self.assertEqual(result["next_cursor"], "cursor-v2")
        self.assertEqual(result["modified"], [])
        self.assertEqual(result["removed"], [])

    @patch("server.plaid_client.plaid_api.PlaidApi")
    def test_sync_with_cursor(self, mock_api_cls):
        api = _mock_plaid_api(mock_api_cls)
        api.transactions_sync.return_value = {
            "added": [],
            "modified": [{"transaction_id": "tx2"}],
            "removed": [],
            "next_cursor": "cursor-v3",
        }

        with patch.dict(os.environ, _env(), clear=False):
            from server import plaid_client
            result = plaid_client.sync_transactions(
                "access-sandbox-token", cursor="cursor-v2"
            )

        self.assertEqual(result["next_cursor"], "cursor-v3")
        self.assertEqual(len(result["modified"]), 1)

        # Verify the cursor was passed in the request
        call_args = api.transactions_sync.call_args
        request = call_args[0][0]
        self.assertEqual(request.cursor, "cursor-v2")


# ---------------------------------------------------------------------------
# Environment-variable validation
# ---------------------------------------------------------------------------

class TestEnvValidation(unittest.TestCase):

    def test_bad_plaid_env_raises_value_error(self):
        bad_env = _env({"PLAID_ENV": "staging"})
        with patch.dict(os.environ, bad_env, clear=False):
            import importlib
            from server import plaid_client
            importlib.reload(plaid_client)
            with self.assertRaises(ValueError):
                plaid_client.create_link_token()

    @patch("server.plaid_client.plaid_api.PlaidApi")
    def test_missing_client_id_raises_environment_error(self, mock_api_cls):
        env = {
            "PLAID_ENV": "sandbox",
            "PLAID_SECRET": "test-secret",
        }
        # Remove PLAID_CLIENT_ID if present
        patched = {k: v for k, v in os.environ.items() if k != "PLAID_CLIENT_ID"}
        patched.update(env)
        with patch.dict(os.environ, patched, clear=True):
            import importlib
            from server import plaid_client
            importlib.reload(plaid_client)
            with self.assertRaises(EnvironmentError):
                plaid_client.create_link_token()

    @patch("server.plaid_client.plaid_api.PlaidApi")
    def test_missing_secret_raises_environment_error(self, mock_api_cls):
        env = {
            "PLAID_ENV": "sandbox",
            "PLAID_CLIENT_ID": "test-client-id",
        }
        patched = {k: v for k, v in os.environ.items() if k != "PLAID_SECRET"}
        patched.update(env)
        with patch.dict(os.environ, patched, clear=True):
            import importlib
            from server import plaid_client
            importlib.reload(plaid_client)
            with self.assertRaises(EnvironmentError):
                plaid_client.create_link_token()


if __name__ == "__main__":
    unittest.main()
