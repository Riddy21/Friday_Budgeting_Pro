"""
server/main.py — FastMCP server skeleton for Friday Budgeting Pro.

All tools are registered as stubs returning {'status': 'not_implemented'}.
Real implementations land in later tickets.
"""

from __future__ import annotations

import json as _json
import time as _time
import uuid
from typing import List, Optional

import logging
import os
import tempfile
from pathlib import Path

import fastmcp

import server.excel_export as excel_export

from server.db import get_db, transaction as db_txn
from server.sync_lock import sync_lock, LockBusy
from server.classifier import apply_rules
import server.paths
import server.crypto
from server.providers.plaid import PlaidProvider
import server.health_monitor

_plaid = PlaidProvider()

mcp = fastmcp.FastMCP("friday-budgeting-pro")

_logger = logging.getLogger(__name__)

# Project root — tests monkeypatch this to a tmp dir so .env writes stay isolated.
project_root: Path = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Setup tools
# ---------------------------------------------------------------------------


@mcp.tool
def setup_status() -> dict:
    """Return whether initial setup is not_started, in_progress, or complete.

    Status rules:
      - "not_started"  → 0 ledgers AND 0 bank_connections
      - "in_progress"  → ≥1 ledger AND 0 bank_connections
                         (user picked a ledger but hasn't linked a bank yet)
      - "complete"     → ≥1 ledger AND ≥1 bank_connection
    """
    conn = get_db(server.paths.DB_PATH)
    try:
        ledger_count = conn.execute("SELECT COUNT(*) FROM ledgers").fetchone()[0]
        bank_count = conn.execute("SELECT COUNT(*) FROM bank_connections").fetchone()[0]
    finally:
        conn.close()

    if ledger_count == 0 and bank_count == 0:
        status = "not_started"
    elif ledger_count >= 1 and bank_count == 0:
        status = "in_progress"
    else:
        status = "complete"

    return {"status": status}


@mcp.tool
def apply_initial_setup(
    banks_to_link: List,
    extra_ledgers: List,
    hints: List,
) -> dict:
    """Perform the whole first-run setup in one call.

    Parameters
    ----------
    banks_to_link : List[str]
        Human-readable bank names the user wants to connect.  NOTE: This tool
        does NOT run Plaid Link — that interactive flow lives in start_link /
        complete_link.  We simply acknowledge the requested banks and return
        them so the caller can chain start_link calls for each one.
    extra_ledgers : List[dict]
        Additional ledgers beyond "Personal".  Each entry is a dict like::

            {"name": "Business", "line_items": [{"name": "Office", "type": "expense"}, ...]}

        The built-in "Personal" ledger is always created (with the standard
        10 line items below) regardless of this parameter.
    hints : List[str]
        Natural-language classification hints; each becomes a row in
        ``classification_hints``.  De-duped on exact text.

    Standard Personal line items (always created):
      Salary (income), Groceries (expense), Dining (expense),
      Transport (expense), Subscriptions (expense), Healthcare (expense),
      Travel (expense), Shopping (expense), Misc (expense), Other (expense)

    Idempotent: re-running will not duplicate ledgers, line items, or hints.

    Returns
    -------
    dict
        {"status": "ok", "ledgers_created": [...], "line_items_created": N,
         "hints_created": N, "banks_to_link": [...]}
    """
    PERSONAL_LINE_ITEMS = [
        ("Salary", "income"),
        ("Groceries", "expense"),
        ("Dining", "expense"),
        ("Transport", "expense"),
        ("Subscriptions", "expense"),
        ("Healthcare", "expense"),
        ("Travel", "expense"),
        ("Shopping", "expense"),
        ("Misc", "expense"),
        ("Other", "expense"),
    ]

    # Build the full ledger spec: Personal first, then any extras.
    ledger_specs = [{"name": "Personal", "line_items": PERSONAL_LINE_ITEMS}]
    for el in (extra_ledgers or []):
        items = [(li["name"], li["type"]) for li in el.get("line_items", [])]
        ledger_specs.append({"name": el["name"], "line_items": items})

    ledgers_created: list[str] = []
    line_items_created = 0
    hints_created = 0

    conn = get_db(server.paths.DB_PATH)
    try:
        with db_txn(conn):
            for spec in ledger_specs:
                ledger_name = spec["name"]

                # Upsert ledger — skip if already present.
                existing_ledger = conn.execute(
                    "SELECT id FROM ledgers WHERE name = ?", (ledger_name,)
                ).fetchone()
                if existing_ledger:
                    ledger_id = existing_ledger["id"]
                else:
                    ledger_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO ledgers (id, name) VALUES (?, ?)",
                        (ledger_id, ledger_name),
                    )
                    ledgers_created.append(ledger_name)

                # Upsert line items — skip if name+type already present in this ledger.
                for item_name, item_type in spec["line_items"]:
                    existing_item = conn.execute(
                        "SELECT id FROM line_items "
                        "WHERE ledger_id = ? AND name = ? AND item_type = ?",
                        (ledger_id, item_name, item_type),
                    ).fetchone()
                    if existing_item:
                        continue
                    conn.execute(
                        "INSERT INTO line_items (id, ledger_id, name, item_type) "
                        "VALUES (?, ?, ?, ?)",
                        (str(uuid.uuid4()), ledger_id, item_name, item_type),
                    )
                    line_items_created += 1

            # Upsert hints — de-dupe on exact text.
            for hint_text in (hints or []):
                existing_hint = conn.execute(
                    "SELECT id FROM classification_hints WHERE text = ?",
                    (hint_text,),
                ).fetchone()
                if existing_hint:
                    continue
                conn.execute(
                    "INSERT INTO classification_hints (id, text) VALUES (?, ?)",
                    (str(uuid.uuid4()), hint_text),
                )
                hints_created += 1
    finally:
        conn.close()

    return {
        "status": "ok",
        "ledgers_created": ledgers_created,
        "line_items_created": line_items_created,
        "hints_created": hints_created,
        "banks_to_link": [*banks_to_link] if banks_to_link else [],
    }


