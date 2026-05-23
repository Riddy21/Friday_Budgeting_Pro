"""
server/main.py — FastMCP server skeleton for Friday Budgeting Pro.

All tools are registered as stubs returning {'status': 'not_implemented'}.
Real implementations land in later tickets.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

import fastmcp

from server.db import get_db
import server.paths
import server.crypto
import server.plaid_client

mcp = fastmcp.FastMCP("friday-budgeting-pro")

# ---------------------------------------------------------------------------
# Setup tools
# ---------------------------------------------------------------------------


@mcp.tool
def setup_status() -> dict:
    """Return whether initial setup is not_started, in_progress, or complete."""
    return {"status": "not_implemented"}


@mcp.tool
def apply_initial_setup(
    banks_to_link: List,
    extra_ledgers: List,
    hints: List,
) -> dict:
    """Perform the whole first-run setup in one call."""
    return {"status": "not_implemented"}


# ---------------------------------------------------------------------------
# Banks tools
# ---------------------------------------------------------------------------


@mcp.tool
def start_link() -> dict:
    """Return a URL to open Plaid Link.

    Calls plaid_client.create_link_token() and returns a URL pointing at
    the (future) UI link page (served by #14).
    """
    link_token = server.plaid_client.create_link_token()
    return {"url": f"http://127.0.0.1:6789/link?token={link_token}"}


@mcp.tool
def complete_link(public_token: str) -> dict:
    """Exchange a Plaid public token and store the access token.

    Exchanges the public_token for a Plaid access_token + item_id, encrypts
    the access token via server.crypto, and inserts a new row into
    bank_connections.  Returns the new connection_id.

    institution_name is left NULL for now — fetching it requires
    Plaid /institutions/get_by_id which is out of scope; see issue #34.
    """
    result = server.plaid_client.exchange_public_token(public_token)
    access_token = result["access_token"]
    item_id = result["item_id"]

    encrypted_token = server.crypto.encrypt(access_token)
    connection_id = str(uuid.uuid4())

    conn = get_db(server.paths.DB_PATH)
    try:
        conn.execute(
            """
            INSERT INTO bank_connections
                (id, plaid_item_id, plaid_access_token_encrypted, status)
            VALUES (?, ?, ?, 'active')
            """,
            (connection_id, item_id, encrypted_token),
        )
        conn.commit()
    finally:
        conn.close()

    return {"connection_id": connection_id, "institution_name": None}


@mcp.tool
def list_connections() -> dict:
    """List all saved Plaid bank connections.

    Returns id, institution_name, status, and last_synced_at for each
    connection.  The encrypted access token is NEVER included in the output.
    """
    conn = get_db(server.paths.DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT id, institution_name, status, last_synced_at
            FROM bank_connections
            ORDER BY rowid
            """
        ).fetchall()
        connections = [dict(r) for r in rows]
    finally:
        conn.close()

    return {"connections": connections}


@mcp.tool
def refresh_connection(id: str) -> dict:
    """Trigger an Update Mode Plaid Link for an existing connection.

    Generates a new Plaid Link token for Update Mode.  The plaid-python SDK
    supports passing an access_token to create_link_token() for proper Update
    Mode, but our wrapper does not yet expose that parameter — see TODO below.

    TODO: Pass the decrypted access_token to create_link_token() for a true
    Update Mode link token (requires plaid_client.create_link_token to accept
    an optional access_token kwarg).  Tracked in issue #34.
    """
    # For now, generate a fresh link token (same as start_link)
    link_token = server.plaid_client.create_link_token()
    return {"url": f"http://127.0.0.1:6789/link?token={link_token}"}


