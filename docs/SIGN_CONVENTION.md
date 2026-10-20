# Amount Sign Convention

## Source of Truth

The **transactions.amount** field from Plaid/SimpleFin is the source of truth:

- **Positive amount** = money OUT (expense, debit, payment)
- **Negative amount** = money IN (income, refund, credit, deposit)

## Entry Storage

The **transaction_entries.amount** field preserves the original sign:

- **Positive** → spending/expense entry
- **Negative** → income/refund entry

The `entry_type` field indicates the classification:
- `spending` - expense (typically positive)
- `income` - income/refund (typically negative)
- `transfer` - internal transfer
- `savings` - savings contribution
- `skip` - excluded from reports

## Display

When displaying amounts:
- Use `ABS()` for totals and summaries (magnitude only)
- Preserve sign for individual transaction display (shows direction)
- Format negative as refunds/credits in the UI

## Rules

1. Never use `abs()` when storing entry amounts — preserve the source sign
2. Use `abs()` only for: comparisons, totals, display formatting
3. The classifier determines `entry_type` based on the sign and line_item type
