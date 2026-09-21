# How to set up Flinks (Canadian banks)

Friday Budgeting Pro supports two bank connectors:

- **Plaid** — US banks and some Canadian institutions (hosted Plaid Link).
- **Flinks** — a Canadian open-banking aggregator with strong coverage of RBC,
  TD, BMO, Scotiabank, CIBC and many credit unions (hosted Flinks Connect
  iframe).

This guide covers Flinks. When you click **+ Connect a bank** in the UI you'll
see a chooser with both options.

---

## 1. Get your Flinks credentials

From your [Flinks dashboard](https://dashboard.flinks.io) you need four values:

| Field | What it is |
|---|---|
| **CustomerId** | Your Flinks customer UUID |
| **secret key** | Used to generate the single-use *authorize token* for the iframe (`flinks-auth-key` header) |
| **x-api-key** | Used for the data endpoints, e.g. `GetAccountsDetail` (`x-api-key` header) |
| **instance** | Your Flinks subdomain, e.g. your dashboard prefix; `toolbox` is the public sandbox |

### Try it first with the public sandbox

Flinks publishes a shared sandbox on the `toolbox` instance so you can exercise
the entire flow without a real bank or a paid account:

| Field | Value |
|---|---|
| instance | `toolbox` |
| CustomerId | `43387ca6-0391-4c82-857d-70d95f087ecb` |
| secret key | `c4569c54-e167-4d34-8de6-f4113bc82414` |
| x-api-key | `3d5266a8-b697-48d4-8de6-52e2e2662acc` |

The sandbox iframe automatically appends `&demo=true`, and the only available
test bank is **Flinks Capital**. Pick it, enter any demo login, and Friday will
import the sandbox accounts and transactions.

---

## 2. Configure the credentials

Ask your OpenClaw agent (this calls the `configure_flinks` MCP tool):

```
Set up my Flinks credentials — customer id is <CustomerId>,
secret key is <secret key>, x-api-key is <x-api-key>, instance is <instance>
```

Or, for the sandbox, simply:

```
Set up Flinks with the toolbox sandbox credentials
```

What happens:

- The credentials are saved to the per-user **`flinks_config`** table.
- They are **merged** into `.env` (`FLINKS_CUSTOMER_ID`, `FLINKS_SECRET_KEY`,
  `FLINKS_X_API_KEY`, `FLINKS_INSTANCE`). The merge preserves your existing
  Plaid keys and comments — nothing is clobbered, so Plaid and Flinks coexist.
- The running process picks them up immediately (no restart needed).

`.env` is written atomically with mode `0600`.

---

## 3. Connect a bank

1. Open the web UI (default `http://127.0.0.1:6789`) → **Accounts**.
2. Click **+ Connect a bank**.
3. On the chooser page, click **Flinks**.
4. The **Flinks Connect** iframe loads. Pick your institution (or **Flinks
   Capital** in the sandbox) and complete login / MFA.
5. On success, Flinks returns a permanent **LoginId**; Friday stores the
   connection and kicks off an initial sync in the background.
6. You're redirected to **Accounts?linked=1** and your accounts + transactions
   appear once the sync finishes.

---

## 4. How syncing works

- Each `sync` re-authorizes with the stored **LoginId** and pulls the latest
  **90 days** of *posted* transactions (`DaysOfTransactions=Days90`).
- Flinks may respond `202 Accepted` while it assembles the data; Friday polls
  `GetAccountsDetailAsync/{RequestId}` every 10s until it's ready.
- Flinks returns a point-in-time snapshot, so there are no incremental
  cursors — Friday de-duplicates against the DB by `transaction_id`.
- If Flinks returns **401** (expired/invalid login), the connection is flipped
  to **`needs_reauth`** and surfaced in the re-auth banner, exactly like an
  expired Plaid item.

### Sign convention

Flinks reports `Debit` (money out) and `Credit` (money in) as separate positive
fields. Friday normalises to Plaid's convention where **positive = money leaving
your account**:

```
amount = (Debit or 0) - (Credit or 0)
```

So a $5.50 debit becomes `+5.50` and a $2000 credit becomes `-2000.00`.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Chooser shows but Flinks page errors "not configured" | Run `configure_flinks` first (or set the `FLINKS_*` env vars). |
| Iframe loads but no banks listed | Sandbox needs `&demo=true` (added automatically for non-`production` instances) and only shows **Flinks Capital**. |
| Connection shows **needs reauth** | The LoginId expired — reconnect via **+ Connect a bank → Flinks**. |
| Wrong subdomain / 404 | Check your **instance** value; the API host is `https://{instance}-api.private.fin.ag`. |
