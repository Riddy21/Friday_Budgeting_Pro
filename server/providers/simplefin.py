"""
server/providers/simplefin.py — SimpleFIN Bridge implementation of BankProvider.

SimpleFIN Bridge is an open banking aggregator that lets users share read-only
financial data with third-party apps.  Unlike Plaid, SimpleFIN has no
per-app API keys: the "Access URL" (obtained once per user-bank connection)
contains embedded Basic-Auth credentials.

Flow
----
1. Direct the user to https://bridge.simplefin.org/simplefin/create to generate
   a Setup Token.  The user connects their bank there.
2. The user returns to the app and pastes the Setup Token.
3. The app calls ``claim_setup_token(setup_token)`` **once** to exchange the
   Setup Token for a permanent Access URL
   (e.g. ``https://user123:password@bridge.simplefin.org/simplefin``).
4. The app stores the Access URL **encrypted**.  This is the only credential
   needed going forward.
5. The app calls ``sync_transactions(access_url, cursor)`` periodically to
   fetch transactions.  ``cursor`` is a ``YYYY-MM-DD`` start date; defaults to
   90 days ago on the first call.

Security notes
--------------
* The Access URL embeds username:password — treat it with the same care as a
  Plaid access token (encrypt at rest via ``server.crypto.encrypt``).
* Only HTTPS Access URLs are accepted (enforced in ``claim_setup_token``).
* Access tokens are revoked by the user via the SimpleFIN Bridge UI at
  https://bridge.simplefin.org — no programmatic revocation exists.

SimpleFIN spec:      https://www.simplefin.org/protocol.html
Developer guide:     https://bridge.simplefin.org/info/developers
"""

from __future__ import annotations

import base64
import datetime as _datetime
import json as _json
import os
import urllib.error
import urllib.parse
import urllib.request

from server.providers.base import BankProvider

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SimpleFINError(Exception):
    """Base class for SimpleFIN provider errors."""


class SimpleFINAuthError(SimpleFINError):
    """Raised on a 403 from SimpleFIN (bad/expired Access URL or Setup Token).

    Carries a ``.body`` attribute shaped like a Plaid error so the existing
    health-monitor re-auth detection (``error_code == 'ITEM_LOGIN_REQUIRED'``)
    works without SimpleFIN-specific branching.
    """

    def __init__(self, message: str = "SimpleFIN authentication failed (403)") -> None:
        super().__init__(message)
        self.body = _json.dumps(
            {
                "error_code": "ITEM_LOGIN_REQUIRED",
                "error_message": message,
            }
        )


