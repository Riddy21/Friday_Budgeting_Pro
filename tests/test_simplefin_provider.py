"""
Tests for server/providers/simplefin.py (SimpleFINProvider)

All network calls are mocked via the ``_request`` seam — no network access
required.
"""

from __future__ import annotations

import base64
import datetime
import json
import unittest

from server.providers.simplefin import (
    SimpleFINAPIError,
    SimpleFINAuthError,
    SimpleFINProvider,
    _to_float,
)

# ---------------------------------------------------------------------------
# Sample data matching the SimpleFIN protocol spec
# ---------------------------------------------------------------------------

# A realistic /accounts response payload.
ACCOUNTS_PAYLOAD = {
    "errlist": [],
    "connections": [
        {
            "conn_id": "CON-001",
            "name": "My Test Bank - Alice",
            "org_id": "ORG-001",
            "org_url": "https://testbank.example.com",
            "sfin_url": "https://testbank.example.com",
        }
    ],
    "accounts": [
        {
            "id": "acct-chequing",
            "name": "Chequing",
            "conn_id": "CON-001",
            "currency": "CAD",
            "balance": "1000.00",
            "available-balance": "950.00",
            "balance-date": 1700000000,
            "transactions": [
                {
                    "id": "txn-debit",
                    "posted": 1700000000,  # 2023-11-14T22:13:20Z
                    "amount": "-50.25",  # debit: money out
                    "description": "Grocery Store",
                },
                {
                    "id": "txn-credit",
                    "posted": 1700100000,
                    "amount": "2000.00",  # credit: money in (payroll)
                    "description": "Payroll Deposit",
                },
            ],
        },
        {
            "id": "acct-savings",
            "name": "Savings",
            "conn_id": "CON-001",
            "currency": "CAD",
            "balance": "5000.00",
            "available-balance": "5000.00",
            "balance-date": 1700000000,
            "transactions": [],
        },
    ],
}

# A minimal access URL (without real credentials).
ACCESS_URL = "https://testuser:testpass@bridge.simplefin.org/simplefin"

# A fake setup token: base64("https://bridge.simplefin.org/simplefin/claim/DEMO")
FAKE_CLAIM_URL = "https://bridge.simplefin.org/simplefin/claim/DEMO"
FAKE_SETUP_TOKEN = base64.b64encode(FAKE_CLAIM_URL.encode()).decode()


# ---------------------------------------------------------------------------
# Helper: build a provider whose _request method returns a canned response
# ---------------------------------------------------------------------------


def _provider_with_response(status: int, body: bytes | dict | str) -> SimpleFINProvider:
    """Return a SimpleFINProvider whose ``_request`` always returns the given response."""
    if isinstance(body, dict):
        raw = json.dumps(body).encode("utf-8")
    elif isinstance(body, str):
        raw = body.encode("utf-8")
    else:
        raw = body

    provider = SimpleFINProvider()

    def _mock_request(method, url, **kwargs):  # noqa: ANN001
        if status == 403:
            raise SimpleFINAuthError("mock 403")
        if status >= 400:
            raise SimpleFINAPIError(status, "mock error")
        return status, raw

    provider._request = _mock_request
    return provider


# ---------------------------------------------------------------------------
# Tests: claim_setup_token
# ---------------------------------------------------------------------------


class TestClaimSetupToken(unittest.TestCase):
    def test_claim_returns_access_url(self):
        """A valid setup token is base64-decoded and POSTed; the response is returned."""
        provider = _provider_with_response(200, ACCESS_URL)
        result = provider.claim_setup_token(FAKE_SETUP_TOKEN)
        self.assertEqual(result, ACCESS_URL)

    def test_empty_token_raises_value_error(self):
        provider = SimpleFINProvider()
        with self.assertRaises(ValueError):
            provider.claim_setup_token("")

    def test_whitespace_only_token_raises_value_error(self):
        provider = SimpleFINProvider()
        with self.assertRaises(ValueError):
            provider.claim_setup_token("   ")

    def test_non_base64_token_raises_value_error(self):
        provider = SimpleFINProvider()
        with self.assertRaises(ValueError):
            provider.claim_setup_token("!!!not-base64!!!")

    def test_non_https_claim_url_raises_value_error(self):
        """Decoded URL must be HTTPS."""
        bad_token = base64.b64encode(b"http://evil.example.com/claim").decode()
        provider = SimpleFINProvider()
        with self.assertRaises(ValueError, msg="non-HTTPS URL must be rejected"):
            provider.claim_setup_token(bad_token)

    def test_403_raises_auth_error(self):
        """403 from the claim endpoint means the token was already used."""
        provider = _provider_with_response(403, b"Forbidden")
        with self.assertRaises(SimpleFINAuthError):
            provider.claim_setup_token(FAKE_SETUP_TOKEN)

    def test_non_https_response_raises_api_error(self):
        """If the server returns a non-HTTPS body, raise SimpleFINAPIError."""
        provider = _provider_with_response(200, b"http://not-https/access")
        with self.assertRaises(SimpleFINAPIError):
            provider.claim_setup_token(FAKE_SETUP_TOKEN)

    def test_token_with_whitespace_is_stripped(self):
        """Leading/trailing whitespace in the token should be stripped."""
        provider = _provider_with_response(200, ACCESS_URL)
        result = provider.claim_setup_token(f"  {FAKE_SETUP_TOKEN}  ")
        self.assertEqual(result, ACCESS_URL)