@mcp.tool
def disconnect(id: str) -> dict:
    """Disconnect and remove a Plaid bank connection.

    Removes the connection row and any associated sync_cursor row from the
    local database.  Calling Plaid's /item/remove endpoint to revoke the
    access token on Plaid's side is out of scope for this PR (the local
    database record is the authoritative store for this app).
    """
    conn = get_db(server.paths.DB_PATH)
    try:
        conn.execute(
            "DELETE FROM sync_cursors WHERE connection_id = ?",
            (id,),
        )
        conn.execute(
            "DELETE FROM bank_connections WHERE id = ?",
            (id,),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True}


# ---------------------------------------------------------------------------
# Ledger tools
# ---------------------------------------------------------------------------


@mcp.tool
def list_ledgers() -> dict:
    """List all ledgers and their line items."""
    return {"status": "not_implemented"}


@mcp.tool
def add_line_item(ledger_id: str, name: str, item_type: str) -> dict:
    """Add a new line item to a ledger."""
    return {"status": "not_implemented"}


@mcp.tool
def add_ledger(name: str) -> dict:
    """Create a new ledger."""
    return {"status": "not_implemented"}


@mcp.tool
def remove_line_item(id: str) -> dict:
    """Remove a line item from a ledger."""
    return {"status": "not_implemented"}


# ---------------------------------------------------------------------------
# Transaction tools
# ---------------------------------------------------------------------------


@mcp.tool
def sync() -> dict:
    """Pull new transactions from Plaid, classify them, and return a summary."""
    return {"status": "not_implemented"}


@mcp.tool
def list(filters: Optional[dict] = None) -> dict:
    """Query transactions with optional filters.

    Supported filter keys (all optional):
      date_from    (str, ISO date, inclusive)
      date_to      (str, ISO date, inclusive)
      ledger_id    (str)
      line_item_id (str)
      reviewed     (bool)
      source       (str: "rule" | "llm" | "manual")
    """
    filters = filters or {}
    conditions: list[str] = []
    params: list = []

    if "date_from" in filters:
        conditions.append("t.date >= ?")
        params.append(filters["date_from"])
    if "date_to" in filters:
        conditions.append("t.date <= ?")
        params.append(filters["date_to"])
    if "ledger_id" in filters:
        conditions.append("te.ledger_id = ?")
        params.append(filters["ledger_id"])
    if "line_item_id" in filters:
        conditions.append("te.line_item_id = ?")
        params.append(filters["line_item_id"])
    if "reviewed" in filters:
        conditions.append("te.reviewed = ?")
        params.append(1 if filters["reviewed"] else 0)
    if "source" in filters:
        conditions.append("te.source = ?")
        params.append(filters["source"])

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = f"""
        SELECT
            te.id,
            te.transaction_id,
            te.ledger_id,
            te.line_item_id,
            te.amount,
            te.source,
            te.confidence,
            te.reviewed,
            t.date,
            t.merchant,
            t.amount AS transaction_amount
        FROM transaction_entries te
        JOIN transactions t ON t.id = te.transaction_id
        {where_clause}
        ORDER BY t.date DESC, te.id
    """

    conn = get_db(server.paths.DB_PATH)
    try:
        rows = conn.execute(sql, params).fetchall()
        entries = [dict(r) for r in rows]
    finally:
        conn.close()

    return {"entries": entries}


@mcp.tool
def get_needs_review() -> dict:
    """Return transactions that require manual classification review."""
    return list(filters={"reviewed": False})


@mcp.tool
def route(transaction_id: str, allocations: List) -> dict:
    """Manually route a transaction to one or more line items."""
    return {"status": "not_implemented"}


@mcp.tool
def add_hint(text: str) -> dict:
    """Save a natural-language classification hint."""
    return {"status": "not_implemented"}


# ---------------------------------------------------------------------------
# Report tools
# ---------------------------------------------------------------------------


@mcp.tool
def summary(period: str) -> dict:
    """Return spending totals for a given period."""
    return {"status": "not_implemented"}


@mcp.tool
def export_excel(years: Optional[List] = None) -> dict:
    """Generate and return an Excel export of transactions."""
    return {"status": "not_implemented"}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
