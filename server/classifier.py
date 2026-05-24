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

import json
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


# ---------------------------------------------------------------------------
# Tier 2 — LLM-based classifier
# ---------------------------------------------------------------------------


def classify_with_llm(
    conn: sqlite3.Connection,
    transaction: dict | sqlite3.Row,
) -> dict:
    """Classify *transaction* using the LLM and return an entry dict.

    Builds rich context from the DB (ledger tree, classification hints, recent
    similar transactions) and asks the LLM to pick the best line_item.

    Args:
        conn: An open sqlite3 connection with row_factory = sqlite3.Row.
        transaction: A dict or sqlite3.Row with at minimum:
            - id        (str)
            - merchant  (str)
            - amount    (real)
            - date      (str, ISO format)

    Returns:
        A transaction_entry dict::

            {
                "transaction_id": <str>,
                "ledger_id":      <str>,
                "line_item_id":   <str>,
                "amount":         <real>,
                "source":         "llm",
                "confidence":     <float>,
                "reviewed":       0,
            }

    Raises:
        ValueError: If the LLM response is not valid JSON, is missing required
            fields, references a line_item_id that does not exist in the DB, or
            returns a ledger_id that does not match the ledger_id looked up from
            the line_item in the DB (ledger_id mismatch).
    """
    from server.llm import chat  # local import keeps chat patchable in tests

    # ------------------------------------------------------------------
    # 1. Build context: full ledger tree
    # ------------------------------------------------------------------
    ledgers_rows = conn.execute(
        "SELECT id, name FROM ledgers ORDER BY name"
    ).fetchall()

    line_items_rows = conn.execute(
        "SELECT li.id, li.name, li.ledger_id, l.name AS ledger_name"
        "  FROM line_items li"
        "  JOIN ledgers l ON l.id = li.ledger_id"
        " ORDER BY l.name, li.name"
    ).fetchall()

    # Group line_items by ledger for a readable tree
    ledger_tree_lines: list[str] = []
    for ledger_row in ledgers_rows:
        ledger_tree_lines.append(f"  Ledger: {ledger_row['name']} (id={ledger_row['id']})")
        for li in line_items_rows:
            if li["ledger_id"] == ledger_row["id"]:
                ledger_tree_lines.append(f"    - {li['name']} (id={li['id']})")

    ledger_tree_text = "\n".join(ledger_tree_lines)

    # ------------------------------------------------------------------
    # 2. Build context: classification hints
    # ------------------------------------------------------------------
    hints_rows = conn.execute(
        "SELECT text FROM classification_hints ORDER BY id"
    ).fetchall()
    hints_text = "\n".join(f"  - {r['text']}" for r in hints_rows)
    if not hints_text:
        hints_text = "  (none)"

    # ------------------------------------------------------------------
    # 3. Build context: account description (if present)
    # ------------------------------------------------------------------
    account_description: str | None = None
    if isinstance(transaction, dict):
        bank_account_id = transaction.get("bank_account_id")
    else:
        cols = [d[0] for d in transaction.description] if hasattr(transaction, "description") else []
        bank_account_id = transaction["bank_account_id"] if "bank_account_id" in cols else None
    if bank_account_id:
        acct_row = conn.execute(
            "SELECT description FROM bank_accounts WHERE id = ?",
            (bank_account_id,),
        ).fetchone()
        if acct_row and acct_row["description"]:
            account_description = acct_row["description"]

    # ------------------------------------------------------------------
    # 4. Build context: 5 most recent reviewed entries with same merchant
    # ------------------------------------------------------------------
    merchant: str = transaction["merchant"] or ""
    recent_rows = conn.execute(
        "SELECT t.merchant, t.amount, t.date, te.line_item_id, li.name AS li_name,"
        "       l.name AS ledger_name"
        "  FROM transaction_entries te"
        "  JOIN transactions t  ON t.id  = te.transaction_id"
        "  JOIN line_items   li ON li.id = te.line_item_id"
        "  JOIN ledgers      l  ON l.id  = te.ledger_id"
        " WHERE te.reviewed = 1"
        "   AND t.merchant = ?"
        " ORDER BY t.date DESC"
        " LIMIT 5",
        (merchant,),
    ).fetchall()

    if recent_rows:
        recent_lines = [
            f"  - {r['date']} | {r['merchant']} | ${r['amount']:.2f}"
            f" → {r['ledger_name']} / {r['li_name']} (id={r['line_item_id']})"
            for r in recent_rows
        ]
        recent_text = "\n".join(recent_lines)
    else:
        recent_text = "  (none)"

    # ------------------------------------------------------------------
    # 5. Compose the prompt
    # ------------------------------------------------------------------
    system_msg = (
        "You are a personal finance classifier. Your job is to classify a bank "
        "transaction into exactly ONE line item from the user's ledger. "
        "Reply with ONLY a JSON object — no markdown, no explanation outside the JSON — "
        "with these keys:\n"
        '  {"line_item_id": "<exact id>", "confidence": <0.0-1.0>, "reasoning": "<short>"}\n'
        "Choose the line_item_id that best fits the transaction based on the ledger tree, "
        "the user's hints, and any recent similar transactions."
    )

    account_context_section = (
        f"## Account Context\n  {account_description}\n\n"
        if account_description
        else ""
    )

    user_msg = (
        f"## Ledger Tree\n{ledger_tree_text}\n\n"
        f"## Classification Hints\n{hints_text}\n\n"
        f"## Recent Similar Transactions (reviewed)\n{recent_text}\n\n"
        f"{account_context_section}"
        f"## Transaction to Classify\n"
        f"  Merchant : {merchant}\n"
        f"  Amount   : ${transaction['amount']:.2f}\n"
        f"  Date     : {transaction.get('date', 'unknown')}\n\n"
        "Reply with the JSON object only."
    )

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user",   "content": user_msg},
    ]

    # ------------------------------------------------------------------
    # 5. Call the LLM and parse the response
    # ------------------------------------------------------------------
    raw = chat(messages, temperature=0.0)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LLM returned non-JSON response: {raw!r}"
        ) from exc

    if "line_item_id" not in parsed:
        raise ValueError(
            f"LLM JSON missing 'line_item_id' key: {parsed!r}"
        )

    line_item_id: str = parsed["line_item_id"]
    confidence: float = float(parsed.get("confidence", 0.0))

    # ------------------------------------------------------------------
    # 6. Resolve ledger_id from the DB (validates line_item_id exists)
    # ------------------------------------------------------------------
    li_row = conn.execute(
        "SELECT ledger_id FROM line_items WHERE id = ?",
        (line_item_id,),
    ).fetchone()

    if li_row is None:
        raise ValueError(
            f"LLM returned unknown line_item_id={line_item_id!r} which does not exist in the DB."
        )

    ledger_id: str = li_row["ledger_id"]

    # ------------------------------------------------------------------
    # 7. Validate ledger_id consistency if the LLM also returned one
    # ------------------------------------------------------------------
    if "ledger_id" in parsed:
        llm_ledger_id = parsed["ledger_id"]
        if llm_ledger_id != ledger_id:
            raise ValueError(
                f"LLM returned ledger_id={llm_ledger_id!r} but line_item_id={line_item_id!r}"
                f" belongs to ledger_id={ledger_id!r} — ledger_id mismatch."
            )

    return {
        "transaction_id": transaction["id"],
        "ledger_id":      ledger_id,
        "line_item_id":   line_item_id,
        "amount":         transaction["amount"],
        "source":         "llm",
        "confidence":     confidence,
        "reviewed":       0,
    }