# ---------------------------------------------------------------------------
# Tests: _parse_access_url
# ---------------------------------------------------------------------------


class TestParseAccessUrl(unittest.TestCase):
    def test_standard_url(self):
        url, user, pwd = SimpleFINProvider._parse_access_url(ACCESS_URL)
        self.assertEqual(url, "https://bridge.simplefin.org/simplefin/accounts")
        self.assertEqual(user, "testuser")
        self.assertEqual(pwd, "testpass")

    def test_url_with_port(self):
        url, user, pwd = SimpleFINProvider._parse_access_url(
            "https://alice:s3cr3t@bridge.example.com:9000/simplefin"
        )
        self.assertEqual(url, "https://bridge.example.com:9000/simplefin/accounts")
        self.assertEqual(user, "alice")
        self.assertEqual(pwd, "s3cr3t")

    def test_invalid_url_raises(self):
        with self.assertRaises(ValueError):
            SimpleFINProvider._parse_access_url("not-a-url")


# ---------------------------------------------------------------------------
# Tests: sync_transactions
# ---------------------------------------------------------------------------


class TestSyncTransactions(unittest.TestCase):
    def _provider(self):
        return _provider_with_response(200, ACCOUNTS_PAYLOAD)

    def test_returns_plaid_shaped_dict(self):
        result = self._provider().sync_transactions(ACCESS_URL)
        self.assertIn("added", result)
        self.assertIn("modified", result)
        self.assertIn("removed", result)
        self.assertIn("next_cursor", result)
        self.assertIn("accounts", result)

    def test_modified_and_removed_are_empty(self):
        """SimpleFIN is a point-in-time snapshot; modified/removed are always empty."""
        result = self._provider().sync_transactions(ACCESS_URL)
        self.assertEqual(result["modified"], [])
        self.assertEqual(result["removed"], [])

    def test_accounts_count(self):
        result = self._provider().sync_transactions(ACCESS_URL)
        self.assertEqual(len(result["accounts"]), 2)

    def test_transactions_count(self):
        result = self._provider().sync_transactions(ACCESS_URL)
        # Chequing has 2 txns; Savings has 0.
        self.assertEqual(len(result["added"]), 2)

    def test_debit_amount_sign(self):
        """SimpleFIN negative amount → Plaid positive amount (money out)."""
        result = self._provider().sync_transactions(ACCESS_URL)
        debit_txn = next(t for t in result["added"] if t["transaction_id"] == "txn-debit")
        self.assertAlmostEqual(debit_txn["amount"], 50.25)

    def test_credit_amount_sign(self):
        """SimpleFIN positive amount → Plaid negative amount (money in)."""
        result = self._provider().sync_transactions(ACCESS_URL)
        credit_txn = next(t for t in result["added"] if t["transaction_id"] == "txn-credit")
        self.assertAlmostEqual(credit_txn["amount"], -2000.00)

    def test_transaction_date_format(self):
        """Posted Unix timestamp is converted to YYYY-MM-DD."""
        result = self._provider().sync_transactions(ACCESS_URL)
        txn = next(t for t in result["added"] if t["transaction_id"] == "txn-debit")
        # 1700000000 UTC → 2023-11-14
        self.assertEqual(txn["date"], "2023-11-14")

    def test_next_cursor_is_today(self):
        result = self._provider().sync_transactions(ACCESS_URL)
        today = datetime.date.today().isoformat()
        self.assertEqual(result["next_cursor"], today)

    def test_pending_is_false(self):
        """SimpleFIN only returns posted transactions; pending should always be False."""
        result = self._provider().sync_transactions(ACCESS_URL)
        for txn in result["added"]:
            self.assertFalse(txn["pending"])

    def test_account_normalisation(self):
        result = self._provider().sync_transactions(ACCESS_URL)
        chequing = next(a for a in result["accounts"] if a["account_id"] == "acct-chequing")
        self.assertEqual(chequing["name"], "Chequing")
        self.assertAlmostEqual(chequing["balances"]["current"], 1000.0)
        self.assertAlmostEqual(chequing["balances"]["available"], 950.0)
        self.assertEqual(chequing["balances"]["iso_currency_code"], "CAD")

    def test_cursor_applies_overlap(self):
        """With a cursor, start-date should be overlap_days before the cursor date."""
        calls = []

        def _capturing_request(method, url, **kwargs):
            calls.append(url)
            return 200, json.dumps(ACCOUNTS_PAYLOAD).encode()

        provider = SimpleFINProvider(overlap_days=5)
        provider._request = _capturing_request

        cursor = "2024-06-01"
        provider.sync_transactions(ACCESS_URL, cursor=cursor)

        self.assertTrue(calls, "Expected at least one HTTP call")
        url = calls[0]
        # start-date should be 5 days before 2024-06-01 = 2024-05-27
        expected_start = int(
            datetime.datetime(2024, 5, 27, tzinfo=datetime.timezone.utc).timestamp()
        )
        self.assertIn(f"start-date={expected_start}", url)

    def test_no_cursor_uses_90_days(self):
        """Without a cursor, start-date should be 90 days ago."""
        calls = []

        def _capturing_request(method, url, **kwargs):
            calls.append(url)
            return 200, json.dumps(ACCOUNTS_PAYLOAD).encode()

        provider = SimpleFINProvider()
        provider._request = _capturing_request
        provider.sync_transactions(ACCESS_URL, cursor=None)

        url = calls[0]
        expected_start = int(
            (datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=90))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        # Allow a small drift (seconds) due to test execution time.
        self.assertIn("start-date=", url)

    def test_403_raises_auth_error(self):
        provider = _provider_with_response(403, b"Forbidden")
        # sync_transactions itself does not catch auth errors — the caller
        # (sync loop in server.main) handles them and sets needs_reauth.
        with self.assertRaises(SimpleFINAuthError):
            provider.sync_transactions(ACCESS_URL)