# ---------------------------------------------------------------------------
# Banks tools
# ---------------------------------------------------------------------------


@mcp.tool
def start_link() -> dict:
    """Return a URL to open Plaid Link.

    Calls PlaidProvider.create_link_token() and returns a URL pointing at
    the (future) UI link page (served by #14).
    """
    link_token = _plaid.create_link_token()
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
    result = _plaid.exchange_public_token(public_token)
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
    link_token = _plaid.create_link_token()
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

    def _get(obj, key, default=None):
        """Get a field from a dict or an SDK object."""
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def _is_reauth_error(exc: Exception) -> bool:
        """Return True when *exc* signals ITEM_LOGIN_REQUIRED."""
        body = getattr(exc, "body", None)
        if body:
            try:
                parsed = _json.loads(body)
                return parsed.get("error_code") == "ITEM_LOGIN_REQUIRED"
            except Exception:
                pass
        return False

    connections_synced = 0
    total_added = 0
    total_modified = 0
    total_removed = 0
    total_classified = 0

    health_check_result: dict = {}

    try:
        with sync_lock(timeout=0.0):
            db_conn = get_db(server.paths.DB_PATH)
            try:
                # Run health check first so stale/expired connections are
                # updated before we attempt to sync them.
                health_check_result = server.health_monitor.check_all_connections(
                    db_conn, plaid_provider=_plaid
                )

                active_conns = db_conn.execute(
                    "SELECT id, plaid_access_token_encrypted "
                    "FROM bank_connections WHERE status = 'active'"
                ).fetchall()

                for bc in active_conns:
                    connection_id = bc["id"]
                    encrypted_token = bc["plaid_access_token_encrypted"]
                    access_token = server.crypto.decrypt(encrypted_token)

                    cursor_row = db_conn.execute(
                        "SELECT cursor FROM sync_cursors WHERE connection_id = ?",
                        (connection_id,),
                    ).fetchone()
                    cursor = cursor_row["cursor"] if cursor_row else None

                    try:
                        result = _plaid.sync_transactions(access_token, cursor)
                    except Exception as e:
                        if _is_reauth_error(e):
                            with db_txn(db_conn):
                                db_conn.execute(
                                    "UPDATE bank_connections SET status='needs_reauth' WHERE id=?",
                                    (connection_id,),
                                )
                            continue
                        raise

                    added_txns = result.get("added", []) if isinstance(result, dict) else []
                    modified_txns = result.get("modified", []) if isinstance(result, dict) else []
                    removed_txns = result.get("removed", []) if isinstance(result, dict) else []
                    next_cursor = result.get("next_cursor") if isinstance(result, dict) else None

                    now = int(_time.time())
                    conn_added = 0
                    conn_modified = 0
                    conn_removed = 0
                    conn_classified = 0

                    with db_txn(db_conn):
                        # --- Added transactions ---
                        for txn in added_txns:
                            plaid_account_id = _get(txn, "account_id")
                            plaid_txn_id = _get(txn, "transaction_id")
                            date = _get(txn, "date")
                            name = _get(txn, "name") or ""
                            merchant_name = _get(txn, "merchant_name") or ""
                            merchant = merchant_name if merchant_name else name
                            amount = _get(txn, "amount")
                            pending = bool(_get(txn, "pending", False))

                            # Upsert bank_account (INSERT OR IGNORE on plaid_account_id UNIQUE)
                            db_conn.execute(
                                "INSERT OR IGNORE INTO bank_accounts "
                                "(id, connection_id, plaid_account_id) VALUES (?, ?, ?)",
                                (str(uuid.uuid4()), connection_id, plaid_account_id),
                            )
                            ba_row = db_conn.execute(
                                "SELECT id FROM bank_accounts WHERE plaid_account_id = ?",
                                (plaid_account_id,),
                            ).fetchone()
                            bank_account_id = ba_row["id"]

                            txn_id = str(uuid.uuid4())
                            cur = db_conn.execute(
                                "INSERT OR IGNORE INTO transactions "
                                "(id, bank_account_id, plaid_transaction_id, date, merchant, amount, pending) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (
                                    txn_id,
                                    bank_account_id,
                                    plaid_txn_id,
                                    str(date) if date is not None else None,
                                    merchant,
                                    amount,
                                    1 if pending else 0,
                                ),
                            )

                            if cur.rowcount > 0:
                                # Newly inserted — count and attempt Tier-1 classification
                                conn_added += 1
                                txn_dict = {
                                    "id": txn_id,
                                    "merchant": merchant,
                                    "amount": amount,
                                    "bank_account_id": bank_account_id,
                                }
                                entry = apply_rules(db_conn, txn_dict)
                                if entry is not None:
                                    db_conn.execute(
                                        "INSERT OR IGNORE INTO transaction_entries "
                                        "(id, transaction_id, ledger_id, line_item_id, "
                                        " amount, source, confidence, reviewed) "
                                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                        (
                                            str(uuid.uuid4()),
                                            entry["transaction_id"],
                                            entry["ledger_id"],
                                            entry["line_item_id"],
                                            entry["amount"],
                                            entry["source"],
                                            entry["confidence"],
                                            entry["reviewed"],
                                        ),
                                    )
                                    conn_classified += 1

                        # --- Modified transactions ---
                        for txn in modified_txns:
                            plaid_txn_id = _get(txn, "transaction_id")
                            date = _get(txn, "date")
                            name = _get(txn, "name") or ""
                            merchant_name = _get(txn, "merchant_name") or ""
                            merchant = merchant_name if merchant_name else name
                            amount = _get(txn, "amount")
                            pending = bool(_get(txn, "pending", False))
                            db_conn.execute(
                                "UPDATE transactions "
                                "SET date=?, merchant=?, amount=?, pending=? "
                                "WHERE plaid_transaction_id=?",
                                (
                                    str(date) if date is not None else None,
                                    merchant,
                                    amount,
                                    1 if pending else 0,
                                    plaid_txn_id,
                                ),
                            )
                            conn_modified += 1

                        # --- Removed transactions ---
                        for txn in removed_txns:
                            plaid_txn_id = _get(txn, "transaction_id")
                            db_conn.execute(
                                "DELETE FROM transactions WHERE plaid_transaction_id = ?",
                                (plaid_txn_id,),
                            )
                            conn_removed += 1

                        # Upsert cursor — advances ONLY on full success of this batch
                        db_conn.execute(
                            "INSERT INTO sync_cursors (connection_id, cursor, last_synced_at) "
                            "VALUES (?, ?, ?) "
                            "ON CONFLICT(connection_id) DO UPDATE SET "
                            "    cursor = excluded.cursor, "
                            "    last_synced_at = excluded.last_synced_at",
                            (connection_id, next_cursor, now),
                        )

                        db_conn.execute(
                            "UPDATE bank_connections SET last_synced_at=? WHERE id=?",
                            (now, connection_id),
                        )

                    connections_synced += 1
                    total_added += conn_added
                    total_modified += conn_modified
                    total_removed += conn_removed
                    total_classified += conn_classified

            finally:
                db_conn.close()

    except LockBusy:
        return {"status": "already_running"}

    return {
        "status": "ok",
        "connections_synced": connections_synced,
        "added": total_added,
        "modified": total_modified,
        "removed": total_removed,
        "classified_by_rule": total_classified,
        "health_check": health_check_result,
    }


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
    cleaned = text.strip()
    if len(cleaned) < 1:
        raise ValueError("hint text must be non-empty")
    conn = get_db(server.paths.DB_PATH)
    try:
        existing = conn.execute(
            "SELECT id FROM classification_hints WHERE text = ?",
            (cleaned,),
        ).fetchone()
        if existing:
            return {"id": existing["id"], "text": cleaned, "created": False}
        new_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO classification_hints (id, text) VALUES (?, ?)",
            (new_id, cleaned),
        )
        conn.commit()
        return {"id": new_id, "text": cleaned, "created": True}
    finally:
        conn.close()


