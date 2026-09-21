"""
Tests for server/providers/flinks.py (FlinksProvider)

All Flinks API calls are mocked via the single ``_request`` seam — no network
access required.  Mirrors tests/test_plaid_provider.py in spirit.
"""

from __future__ import annotations

import unittest

from server.providers.flinks import (
    FlinksAPIError,
    FlinksAuthError,
    FlinksNotConfiguredError,
    FlinksProvider,
)

# ---------------------------------------------------------------------------
# Public Flinks sandbox credentials (toolbox instance)
# ---------------------------------------------------------------------------

SANDBOX = {
    "customer_id": "43387ca6-0391-4c82-857d-70d95f087ecb",
    "secret_key": "c4569c54-e167-4d34-8de6-f4113bc82414",
    "x_api_key": "3d5266a8-b697-48d4-8de6-52e2e2662acc",
    "instance": "toolbox",
}

LOGIN_ID = "login-abc-123"

# A GetAccountsDetail-shaped payload matching the Flinks v3 docs.
ACCOUNTS_PAYLOAD = {
    "InstitutionName": "RBC Royal Bank",
    "Accounts": [
        {
            "Id": "acct-1",
            "Title": "Chequing",
            "LastFourDigits": "1234",
            "Balance": {"Available": 100.0, "Current": 100.0, "Limit": None},
            "Category": "operations",
            "Type": "Checking",
            "Currency": "CAD",
            "Transactions": [
                {
                    "Id": "txn-debit",
                    "Date": "2026-09-01T00:00:00Z",
                    "Description": "Tim Hortons",
                    "Debit": 5.50,
                    "Credit": None,
                    "Balance": 94.50,
                },
                {
                    "Id": "txn-credit",
                    "Date": "2026-09-02T00:00:00Z",
                    "Description": "Payroll Deposit",
                    "Debit": None,
                    "Credit": 2000.00,
                    "Balance": 2094.50,
                },
            ],
        }
    ],
}


def _make_provider(**overrides):
    """Build a provider with sandbox creds and a no-op sleep."""
    kwargs = dict(SANDBOX)
    kwargs.update(overrides)
    p = FlinksProvider(**kwargs)
    p._sleep = lambda *_a, **_k: None  # never really wait during tests
    return p


def _router(*, detail_status=200, detail_payload=None, async_payload=None):
    """Return a fake ``_request`` that routes by URL to canned responses.

    Ordering matters: 'GenerateAuthorizeToken' must be checked before the bare
    'Authorize' endpoint, and 'GetAccountsDetailAsync' before 'GetAccountsDetail'.
    """
    detail_payload = detail_payload if detail_payload is not None else ACCOUNTS_PAYLOAD
    async_payload = async_payload if async_payload is not None else ACCOUNTS_PAYLOAD

    def fake_request(method, url, headers=None, body=None):
        if "GenerateAuthorizeToken" in url:
            return 200, {"AuthorizeToken": "authorize-token-xyz"}
        if "GetAccountsDetailAsync" in url:
            return 200, async_payload
        if "GetAccountsDetail" in url:
            if detail_status == 202:
                return 202, {}
            return 200, detail_payload
        if "Authorize" in url:
            return 200, {"RequestId": "req-456"}
        raise AssertionError(f"unexpected url in test: {url}")

    return fake_request


# ---------------------------------------------------------------------------
# Configuration / guards
# ---------------------------------------------------------------------------


class TestConfiguration(unittest.TestCase):
    def test_is_configured_true_with_all_creds(self):
        self.assertTrue(_make_provider().is_configured())

    def test_is_configured_false_when_missing(self):
        p = FlinksProvider(customer_id=None, secret_key=None, x_api_key=None)
        self.assertFalse(p.is_configured())

    def test_generate_token_requires_config(self):
        p = FlinksProvider(customer_id="", secret_key="", x_api_key="")
        with self.assertRaises(FlinksNotConfiguredError):
            p.generate_authorize_token()

    def test_api_and_iframe_bases_use_instance(self):
        p = _make_provider(instance="toolbox")
        self.assertEqual(p._api_base(), "https://toolbox-api.private.fin.ag")
        self.assertEqual(p._iframe_base(), "https://toolbox-iframe.private.fin.ag")


# ---------------------------------------------------------------------------
# Connect flow: token / connect url / authorize
# ---------------------------------------------------------------------------


class TestConnectFlow(unittest.TestCase):
    def test_generate_authorize_token(self):
        p = _make_provider()
        p._request = _router()
        self.assertEqual(p.generate_authorize_token(), "authorize-token-xyz")

    def test_create_connect_url_sandbox_has_demo(self):
        p = _make_provider(instance="toolbox")
        url = p.create_connect_url("tok-1")
        self.assertIn("https://toolbox-iframe.private.fin.ag/v2/", url)
        self.assertIn("authorizeToken=tok-1", url)
        self.assertIn("demo=true", url)

    def test_create_connect_url_production_no_demo(self):
        p = _make_provider(instance="production")
        url = p.create_connect_url("tok-1")
        self.assertNotIn("demo=true", url)

    def test_authorize_returns_request_id(self):
        p = _make_provider()
        p._request = _router()
        self.assertEqual(p.authorize(LOGIN_ID), "req-456")


# ---------------------------------------------------------------------------
# GetAccountsDetail — sync (200) and async (202 → poll → 200)
# ---------------------------------------------------------------------------