# ---------------------------------------------------------------------------
# Tests: get_item_status
# ---------------------------------------------------------------------------


class TestGetItemStatus(unittest.TestCase):
    def test_healthy_returns_no_error(self):
        provider = _provider_with_response(200, ACCOUNTS_PAYLOAD)
        status = provider.get_item_status(ACCESS_URL)
        self.assertIsNone(status["error_code"])
        self.assertIsNone(status["error_message"])

    def test_403_returns_item_login_required(self):
        provider = _provider_with_response(403, b"Forbidden")
        status = provider.get_item_status(ACCESS_URL)
        self.assertEqual(status["error_code"], "ITEM_LOGIN_REQUIRED")
        self.assertIsNotNone(status["error_message"])

    def test_api_error_returns_provider_error(self):
        provider = _provider_with_response(500, b"Server Error")
        status = provider.get_item_status(ACCESS_URL)
        self.assertEqual(status["error_code"], "PROVIDER_ERROR")


# ---------------------------------------------------------------------------
# Tests: remove_item
# ---------------------------------------------------------------------------


class TestRemoveItem(unittest.TestCase):
    def test_returns_not_revoked(self):
        provider = SimpleFINProvider()
        result = provider.remove_item(ACCESS_URL)
        self.assertFalse(result["revoked"])
        self.assertIn("reason", result)


# ---------------------------------------------------------------------------
# Tests: not-implemented stubs
# ---------------------------------------------------------------------------


class TestNotImplemented(unittest.TestCase):
    def test_create_link_token_raises(self):
        provider = SimpleFINProvider()
        with self.assertRaises(NotImplementedError):
            provider.create_link_token()

    def test_exchange_public_token_raises(self):
        provider = SimpleFINProvider()
        with self.assertRaises(NotImplementedError):
            provider.exchange_public_token("fake-token")


# ---------------------------------------------------------------------------
# Tests: _to_float helper
# ---------------------------------------------------------------------------


class TestToFloat(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_to_float(None))

    def test_string_number(self):
        self.assertAlmostEqual(_to_float("3.14"), 3.14)

    def test_negative_string(self):
        self.assertAlmostEqual(_to_float("-50.25"), -50.25)

    def test_integer(self):
        self.assertAlmostEqual(_to_float(100), 100.0)

    def test_invalid_string_returns_none(self):
        self.assertIsNone(_to_float("not-a-number"))


# ---------------------------------------------------------------------------
# Tests: get_institution_name
# ---------------------------------------------------------------------------


class TestGetInstitutionName(unittest.TestCase):
    def test_returns_connection_name(self):
        provider = _provider_with_response(200, ACCOUNTS_PAYLOAD)
        name = provider.get_institution_name(ACCESS_URL)
        self.assertEqual(name, "My Test Bank - Alice")

    def test_falls_back_on_error(self):
        provider = _provider_with_response(500, b"error")
        name = provider.get_institution_name(ACCESS_URL)
        self.assertEqual(name, "SimpleFIN")

    def test_empty_connections_returns_simplefin(self):
        payload = dict(ACCOUNTS_PAYLOAD)
        payload["connections"] = []
        provider = _provider_with_response(200, payload)
        name = provider.get_institution_name(ACCESS_URL)
        self.assertEqual(name, "SimpleFIN")


if __name__ == "__main__":
    unittest.main()