# ---------------------------------------------------------------------------
# Tier 2b — safe wrapper with fallback-to-review
# ---------------------------------------------------------------------------


def safe_classify(
    conn: sqlite3.Connection,
    transaction: dict | sqlite3.Row,
    fallback_to_review: bool = True,
) -> dict:
    """Classify *transaction* with graceful degradation on LLM validation failures.

    Wraps :func:`classify_with_llm` and intercepts ``ValueError`` raised by
    any validation step (bad JSON, missing ``line_item_id`` key, unknown
    ``line_item_id``, or ``ledger_id`` mismatch).

    Args:
        conn: An open sqlite3 connection with ``row_factory = sqlite3.Row``.
        transaction: A dict or sqlite3.Row with at minimum:
            - id        (str)
            - merchant  (str)
            - amount    (real)
            - date      (str, ISO format)
        fallback_to_review: When ``True`` (default) a validation failure
            returns a stub entry flagged for human review instead of raising.
            When ``False`` the ``ValueError`` is re-raised.

    Returns:
        On success: the entry dict returned by :func:`classify_with_llm`
        (possibly with ``source`` updated by a downstream
        :func:`flag_for_review` call if you use that separately).

        On validation failure with *fallback_to_review=True*::

            {
                "transaction_id":  <str>,
                "ledger_id":       None,
                "line_item_id":    None,
                "amount":          <real>,
                "source":          "llm-rejected",
                "confidence":      0.0,
                "reviewed":        0,
                "rejection_reason": "<short description of what went wrong>",
            }

    Raises:
        ValueError: Only when *fallback_to_review=False* and the LLM output
            fails validation.
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        return classify_with_llm(conn, transaction)
    except ValueError as exc:
        reason = str(exc)
        logger.warning(
            "safe_classify: LLM output rejected for transaction_id=%r — %s",
            transaction["id"],
            reason,
        )
        if not fallback_to_review:
            raise
        return {
            "transaction_id":   transaction["id"],
            "ledger_id":        None,
            "line_item_id":     None,
            "amount":           transaction["amount"],
            "source":           "llm-rejected",
            "confidence":       0.0,
            "reviewed":         0,
            "rejection_reason": reason,
        }


# ---------------------------------------------------------------------------
# Tier 3 — review-flag + auto-promotion
# ---------------------------------------------------------------------------


def flag_for_review(
    transaction: dict,
    llm_result: dict,
    threshold: float = 0.75,
) -> dict:
    """Tag low-confidence LLM results as needing human review.

    This is a **pure function** — it performs no database reads or writes.

    Args:
        transaction: The raw transaction dict (used for context, not mutated).
        llm_result:  The entry dict produced by :func:`classify_with_llm`.
            Expected keys: ``transaction_id``, ``ledger_id``,
            ``line_item_id``, ``amount``, ``source``, ``confidence``,
            ``reviewed``.
        threshold:   Minimum confidence (inclusive) required for the result to
            pass through unchanged.  Defaults to **0.75**.  Callers can
            override — e.g. ``flag_for_review(txn, res, threshold=0.9)``
            for a stricter policy.

    Returns:
        A copy of *llm_result* with ``source`` set to ``"llm-needs-review"``
        when ``confidence < threshold``, otherwise *llm_result* unchanged.
        ``reviewed`` is always ``0`` on the returned entry.
    """
    if llm_result["confidence"] < threshold:
        flagged = dict(llm_result)
        flagged["source"] = "llm-needs-review"
        return flagged
    return llm_result


def maybe_promote_to_rule(
    conn: sqlite3.Connection,
    transaction: dict | sqlite3.Row,
    llm_result: dict,
) -> dict | None:
    """Auto-create a Tier-1 routing rule after 3+ consistent reviewed entries.

    Looks at recent ``transaction_entries`` for the same merchant that have
    ``source IN ('llm', 'manual')`` and ``reviewed = 1``.  If at least **3**
    of them share the same ``line_item_id`` as *llm_result*, a new
    ``routing_rule`` is inserted.

    **Idempotent** — if a rule with the same
    ``(merchant_pattern, line_item_id)`` pair already exists, the existing
    rule is returned and no duplicate is created.

    Args:
        conn:        An open sqlite3 connection.
        transaction: A dict or sqlite3.Row with at least a ``merchant`` key.
        llm_result:  The entry dict produced by :func:`classify_with_llm`
            (or :func:`flag_for_review`).  Must contain ``line_item_id``.

    Returns:
        A dict ``{"id": …, "merchant_pattern": …, "line_item_id": …}``
        for the new (or pre-existing) rule, or ``None`` if the promotion
        threshold was not met.
    """
    import uuid

    merchant: str = transaction["merchant"] or ""
    target_line_item_id: str = llm_result["line_item_id"]

    # ------------------------------------------------------------------
    # 1. Count consistent reviewed entries for this merchant + line_item
    # ------------------------------------------------------------------
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
          FROM transaction_entries te
          JOIN transactions t ON t.id = te.transaction_id
         WHERE t.merchant = ?
           AND te.source IN ('llm', 'manual')
           AND te.reviewed = 1
           AND te.line_item_id = ?
        """,
        (merchant, target_line_item_id),
    ).fetchone()

    consistent_count: int = row[0] if row else 0

    if consistent_count < 3:
        return None

    # ------------------------------------------------------------------
    # 2. Idempotency check — return existing rule if one already exists
    # ------------------------------------------------------------------
    existing = conn.execute(
        """
        SELECT id, merchant_pattern, line_item_id
          FROM routing_rules
         WHERE merchant_pattern = ?
           AND line_item_id = ?
        """,
        (merchant, target_line_item_id),
    ).fetchone()

    if existing is not None:
        # Do NOT insert another log row for an already-existing rule.
        return {
            "id":               existing[0],
            "merchant_pattern": existing[1],
            "line_item_id":     existing[2],
        }

    # ------------------------------------------------------------------
    # 3. Collect the transaction ids that contributed to the promotion
    #    (the 3+ consistent reviewed entries for this merchant + line_item)
    # ------------------------------------------------------------------
    import time

    source_rows = conn.execute(
        """
        SELECT t.id AS txn_id
          FROM transaction_entries te
          JOIN transactions t ON t.id = te.transaction_id
         WHERE t.merchant = ?
           AND te.source IN ('llm', 'manual')
           AND te.reviewed = 1
           AND te.line_item_id = ?
        ORDER BY t.date DESC
        """,
        (merchant, target_line_item_id),
    ).fetchall()
    source_transaction_ids: list[str] = [r[0] for r in source_rows]

    # ------------------------------------------------------------------
    # 4. Create the new routing rule and write the audit log row atomically
    # ------------------------------------------------------------------
    new_id = str(uuid.uuid4())
    log_id = str(uuid.uuid4())
    created_at = int(time.time())

    conn.execute(
        """
        INSERT INTO routing_rules (id, merchant_pattern, line_item_id)
        VALUES (?, ?, ?)
        """,
        (new_id, merchant, target_line_item_id),
    )
    conn.execute(
        """
        INSERT INTO auto_promoted_rules_log
               (id, rule_id, merchant, line_item_id, source_transaction_ids, created_at)
        VALUES (?,  ?,       ?,        ?,            ?,                      ?)
        """,
        (
            log_id,
            new_id,
            merchant,
            target_line_item_id,
            json.dumps(source_transaction_ids),
            created_at,
        ),
    )
    conn.commit()

    return {
        "id":               new_id,
        "merchant_pattern": merchant,
        "line_item_id":     target_line_item_id,
    }