@mcp.tool
def list_hints() -> dict:
    """Return all classification hints."""
    conn = get_db(server.paths.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, text FROM classification_hints ORDER BY rowid"
        ).fetchall()
        return {"hints": [{"id": row["id"], "text": row["text"]} for row in rows]}
    finally:
        conn.close()


@mcp.tool
def remove_hint(id: str) -> dict:
    """Delete a classification hint by id."""
    conn = get_db(server.paths.DB_PATH)
    try:
        cursor = conn.execute(
            "DELETE FROM classification_hints WHERE id = ?",
            (id,),
        )
        conn.commit()
        return {"ok": True, "removed": cursor.rowcount > 0}
    finally:
        conn.close()


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
    server.paths.ensure_app_dir()
    import datetime
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"friday-bp-{timestamp}.xlsx"
    path = server.paths.EXPORTS_DIR / filename
    conn = get_db(server.paths.DB_PATH)
    try:
        excel_export.export_to_file(conn, path, years if years else None)
    finally:
        conn.close()
    return {"status": "ok", "path": str(path), "size_bytes": path.stat().st_size}


# ---------------------------------------------------------------------------
# Auto-promoted rules audit tools
# ---------------------------------------------------------------------------


@mcp.tool
def list_auto_promoted_rules() -> dict:
    """Return all auto-promoted routing rules with their audit log metadata.

    Each entry includes a ``rule_still_active`` boolean indicating whether the
    underlying ``routing_rule`` still exists in the database.
    """
    conn = get_db(server.paths.DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT apl.id,
                   apl.rule_id,
                   apl.merchant,
                   apl.line_item_id,
                   apl.source_transaction_ids,
                   apl.created_at,
                   CASE WHEN rr.id IS NOT NULL THEN 1 ELSE 0 END AS rule_still_active
              FROM auto_promoted_rules_log apl
              LEFT JOIN routing_rules rr ON rr.id = apl.rule_id
             ORDER BY apl.created_at DESC
            """
        ).fetchall()
        return {
            "rules": [
                {
                    "id":                     row["id"],
                    "rule_id":                row["rule_id"],
                    "merchant":               row["merchant"],
                    "line_item_id":           row["line_item_id"],
                    "source_transaction_ids": _json.loads(row["source_transaction_ids"]),
                    "created_at":             row["created_at"],
                    "rule_still_active":      bool(row["rule_still_active"]),
                }
                for row in rows
            ]
        }
    finally:
        conn.close()


@mcp.tool
def undo_auto_promoted_rule(rule_id: str) -> dict:
    """Delete an auto-promoted routing rule and revert affected transaction entries.

    - Deletes the ``routing_rule`` (CASCADE removes the ``auto_promoted_rules_log`` row).
    - Resets ``reviewed = 0`` on every ``transaction_entry`` whose
      ``source = 'rule'`` and whose transaction's merchant matches the rule's
      ``merchant_pattern``.
    - The entire operation is wrapped in a single transaction for atomicity.

    Returns::

        {"ok": True, "rule_deleted": bool, "entries_reverted": int}
    """
    conn = get_db(server.paths.DB_PATH)
    try:
        # Look up the rule before deleting so we know the merchant_pattern.
        rule_row = conn.execute(
            "SELECT id, merchant_pattern FROM routing_rules WHERE id = ?",
            (rule_id,),
        ).fetchone()

        if rule_row is None:
            return {"ok": True, "rule_deleted": False, "entries_reverted": 0}

        merchant_pattern: str = rule_row["merchant_pattern"]

        with db_txn(conn):
            # Revert affected transaction_entries (source='rule', merchant matches).
            cursor = conn.execute(
                """
                UPDATE transaction_entries
                   SET reviewed = 0
                 WHERE source = 'rule'
                   AND transaction_id IN (
                       SELECT id FROM transactions WHERE merchant = ?
                   )
                """,
                (merchant_pattern,),
            )
            entries_reverted: int = cursor.rowcount

            # Delete the routing rule (CASCADE deletes the log row too).
            conn.execute(
                "DELETE FROM routing_rules WHERE id = ?",
                (rule_id,),
            )

        return {"ok": True, "rule_deleted": True, "entries_reverted": entries_reverted}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Configuration tools
# ---------------------------------------------------------------------------


@mcp.tool
def configure_plaid(
    client_id: str,
    secret: str,
    env: str = "production",
) -> dict:
    """Write Plaid credentials to .env and update the running process environment.

    Parameters
    ----------
    client_id : str
        Plaid client ID (non-empty).
    secret : str
        Plaid secret for the target environment (non-empty).
    env : str
        One of ``sandbox``, ``development``, or ``production``.
        Defaults to ``production``.

    The .env file is written atomically (temp file + os.replace) with mode
    0o600.  If .env already exists it is fully replaced, not appended.
    os.environ is updated immediately so the next sync() call picks up the
    new credentials without a daemon restart.

    Returns
    -------
    dict
        ``{"ok": True, "env": <env>}``
    """
    _VALID_ENVS = {"sandbox", "development", "production"}

    if not client_id:
        raise ValueError("client_id must be non-empty")
    if not secret:
        raise ValueError("secret must be non-empty")
    if env not in _VALID_ENVS:
        raise ValueError(
            f"env must be one of {sorted(_VALID_ENVS)!r}, got {env!r}"
        )

    env_path = project_root / ".env"
    content = (
        f"PLAID_CLIENT_ID={client_id}\n"
        f"PLAID_SECRET={secret}\n"
        f"PLAID_ENV={env}\n"
    )

    # Atomic write: write to a sibling temp file, then os.replace into place.
    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(dir=env_path.parent, prefix=".env.tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except OSError:
            pass
        raise

    os.replace(tmp_path_str, env_path)
    os.chmod(env_path, 0o600)

    # Update the running process so the next sync() call picks up new creds.
    os.environ["PLAID_CLIENT_ID"] = client_id
    os.environ["PLAID_SECRET"] = secret
    os.environ["PLAID_ENV"] = env

    _logger.info("configure_plaid: wrote .env (env=%s)", env)

    return {"ok": True, "env": env}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
