"""
server/providers/flinks.py — Flinks Connect/API implementation of BankProvider.

Flinks is a Canadian open-banking aggregator.  Unlike Plaid, connecting a bank
is a **hosted iframe** flow (Flinks Connect) that returns a permanent
``LoginId``; there is no ``public_token``/``access_token`` exchange.  We map the
Flinks flow onto the existing Plaid-shaped sync loop as follows:

  * ``create_link_token`` / ``exchange_public_token`` are unsupported (Flinks has
    no Link-token concept — the iframe is launched with a single-use
    *authorize token* instead).
  * ``generate_authorize_token`` + ``create_connect_url`` power the iframe.
  * ``authorize(login_id)`` → ``get_accounts_detail(request_id)`` pull the data,
    normalised into Plaid-shaped account/transaction dicts so the DB-upsert code
    in ``server.main.sync`` works unchanged.
  * ``sync_transactions(login_id, cursor)`` is the adapter the sync loop calls.

Flinks v3 API reference (docs.flinks.com)
-----------------------------------------
Auth + Connect flow:
  1. POST {api}/v3/{customerId}/BankingServices/GenerateAuthorizeToken
       header: flinks-auth-key: {secret_key}
       -> single-use authorize token (15-min TTL)
  2. Launch iframe {iframe}/v2/?authorizeToken={token}[&demo=true]
       -> user picks bank + MFA -> returns permanent loginId
  3. POST {api}/v3/{customerId}/BankingServices/Authorize
       header: flinks-auth-key: {authorize_token}
       body:   {"LoginId": "...", "MostRecentCached": true}
       -> requestId (session-scoped)
  4. POST {api}/v3/{customerId}/BankingServices/GetAccountsDetail
       header: x-api-key: {x_api_key}
       body:   {"RequestId": "...", "WithTransactions": true,
                "WithAccountIdentity": true, "DaysOfTransactions": "Days90"}
       -> 200 (ready) OR 202 (async — poll GetAccountsDetailAsync/{requestId})

Security note
-------------
This module never persists credentials or tokens.  Callers are responsible for
encrypting the LoginId (stored where Plaid keeps its access token) via
``server.crypto.encrypt`` before persisting.
"""

from __future__ import annotations

import json as _json
import os
import time as _time
import urllib.error
import urllib.parse
import urllib.request

from server.providers.base import BankProvider

_DEFAULT_INSTANCE = "toolbox"


class FlinksError(Exception):
    """Base class for Flinks provider errors."""


class FlinksNotConfiguredError(FlinksError):
    """Raised when the provider is used without complete credentials."""


class FlinksAuthError(FlinksError):
    """Raised on an HTTP 401 from Flinks (bad/expired credentials or login).

    Carries a ``.body`` attribute shaped like a Plaid error so the existing
    ``server.main.sync`` re-auth detection (``error_code == 'ITEM_LOGIN_REQUIRED'``)
    flips the connection to ``needs_reauth`` without Flinks-specific branching.
    """

    def __init__(self, message: str = "Flinks authentication failed (401)") -> None:
        super().__init__(message)
        self.body = _json.dumps(
            {
                "error_code": "ITEM_LOGIN_REQUIRED",
                "error_message": message,
            }
        )