class TestGetAccountsDetail(unittest.TestCase):
    def test_direct_200(self):
        p = _make_provider()
        p._request = _router(detail_status=200)
        data = p.get_accounts_detail("req-456")
        self.assertEqual(data["InstitutionName"], "RBC Royal Bank")

    def test_async_202_then_poll_200(self):
        p = _make_provider()
        calls = {"n": 0}
        base = _router(detail_status=202)

        def counting(method, url, headers=None, body=None):
            if "GetAccountsDetailAsync" in url:
                calls["n"] += 1
            return base(method, url, headers=headers, body=body)

        p._request = counting
        data = p.get_accounts_detail("req-456")
        self.assertEqual(data["InstitutionName"], "RBC Royal Bank")
        self.assertGreaterEqual(calls["n"], 1)  # polled the async endpoint

    def test_async_gives_up_after_max_polls(self):
        p = _make_provider()
        p.max_polls = 2

        def always_202(method, url, headers=None, body=None):
            if "GetAccountsDetailAsync" in url:
                return 202, {}
            if "GetAccountsDetail" in url:
                return 202, {}
            raise AssertionError(url)

        p._request = always_202
        with self.assertRaises(FlinksAPIError):
            p.get_accounts_detail("req-456")


# ---------------------------------------------------------------------------
# Normalisation to Plaid shape
# ---------------------------------------------------------------------------


class TestNormalisation(unittest.TestCase):
    def test_get_accounts_plaid_shape(self):
        p = _make_provider()
        p._request = _router()
        accounts = p.get_accounts(LOGIN_ID)
        self.assertEqual(len(accounts), 1)
        a = accounts[0]
        self.assertEqual(a["account_id"], "acct-1")
        self.assertEqual(a["official_name"], "Chequing")
        self.assertEqual(a["name"], "Chequing")
        self.assertEqual(a["type"], "Checking")
        self.assertEqual(a["mask"], "1234")
        self.assertEqual(a["balances"]["current"], 100.0)
        self.assertEqual(a["balances"]["available"], 100.0)
        self.assertEqual(a["balances"]["iso_currency_code"], "CAD")

    def test_transaction_amount_signs(self):
        p = _make_provider()
        p._request = _router()
        txns = {t["transaction_id"]: t for t in p.get_transactions(LOGIN_ID)}

        # Debit (money out) → positive amount (Plaid convention).
        self.assertEqual(txns["txn-debit"]["amount"], 5.50)
        # Credit (money in) → negative amount.
        self.assertEqual(txns["txn-credit"]["amount"], -2000.00)

    def test_transaction_field_mapping(self):
        p = _make_provider()
        p._request = _router()
        t = next(x for x in p.get_transactions(LOGIN_ID) if x["transaction_id"] == "txn-debit")
        self.assertEqual(t["name"], "Tim Hortons")
        self.assertEqual(t["merchant_name"], "Tim Hortons")
        self.assertEqual(t["account_id"], "acct-1")
        self.assertEqual(t["date"], "2026-09-01")  # ISO date only
        self.assertIsNone(t["authorized_datetime"])
        self.assertFalse(t["pending"])
        self.assertEqual(t["iso_currency_code"], "CAD")

    def test_get_transactions_since_filter(self):
        p = _make_provider()
        p._request = _router()
        txns = p.get_transactions(LOGIN_ID, since="2026-09-02")
        ids = {t["transaction_id"] for t in txns}
        self.assertEqual(ids, {"txn-credit"})  # 09-01 filtered out


# ---------------------------------------------------------------------------
# sync_transactions adapter
# ---------------------------------------------------------------------------


class TestSyncTransactions(unittest.TestCase):
    def test_sync_output_shape(self):
        p = _make_provider()
        p._request = _router()
        result = p.sync_transactions(LOGIN_ID)
        self.assertEqual(set(result.keys()),
                         {"added", "modified", "removed", "next_cursor", "accounts"})
        self.assertEqual(len(result["added"]), 2)
        self.assertEqual(result["modified"], [])
        self.assertEqual(result["removed"], [])
        self.assertEqual(len(result["accounts"]), 1)
        self.assertTrue(result["next_cursor"])  # today's date string

    def test_sync_requires_login_id(self):
        p = _make_provider()
        with self.assertRaises(FlinksNotConfiguredError):
            p.sync_transactions(None)


# ---------------------------------------------------------------------------
# 401 handling → needs_reauth
# ---------------------------------------------------------------------------


class TestReauth(unittest.TestCase):
    def test_401_raises_flinks_auth_error_with_reauth_body(self):
        p = _make_provider()

        def raise_401(method, url, headers=None, body=None):
            if "GenerateAuthorizeToken" in url:
                raise FlinksAuthError("expired login")
            raise AssertionError("should have raised before this url")

        p._request = raise_401
        with self.assertRaises(FlinksAuthError) as ctx:
            p.sync_transactions(LOGIN_ID)
        # Body is Plaid-shaped so server.main.sync flips to needs_reauth.
        self.assertIn("ITEM_LOGIN_REQUIRED", ctx.exception.body)

    def test_get_item_status_maps_401_to_login_required(self):
        p = _make_provider()

        def raise_401(method, url, headers=None, body=None):
            raise FlinksAuthError("expired login")

        p._request = raise_401
        status = p.get_item_status(LOGIN_ID)
        self.assertEqual(status["error_code"], "ITEM_LOGIN_REQUIRED")

    def test_get_item_status_ok_when_healthy(self):
        p = _make_provider()
        p._request = _router()
        status = p.get_item_status(LOGIN_ID)
        self.assertIsNone(status["error_code"])


# ---------------------------------------------------------------------------
# BankProvider interface: unsupported Link methods
# ---------------------------------------------------------------------------


class TestUnsupportedLinkMethods(unittest.TestCase):
    def test_create_link_token_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            _make_provider().create_link_token()

    def test_exchange_public_token_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            _make_provider().exchange_public_token("pub-token")


if __name__ == "__main__":
    unittest.main()