class SimpleFINAPIError(SimpleFINError):
    """Raised on any non-403 HTTP error from SimpleFIN."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"SimpleFIN API error {status}: {message}")
        self.status = status


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class SimpleFINProvider(BankProvider):
    """Concrete SimpleFIN Bridge implementation of the BankProvider interface.

    SimpleFIN does not use per-app API keys.  Each Access URL is self-contained:
    Basic-Auth credentials are embedded directly in the URL.  The provider
    therefore takes no credential constructor arguments; all sensitive material
    is passed through the ``access_url`` argument of ``sync_transactions`` /
    ``get_item_status``.

    Parameters
    ----------
    timeout : float
        Per-request timeout in seconds (default 30).
    overlap_days : int
        Days to subtract from *cursor* when fetching to ensure no transactions
        are missed at boundary dates (default 5, per SimpleFIN developer guide).
    """

    name = "simplefin"

    # Default max days of history to fetch on the first sync (or on a malformed
    # cursor). 90 keeps the initial backfill small enough to classify quickly.
    DEFAULT_MAX_DAYS = 90

    def __init__(
        self,
        timeout: float = 30.0,
        overlap_days: int = 5,
        max_days: int | None = None,
    ) -> None:
        self.timeout = timeout
        self.overlap_days = overlap_days
        # Adjustable first-sync window. Priority: explicit arg > env var >
        # DEFAULT_MAX_DAYS (90). Not hard-capped so a deliberate larger pull is
        # possible, but server.main warns + batches when it exceeds the default.
        if max_days is None:
            try:
                max_days = int(
                    os.environ.get("FRIDAY_BP_SYNC_MAX_DAYS", str(self.DEFAULT_MAX_DAYS))
                )
            except (TypeError, ValueError):
                max_days = self.DEFAULT_MAX_DAYS
        self.max_days = max(1, max_days)

    # ------------------------------------------------------------------
    # Low-level HTTP — single seam so unit tests can patch ``_request``.
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> tuple[int, bytes]:
        """Perform an HTTP request and return ``(status_code, raw_bytes)``.

        Uses only the stdlib (no third-party dependencies).  Unit tests
        monkeypatch this method to inject canned responses.

        Raises
        ------
        SimpleFINAuthError
            On HTTP 403.
        SimpleFINAPIError
            On any other 4xx/5xx or network failure.
        """
        req = urllib.request.Request(url, data=body, method=method)
        # Use a browser-like User-Agent; SimpleFIN Bridge (hosted on Cloudflare)
        # blocks the default Python-urllib agent with error 1010.
        req.add_header(
            "User-Agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if username is not None and password is not None:
            # Inject Basic-Auth explicitly rather than relying on urllib's URL
            # parser, which can misbehave with special characters in passwords.
            cred = base64.b64encode(f"{username}:{password}".encode()).decode()
            req.add_header("Authorization", f"Basic {cred}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.getcode(), resp.read()
        except urllib.error.HTTPError as exc:
            detail = b""
            try:
                detail = exc.read()
            except Exception:  # noqa: BLE001
                pass
            decoded = detail.decode("utf-8", errors="replace")
            if exc.code == 403:
                raise SimpleFINAuthError(decoded or "SimpleFIN auth failed (403)") from exc
            raise SimpleFINAPIError(exc.code, decoded or exc.reason) from exc
        except urllib.error.URLError as exc:
            raise SimpleFINAPIError(0, str(exc.reason)) from exc

    # ------------------------------------------------------------------
    # Setup Token → Access URL
    # ------------------------------------------------------------------

    def claim_setup_token(self, setup_token: str) -> str:
        """Exchange a *setup_token* for a permanent Access URL (one-time operation).

        The Setup Token is a Base64-encoded claim URL.  This method decodes it
        and issues one HTTP POST to claim the Access URL.

        .. warning::
            This can only be done **once** per Setup Token.  The caller MUST
            encrypt and persist the returned Access URL before discarding the
            token.

        Parameters
        ----------
        setup_token : str
            The Setup Token pasted by the user from
            https://bridge.simplefin.org/simplefin/create

        Returns
        -------
        str
            The permanent Access URL, e.g.
            ``https://user:pass@bridge.simplefin.org/simplefin``.

        Raises
        ------
        ValueError
            If ``setup_token`` is empty, not valid Base64, or the decoded URL
            is not HTTPS.
        SimpleFINAuthError
            If the claim URL returns 403 (token already claimed or expired).
        SimpleFINAPIError
            On any other HTTP error.
        """
        if not setup_token or not setup_token.strip():
            raise ValueError("setup_token must be non-empty")

        try:
            claim_url = base64.b64decode(setup_token.strip()).decode("utf-8").strip()
        except Exception as exc:
            raise ValueError(f"setup_token is not valid Base64: {exc}") from exc

        if not claim_url.startswith("https://"):
            raise ValueError(
                f"Decoded claim URL does not start with https://: {claim_url!r}. "
                "Only HTTPS claim URLs are accepted."
            )

        # POST with empty body (Content-Length: 0 is required by the spec).
        _, raw = self._request(
            "POST",
            claim_url,
            headers={"Content-Length": "0"},
            body=b"",
        )
        access_url = raw.decode("utf-8").strip()
        if not access_url.startswith("https://"):
            raise SimpleFINAPIError(
                0,
                f"Unexpected claim response (expected HTTPS Access URL): {access_url!r}",
            )
        return access_url

    # ------------------------------------------------------------------
    # Access URL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_access_url(access_url: str) -> tuple[str, str, str]:
        """Decompose *access_url* into ``(accounts_url, username, password)``.

        Example::

            "https://alice:s3cr3t@bridge.simplefin.org/simplefin"
            → ("https://bridge.simplefin.org/simplefin/accounts", "alice", "s3cr3t")
        """
        parsed = urllib.parse.urlparse(access_url)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError(f"Invalid SimpleFIN Access URL: {access_url!r}")
        username = urllib.parse.unquote(parsed.username or "")
        password = urllib.parse.unquote(parsed.password or "")
        # Rebuild without credentials, append /accounts.
        base = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            base += f":{parsed.port}"
        path = parsed.path.rstrip("/")
        accounts_url = f"{base}{path}/accounts"
        return accounts_url, username, password

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_accounts(
        self,
        access_url: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        """GET /accounts and return the parsed JSON response.

        Parameters
        ----------
        start_date : str or None
            ISO date ``YYYY-MM-DD``; converted to a midnight-UTC Unix timestamp
            and passed as the ``start-date`` query parameter.
        end_date : str or None
            ISO date ``YYYY-MM-DD``; converted to a midnight-UTC Unix timestamp
            and passed as the ``end-date`` query parameter.
        """
        accounts_url, username, password = self._parse_access_url(access_url)
        params: dict[str, str] = {"version": "2"}
        if start_date:
            params["start-date"] = str(
                int(
                    _datetime.datetime.strptime(start_date, "%Y-%m-%d")
                    .replace(tzinfo=_datetime.timezone.utc)
                    .timestamp()
                )
            )
        if end_date:
            params["end-date"] = str(
                int(
                    _datetime.datetime.strptime(end_date, "%Y-%m-%d")
                    .replace(tzinfo=_datetime.timezone.utc)
                    .timestamp()
                )
            )
        full_url = f"{accounts_url}?{urllib.parse.urlencode(params)}"
        _, raw = self._request("GET", full_url, username=username, password=password)
        return _json.loads(raw.decode("utf-8"))

    # ------------------------------------------------------------------
    # Normalisation helpers (SimpleFIN → Plaid-compatible shapes)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_account(acct: dict, conn_name: str | None = None) -> dict:
        """Map a SimpleFIN Account object to a Plaid-compatible dict.

        SimpleFIN does not expose account type/subtype or last-4 digits, so
        those fields default to generic values.

        The ``org`` sub-object on each SimpleFIN account (``org.name``,
        ``org.domain``) identifies which institution/bank owns the account.
        We surface this as ``owner_label`` so the sync loop can store it in
        ``bank_accounts.owner_label`` and the UI can separate accounts by
        owner (e.g. "Ridvan" vs "Dollar" — see RIDVAN_REQUESTS.md item 3).
        Fallback chain: org.name → org.domain → conn_name → None.
        """
        currency = acct.get("currency", "USD")
        balance_str = acct.get("balance") or "0"
        avail_str = acct.get("available-balance") or acct.get("balance") or "0"
        acct_name = acct.get("name") or "SimpleFIN Account"

        # --- Owner label: derived from the per-account ``org`` object ---
        # SimpleFIN spec: each account carries an ``org`` dict with fields
        # ``name`` (display name of the org/institution) and ``domain``
        # (canonical domain, e.g. "rbcroyalbank.com").  We prefer the
        # human-readable ``name``; fall back to ``domain`` then ``conn_name``.
        org = acct.get("org") or {}
        owner_label = org.get("name") or org.get("domain") or conn_name or None

        return {
            "account_id": acct.get("id"),
            "name": acct_name,
            "official_name": acct_name,
            "type": "depository",  # SimpleFIN doesn't expose account type; we
            # infer the real type later via infer_account_type()
            # in server.main (see RIDVAN_REQUESTS.md item 5).
            "subtype": None,
            "mask": None,  # SimpleFIN doesn't expose last-4 digits
            "balances": {
                "current": _to_float(balance_str),
                "available": _to_float(avail_str),
                "iso_currency_code": currency,
            },
            # Non-Plaid extras: used by the sync loop in server.main.
            "_conn_name": conn_name,  # institution/connection display name
            "owner_label": owner_label,  # org-level owner label for UI separation
        }

    @staticmethod
    def _normalise_transaction(txn: dict, account_id: str, currency: str) -> dict:
        """Map a SimpleFIN Transaction to a Plaid-compatible dict.

        SimpleFIN ``amount`` is a numeric string where **negative = debit**
        (money leaving the account).  Plaid convention: **positive = money
        leaving the account**.  We invert the sign.
        """
        sfin_amount = _to_float(txn.get("amount", "0")) or 0.0
        plaid_amount = -sfin_amount  # invert: Plaid positive = expense/debit

        posted_ts = txn.get("posted")
        date_only: str | None = None
        if posted_ts is not None:
            try:
                dt = _datetime.datetime.fromtimestamp(int(posted_ts), tz=_datetime.timezone.utc)
                date_only = dt.strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                pass

        description = txn.get("description") or ""
        return {
            "transaction_id": txn.get("id"),
            "account_id": account_id,
            "date": date_only,
            "authorized_datetime": None,
            "name": description,
            "merchant_name": description,
            "amount": plaid_amount,
            "iso_currency_code": currency,
            "pending": False,  # SimpleFIN only returns posted transactions
        }

    # ------------------------------------------------------------------
    # BankProvider interface
    # ------------------------------------------------------------------

    def create_link_token(self, user_id: str = "friday-bp-user") -> str:
        raise NotImplementedError(
            "SimpleFIN has no Link-token concept.  Direct the user to "
            "https://bridge.simplefin.org/simplefin/create to obtain a Setup Token, "
            "then call claim_setup_token() to exchange it for an Access URL."
        )

    def exchange_public_token(self, public_token: str) -> dict:
        raise NotImplementedError(
            "SimpleFIN has no public_token exchange.  Call claim_setup_token() "
            "with the Setup Token from the SimpleFIN Bridge."
        )

    def sync_transactions(self, access_url: str, cursor: str | None = None) -> dict:
        """Fetch accounts and transactions using the SimpleFIN /accounts endpoint.

        Maps the SimpleFIN response into the Plaid-shaped dict expected by the
        sync loop in ``server.main``.

        Parameters
        ----------
        access_url : str
            Plaintext SimpleFIN Access URL.  The caller is responsible for
            decrypting before passing here (see ``server.crypto.decrypt``).
        cursor : str or None
            Start date as ``YYYY-MM-DD`` from the previous sync.  Pass ``None``
            for the initial fetch (defaults to 90 days ago).

        Returns
        -------
        dict
            Plaid-shaped dict with keys ``added``, ``modified``, ``removed``,
            ``next_cursor`` (today's ISO date), and ``accounts``.
        """
        today = _datetime.date.today()
        end_date_str = today.isoformat()

        if cursor:
            # Overlap by ``overlap_days`` to catch any transactions that post
            # slightly after the day they occurred (per SimpleFIN dev guide).
            try:
                cursor_date = _datetime.date.fromisoformat(cursor)
                start_date = cursor_date - _datetime.timedelta(days=self.overlap_days)
                start_date_str = start_date.isoformat()
            except ValueError:
                # Malformed cursor: fall back to the configured history window.
                start_date_str = (today - _datetime.timedelta(days=self.max_days)).isoformat()
        else:
            # First sync: fetch up to ``self.max_days`` of history (default 90).
            start_date_str = (today - _datetime.timedelta(days=self.max_days)).isoformat()

        data = self._fetch_accounts(access_url, start_date=start_date_str, end_date=end_date_str)

        # Build a conn_id → connection name map for institution_name resolution.
        conn_names: dict[str, str] = {}
        for conn in data.get("connections") or []:
            conn_names[conn.get("conn_id", "")] = conn.get("name", "SimpleFIN")

        raw_accounts = data.get("accounts") or []
        accounts: list[dict] = []
        added: list[dict] = []

        for acct in raw_accounts:
            acct_id = acct.get("id")
            currency = acct.get("currency", "USD")
            conn_id = acct.get("conn_id", "")
            conn_name = conn_names.get(conn_id)
            accounts.append(self._normalise_account(acct, conn_name=conn_name))
            for txn in acct.get("transactions") or []:
                added.append(self._normalise_transaction(txn, acct_id, currency))

        return {
            "added": added,
            "modified": [],
            "removed": [],
            "next_cursor": end_date_str,
            "accounts": accounts,
        }

    def get_item_status(self, access_url: str) -> dict:
        """Probe /accounts to check whether *access_url* is still valid.

        Returns a Plaid-shaped status dict so the health monitor works without
        SimpleFIN-specific branching.
        """
        try:
            self._fetch_accounts(access_url)
            return {"error_code": None, "error_message": None, "item_id": None}
        except SimpleFINAuthError as exc:
            return {
                "error_code": "ITEM_LOGIN_REQUIRED",
                "error_message": str(exc),
                "item_id": None,
            }
        except SimpleFINError as exc:
            return {
                "error_code": "PROVIDER_ERROR",
                "error_message": str(exc),
                "item_id": None,
            }

    def get_all_connections_status(self, access_url: str) -> list[dict]:
        """Check status of all SimpleFIN connections and return per-connection info.

        SimpleFIN returns a list of "connections" (institutions) in the /accounts
        response. Each connection can independently need re-auth. This method
        fetches the accounts and checks each connection for issues.

        Returns
        -------
        list[dict]
            List of connection status dicts:
            [
                {
                    "name": "Royal Bank of Canada",
                    "id": "connection-uuid",
                    "status": "ok" | "auth_required" | "error",
                    "error_message": str | None,
                    "account_count": int,
                },
                ...
            ]
        """
        try:
            data = self._fetch_accounts(access_url)
        except SimpleFINAuthError:
            # Entire access URL is invalid — all connections need re-auth
            return [
                {
                    "name": "SimpleFIN",
                    "id": None,
                    "status": "auth_required",
                    "error_message": "SimpleFIN access token expired or invalid",
                    "account_count": 0,
                }
            ]
        except SimpleFINError as exc:
            return [
                {
                    "name": "SimpleFIN",
                    "id": None,
                    "status": "error",
                    "error_message": str(exc),
                    "account_count": 0,
                }
            ]

        connections = data.get("connections") or []
        result = []

        for conn in connections:
            conn_name = conn.get("name") or conn.get("org", {}).get("name") or "Unknown"
            conn_id = conn.get("id")
            accounts = conn.get("accounts") or []

            # Check for error indicators in the connection
            # SimpleFIN may include error info when a connection has issues
            conn_error = conn.get("error")
            if conn_error:
                error_code = conn_error.get("code", "")
                error_msg = conn_error.get("message", str(conn_error))
                # Common SimpleFIN error codes for auth issues
                if error_code in ("auth_required", "login_required", "credentials_invalid"):
                    status = "auth_required"
                else:
                    status = "error"
                result.append(
                    {
                        "name": conn_name,
                        "id": conn_id,
                        "status": status,
                        "error_message": error_msg,
                        "account_count": len(accounts),
                    }
                )
            else:
                # No error — connection is healthy
                result.append(
                    {
                        "name": conn_name,
                        "id": conn_id,
                        "status": "ok",
                        "error_message": None,
                        "account_count": len(accounts),
                    }
                )

        return result

    def remove_item(self, access_url: str) -> dict:
        """SimpleFIN does not support programmatic revocation.

        Users revoke access tokens from https://bridge.simplefin.org (sign in →
        Apps tab).

        Returns a dict indicating revocation is not supported so callers can
        still proceed with local cleanup.
        """
        return {
            "revoked": False,
            "reason": (
                "SimpleFIN access tokens must be revoked via the SimpleFIN Bridge UI at "
                "https://bridge.simplefin.org — sign in and visit the 'Apps' tab."
            ),
        }

    def get_institution_name(self, access_url: str) -> str:
        """Return the connection name from the first SimpleFIN connection."""
        try:
            data = self._fetch_accounts(access_url)
            conns = data.get("connections") or []
            if conns:
                return conns[0].get("name") or "SimpleFIN"
        except Exception:  # noqa: BLE001
            pass
        return "SimpleFIN"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_float(value) -> float | None:
    """Best-effort float coercion that tolerates strings and None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