class FlinksAPIError(FlinksError):
    """Raised on any non-401 HTTP error from Flinks."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Flinks API error {status}: {message}")
        self.status = status


class FlinksProvider(BankProvider):
    """Concrete Flinks Connect/API implementation of the BankProvider interface.

    Parameters
    ----------
    customer_id : str or None
        Flinks customer id (a UUID).  Falls back to ``FLINKS_CUSTOMER_ID``.
    secret_key : str or None
        Flinks secret key used to generate authorize tokens.  Falls back to
        ``FLINKS_SECRET_KEY``.
    x_api_key : str or None
        Flinks x-api-key used for the data endpoints (GetAccountsDetail).  Falls
        back to ``FLINKS_X_API_KEY``.
    instance : str
        Flinks instance/subdomain (e.g. ``'toolbox'`` for the public sandbox).
        Falls back to ``FLINKS_INSTANCE`` then ``'toolbox'``.
    timeout : float
        Per-request timeout in seconds.
    poll_interval : float
        Seconds to wait between GetAccountsDetailAsync polls (default 10).
    max_polls : int
        Maximum number of async polls before giving up (default 30 ≈ 5 min).
    """

    name = "flinks"

    def __init__(
        self,
        customer_id: str | None = None,
        secret_key: str | None = None,
        x_api_key: str | None = None,
        instance: str = _DEFAULT_INSTANCE,
        timeout: float = 60.0,
        poll_interval: float = 10.0,
        max_polls: int = 30,
    ) -> None:
        self._customer_id = customer_id or os.environ.get("FLINKS_CUSTOMER_ID")
        self._secret_key = secret_key or os.environ.get("FLINKS_SECRET_KEY")
        self._x_api_key = x_api_key or os.environ.get("FLINKS_X_API_KEY")
        self.instance = (
            instance or os.environ.get("FLINKS_INSTANCE") or _DEFAULT_INSTANCE
        )
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_polls = max_polls

        # Per-login raw GetAccountsDetail response cache so get_accounts /
        # get_transactions / sync_transactions share a single network fetch.
        self._detail_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Endpoint helpers
    # ------------------------------------------------------------------

    def _api_base(self) -> str:
        return f"https://{self.instance}-api.private.fin.ag"

    def _iframe_base(self) -> str:
        return f"https://{self.instance}-iframe.private.fin.ag"

    def _svc(self, path: str) -> str:
        """Full URL for a BankingServices sub-path."""
        return (
            f"{self._api_base()}/v3/{self._customer_id}/BankingServices/{path}"
        )

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Return True when customer id + secret key + x-api-key are all present."""
        return bool(self._customer_id and self._secret_key and self._x_api_key)

    def _require_config(self) -> None:
        if not self._customer_id:
            raise FlinksNotConfiguredError(
                "Flinks customer_id is not configured. Run configure_flinks() or "
                "set FLINKS_CUSTOMER_ID in the environment."
            )
        if not self._secret_key:
            raise FlinksNotConfiguredError(
                "Flinks secret_key is not configured. Run configure_flinks() or "
                "set FLINKS_SECRET_KEY in the environment."
            )
        if not self._x_api_key:
            raise FlinksNotConfiguredError(
                "Flinks x_api_key is not configured. Run configure_flinks() or "
                "set FLINKS_X_API_KEY in the environment."
            )

    # ------------------------------------------------------------------
    # Low-level HTTP — single seam so unit tests can mock one method.
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict]:
        """Perform an HTTP request and return ``(status_code, parsed_json)``.

        Uses only the stdlib so no runtime dependency is added.  Unit tests
        monkeypatch this method to inject canned responses.

        Raises
        ------
        FlinksAuthError
            On HTTP 401.
        FlinksAPIError
            On any other 4xx/5xx status.
        """
        req = urllib.request.Request(url, data=body, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                status = resp.getcode()
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            detail = ""
            try:
                detail = exc.read().decode("utf-8")
            except Exception:  # noqa: BLE001
                pass
            if exc.code == 401:
                raise FlinksAuthError(detail or "Flinks authentication failed (401)") from exc
            raise FlinksAPIError(exc.code, detail or exc.reason) from exc
        except urllib.error.URLError as exc:  # network/DNS failure
            raise FlinksAPIError(0, str(exc.reason)) from exc

        try:
            parsed = _json.loads(raw)
        except ValueError:
            parsed = {}
        return status, parsed

    def _post_json(
        self, url: str, headers: dict[str, str], payload: dict | None = None
    ) -> tuple[int, dict]:
        body = _json.dumps(payload or {}).encode("utf-8")
        hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
        hdrs.update(headers)
        return self._request("POST", url, headers=hdrs, body=body)

    def _sleep(self, seconds: float) -> None:
        """Sleep seam (overridden in tests to avoid real waits)."""
        _time.sleep(seconds)

    # ------------------------------------------------------------------
    # Flinks Connect flow
    # ------------------------------------------------------------------

    def generate_authorize_token(self) -> str:
        """Generate a single-use authorize token (15-min TTL) for the iframe.

        POST /v3/{customerId}/BankingServices/GenerateAuthorizeToken with the
        ``flinks-auth-key: {secret_key}`` header.
        """
        self._require_config()
        url = self._svc("GenerateAuthorizeToken")
        status, data = self._post_json(
            url, headers={"flinks-auth-key": self._secret_key or ""}
        )
        token = (
            data.get("AuthorizeToken")
            or data.get("authorizeToken")
            or data.get("Token")
            or data.get("token")
        )
        if not token:
            raise FlinksAPIError(
                status, f"GenerateAuthorizeToken returned no token: {data!r}"
            )
        return str(token)

    def create_connect_url(self, authorize_token: str) -> str:
        """Return the Flinks Connect iframe URL for *authorize_token*.

        The sandbox (``toolbox``) instance requires ``&demo=true``; we always
        add it for non-production instances so the test bank is available.
        """
        base = f"{self._iframe_base()}/v2/"
        params = {"authorizeToken": authorize_token}
        # Sandbox / toolbox needs demo=true to expose the Flinks Capital test bank.
        if self.instance != "production":
            params["demo"] = "true"
        return f"{base}?{urllib.parse.urlencode(params)}"

    def authorize(self, login_id: str) -> str:
        """Exchange a permanent *login_id* for a session-scoped ``requestId``.

        POST /v3/{customerId}/BankingServices/Authorize with a fresh authorize
        token in the ``flinks-auth-key`` header and ``{"LoginId": ...}`` body.
        """
        self._require_config()
        authorize_token = self.generate_authorize_token()
        url = self._svc("Authorize")
        status, data = self._post_json(
            url,
            headers={"flinks-auth-key": authorize_token},
            payload={"LoginId": login_id, "MostRecentCached": True},
        )
        request_id = (
            data.get("RequestId")
            or data.get("requestId")
            or data.get("Id")
        )
        if not request_id:
            raise FlinksAPIError(status, f"Authorize returned no RequestId: {data!r}")
        return str(request_id)

    def get_accounts_detail(self, request_id: str) -> dict:
        """Fetch full account + transaction detail for *request_id*.

        Handles Flinks' async pattern: a 202 means the data is still being
        assembled, so we poll ``GetAccountsDetailAsync/{requestId}`` every
        ``poll_interval`` seconds (up to ``max_polls``) until we get a 200.
        """
        self._require_config()
        url = self._svc("GetAccountsDetail")
        headers = {"x-api-key": self._x_api_key or ""}
        status, data = self._post_json(
            url,
            headers=headers,
            payload={
                "RequestId": request_id,
                "WithTransactions": True,
                "WithAccountIdentity": True,
                "DaysOfTransactions": "Days90",
            },
        )

        polls = 0
        while status == 202 and polls < self.max_polls:
            self._sleep(self.poll_interval)
            polls += 1
            async_url = f"{self._svc('GetAccountsDetailAsync')}/{urllib.parse.quote(str(request_id))}"
            status, data = self._request("GET", async_url, headers=headers)

        if status == 202:
            raise FlinksAPIError(
                202, f"GetAccountsDetail still pending after {self.max_polls} polls"
            )
        return data or {}

    # ------------------------------------------------------------------
    # Data pulls (with per-login caching)
    # ------------------------------------------------------------------

    def _fetch_detail(self, login_id: str, force: bool = False) -> dict:
        """Return (and cache) the raw GetAccountsDetail payload for *login_id*."""
        if not force and login_id in self._detail_cache:
            return self._detail_cache[login_id]
        request_id = self.authorize(login_id)
        detail = self.get_accounts_detail(request_id)
        self._detail_cache[login_id] = detail
        return detail

    def get_accounts(self, login_id: str) -> list[dict]:
        """Return the accounts for *login_id* in a Plaid-compatible shape."""
        detail = self._fetch_detail(login_id)
        raw_accounts = detail.get("Accounts") or []
        return [self._normalise_account(a) for a in raw_accounts]

    def get_transactions(
        self,
        login_id: str,
        account_id: str | None = None,
        since: str | None = None,
    ) -> list[dict]:
        """Return transactions for *login_id* in a Plaid-compatible shape.

        Parameters
        ----------
        account_id : str or None
            When provided, only transactions for that account are returned.
        since : str or None
            Optional ISO date (``YYYY-MM-DD``).  When provided, only
            transactions dated on/after this value are returned.
        """
        detail = self._fetch_detail(login_id)
        raw_accounts = detail.get("Accounts") or []
        out: list[dict] = []
        for acct in raw_accounts:
            acct_id = acct.get("Id")
            if account_id is not None and acct_id != account_id:
                continue
            for txn in acct.get("Transactions") or []:
                norm = self._normalise_transaction(txn, acct_id, acct)
                if since and norm.get("date") and norm["date"] < since:
                    continue
                out.append(norm)
        return out

    def test_connection(self, login_id: str) -> dict:
        """Validate credentials + login by pulling accounts.

        Returns ``{"ok": True, "accounts": <count>}`` on success.  Raises
        ``FlinksAuthError`` / ``FlinksAPIError`` / ``FlinksNotConfiguredError``
        otherwise so callers can surface a precise message.
        """
        accounts = self.get_accounts(login_id)
        return {"ok": True, "accounts": len(accounts)}

    # ------------------------------------------------------------------
    # Normalisation helpers (Flinks → Plaid-ish shapes)
    # ------------------------------------------------------------------

    @staticmethod
    def _get(obj, *keys, default=None):
        """Return the first present key from *keys* on a dict-like *obj*."""
        if not isinstance(obj, dict):
            return default
        for k in keys:
            if k in obj and obj[k] is not None:
                return obj[k]
        return default

    def _normalise_account(self, a: dict) -> dict:
        acct_id = a.get("Id")
        balance = a.get("Balance") if isinstance(a.get("Balance"), dict) else {}
        currency = a.get("Currency") or "CAD"
        acct_type = a.get("Type") or "depository"
        title = a.get("Title") or "Flinks Account"
        return {
            "account_id": acct_id,
            "name": title,
            "official_name": title,
            "type": acct_type,
            "subtype": (a.get("Category") or None),
            "mask": a.get("LastFourDigits"),
            "balances": {
                "current": _to_float(balance.get("Current")),
                "available": _to_float(balance.get("Available")),
                "iso_currency_code": currency,
            },
        }

    def _normalise_transaction(self, t: dict, account_id, account: dict) -> dict:
        # Flinks reports Debit (money out) and Credit (money in) as separate
        # positive fields.  Plaid convention: positive amount = money leaving
        # the account.  So amount = Debit - Credit.
        debit = _to_float(t.get("Debit")) or 0.0
        credit = _to_float(t.get("Credit")) or 0.0
        amount = debit - credit

        raw_date = t.get("Date")
        date_only = str(raw_date)[:10] if raw_date else None
        description = t.get("Description") or ""
        currency = (account.get("Currency") if isinstance(account, dict) else None) or "CAD"

        return {
            "transaction_id": t.get("Id"),
            "account_id": account_id,
            "date": date_only,
            "authorized_datetime": None,
            "name": description,
            "merchant_name": description,
            "amount": amount,
            "iso_currency_code": currency,
            # Flinks only returns posted (settled) transactions.
            "pending": False,
        }

    # ------------------------------------------------------------------
    # BankProvider interface
    # ------------------------------------------------------------------

    def create_link_token(self, user_id: str = "friday-bp-user") -> str:
        raise NotImplementedError(
            "Flinks has no Link-token concept. Use generate_authorize_token() + "
            "create_connect_url() to launch the Flinks Connect iframe."
        )

    def exchange_public_token(self, public_token: str) -> dict:
        raise NotImplementedError(
            "Flinks has no public_token exchange; the iframe returns a permanent "
            "LoginId which is stored directly on the connection row."
        )

    def sync_transactions(
        self, login_id: str | None = None, cursor: str | None = None
    ) -> dict:
        """Adapter that mimics Plaid's ``/transactions/sync`` shape.

        Flinks does not use opaque cursors, so ``cursor`` is interpreted as an
        ISO ``YYYY-MM-DD`` "fetch since" date (used to filter the 90-day window
        Flinks returns).  Returns a dict with ``added`` (all matching txns),
        ``modified`` / ``removed`` (always empty — Flinks gives a point-in-time
        snapshot of posted transactions), ``next_cursor`` (today's date) and
        ``accounts``.

        The permanent LoginId is passed as ``login_id``; for signature
        compatibility with the Plaid sync loop it may also arrive positionally
        as the first argument (the loop calls
        ``provider.sync_transactions(access_token, cursor)``).
        """
        if not login_id:
            raise FlinksNotConfiguredError("Flinks sync_transactions requires a login_id")
        self._require_config()

        # Force a fresh pull each sync (do not reuse a stale cache).
        self._fetch_detail(login_id, force=True)
        accounts = self.get_accounts(login_id)
        since = cursor  # cursor doubles as the fromDate on incremental syncs
        added = self.get_transactions(login_id, since=since)

        next_cursor = _time.strftime("%Y-%m-%d", _time.gmtime())
        return {
            "added": added,
            "modified": [],
            "removed": [],
            "next_cursor": next_cursor,
            "accounts": accounts,
        }

    def get_item_status(self, login_id: str | None = None) -> dict:
        """Return a Plaid-shaped status dict by probing the accounts endpoint.

        A successful probe yields ``error_code=None``; a 401 yields
        ``ITEM_LOGIN_REQUIRED`` so the health monitor moves the connection to
        ``needs_reauth`` exactly like an expired Plaid item.
        """
        try:
            if login_id:
                self.get_accounts(login_id)
            return {"error_code": None, "error_message": None, "item_id": self._customer_id}
        except FlinksAuthError as exc:
            return {
                "error_code": "ITEM_LOGIN_REQUIRED",
                "error_message": str(exc),
                "item_id": self._customer_id,
            }

    def get_institution_name(self, login_id: str | None = None) -> str:
        """Return the institution name from the cached detail, if available."""
        if login_id and login_id in self._detail_cache:
            inst = self._detail_cache[login_id].get("InstitutionName")
            if inst:
                return str(inst)
        return "Flinks"


def _to_float(value) -> float | None:
    """Best-effort float coercion that tolerates strings and None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
