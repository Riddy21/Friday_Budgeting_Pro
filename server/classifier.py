"""
server/classifier.py — Tier 1 deterministic rules-based classifier.

Given a transaction, checks routing_rules for a matching merchant_pattern
and returns a ready-to-insert transaction_entry dict, or None if no rule fires.

This is the first tier in the three-tier classification pipeline:
  Tier 1 (this file): deterministic merchant-pattern rules
  Tier 2: LLM-based classification
  Tier 3: auto-promotion of confident results
"""

from __future__ import annotations

import sqlite3


def apply_rules(
    conn: sqlite3.Connection,
    transaction: dict | sqlite3.Row,
) -> dict | None:
    """Match *transaction* against routing_rules and return an entry dict.

    Args:
        conn: An open sqlite3 connection (row_factory need not be set).
        transaction: A dict or sqlite3.Row with keys:
            - id            (str)  transaction primary key
            - merchant      (str)  merchant name from the bank
            - amount        (real) transaction amount
            - bank_account_id (str)

    Returns:
        A transaction_entry dict ready for insertion if a rule matches::

            {
                "transaction_id": <str>,
                "ledger_id":      <str>,
                "line_item_id":   <str>,
                "amount":         <real>,
                "source":         "rule",
                "confidence":     1.0,
                "reviewed":       0,
            }

        None if no routing_rule matches.

    Matching semantics:
        merchant_pattern is compared case-insensitively as a substring of
        transaction.merchant.  The first rule (lowest id) that matches wins.
    """
    merchant: str = transaction["merchant"] or ""

    # Fetch all rules ordered by id so the "first" match is deterministic.
    cursor = conn.execute(
        "SELECT rr.id, rr.merchant_pattern, rr.line_item_id, li.ledger_id"
        "  FROM routing_rules rr"
        "  JOIN line_items li ON li.id = rr.line_item_id"
        " ORDER BY rr.id ASC",
    )

    for row in cursor:
        pattern: str = row[1] or ""
        if pattern.lower() in merchant.lower():
            return {
                "transaction_id": transaction["id"],
                "ledger_id": row[3],
                "line_item_id": row[2],
                "amount": transaction["amount"],
                "source": "rule",
                "confidence": 1.0,
                "reviewed": 0,
            }

    return None
