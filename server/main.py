"""
server/main.py — FastMCP server skeleton for Friday Budgeting Pro.

All tools are registered as stubs returning {'status': 'not_implemented'}.
Real implementations land in later tickets.
"""

from __future__ import annotations

from typing import List, Optional

import fastmcp

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
    """Return a URL to open Plaid Link."""
    return {"status": "not_implemented"}


@mcp.tool
def complete_link(public_token: str) -> dict:
    """Exchange a Plaid public token and store the access token."""
    return {"status": "not_implemented"}


@mcp.tool
def list_connections() -> dict:
    """List all saved Plaid bank connections."""
    return {"status": "not_implemented"}


@mcp.tool
def refresh_connection(id: str) -> dict:
    """Trigger an Update Mode Plaid Link for an existing connection."""
    return {"status": "not_implemented"}


@mcp.tool
def disconnect(id: str) -> dict:
    """Disconnect and remove a Plaid bank connection."""
    return {"status": "not_implemented"}


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
    """Query transactions with optional filters."""
    return {"status": "not_implemented"}


@mcp.tool
def get_needs_review() -> dict:
    """Return transactions that require manual classification review."""
    return {"status": "not_implemented"}


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
