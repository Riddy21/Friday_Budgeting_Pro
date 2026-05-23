"""
Thin wrapper around the plaid-python SDK for Friday Budgeting Pro.

Exposes three operations:
    create_link_token   — generate a Link token for the frontend
    exchange_public_token — exchange a public token for an access token + item_id
    sync_transactions   — incremental transaction sync via the Transactions Sync API

Environment variables
---------------------
PLAID_ENV        : sandbox | development | production  (default: sandbox)
PLAID_CLIENT_ID  : Plaid client ID (required)
PLAID_SECRET     : Plaid secret for the configured environment (required)

Security note
-------------
Callers MUST encrypt tokens via server.crypto.encrypt before persisting.
This module never touches encryption or the database; it only communicates
with the Plaid API using plaintext tokens supplied by the caller.
"""

import os

import plaid
from plaid.api import plaid_api
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.country_code import CountryCode
from plaid.model.products import Products
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest

# Use URL strings directly so we don't depend on which Environment constants
# the installed plaid-python version exposes (>= 14 dropped Development).
_ENV_MAP: dict[str, str] = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}

_APP_NAME = "Friday Budgeting Pro"


def _build_client() -> plaid_api.PlaidApi:
    """Instantiate a PlaidApi client from environment variables."""
    raw_env = os.environ.get("PLAID_ENV", "sandbox").lower()
    if raw_env not in _ENV_MAP:
        raise ValueError(
            f"Invalid PLAID_ENV '{raw_env}'. Must be one of: "
            + ", ".join(_ENV_MAP)
        )

    client_id = os.environ.get("PLAID_CLIENT_ID")
    if not client_id:
        raise EnvironmentError(
            "PLAID_CLIENT_ID environment variable is not set. "
            "Set it to your Plaid client ID before starting the daemon."
        )

    secret = os.environ.get("PLAID_SECRET")
    if not secret:
        raise EnvironmentError(
            "PLAID_SECRET environment variable is not set. "
            "Set it to the Plaid secret for your environment before starting the daemon."
        )

    configuration = plaid.Configuration(
        host=_ENV_MAP[raw_env],
        api_key={
            "clientId": client_id,
            "secret": secret,
        },
    )
    api_client = plaid.ApiClient(configuration)
    return plaid_api.PlaidApi(api_client)


def create_link_token(user_id: str = "friday-bp-user") -> str:
    """
    Create a Plaid Link token for the given *user_id*.

    Returns the ``link_token`` string that the frontend passes to Plaid Link.

    Callers MUST encrypt tokens via server.crypto.encrypt before persisting.
    """
    client = _build_client()
    request = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=user_id),
        client_name=_APP_NAME,
        products=[Products("transactions")],
        country_codes=[CountryCode("US"), CountryCode("CA")],
        language="en",
    )
    response = client.link_token_create(request)
    return response["link_token"]


def exchange_public_token(public_token: str) -> dict:
    """
    Exchange a public token from Plaid Link for a persistent access token.

    Returns a dict with keys:
        ``access_token`` — the Plaid access token (plaintext; encrypt before persisting)
        ``item_id``       — the Plaid item ID

    Callers MUST encrypt tokens via server.crypto.encrypt before persisting.
    """
    client = _build_client()
    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)
    return {
        "access_token": response["access_token"],
        "item_id": response["item_id"],
    }


def sync_transactions(access_token: str, cursor: str | None = None) -> dict:
    """
    Incrementally sync transactions for the item associated with *access_token*.

    Parameters
    ----------
    access_token : str
        Plaintext Plaid access token.  Caller is responsible for decrypting
        before passing here; see server.crypto.decrypt.
    cursor : str or None
        Pagination cursor from a previous call.  Pass ``None`` for the initial
        fetch (returns all historical transactions).

    Returns a dict with keys:
        ``added``       — list of added transaction objects
        ``modified``    — list of modified transaction objects
        ``removed``     — list of removed transaction objects
        ``next_cursor`` — cursor string for the next incremental call

    Callers MUST encrypt tokens via server.crypto.encrypt before persisting.
    """
    client = _build_client()
    kwargs: dict = {"access_token": access_token}
    if cursor is not None:
        kwargs["cursor"] = cursor

    request = TransactionsSyncRequest(**kwargs)
    response = client.transactions_sync(request)
    return {
        "added": response["added"],
        "modified": response["modified"],
        "removed": response["removed"],
        "next_cursor": response["next_cursor"],
    }
