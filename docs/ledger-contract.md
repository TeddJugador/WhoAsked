# FinancialLedgerAPI: contract for the finance service

What the finance service's ledger adapter can rely on, read from the ledger's source
code rather than its README. The ledger is not modified; where its behaviour is awkward,
the adapter works around it, and the workaround is stated here.

**Source:** <https://github.com/TeddJugador/FinancialLedgerAPI>, commit `dc92889`
(2026-08-03). Files read: `src/lambdas/ingestion.py`, `src/lambdas/common/signature.py`,
`src/lambdas/cdc_archiver.py`, `scripts/generate_hmac.py`, `stacks/ledger_stack.py`,
`tests/unit/test_ingestion.py`, `README.md`. (The finance spec names
`src/lambdas/common/signature.py`; it exists, under `src/lambdas/common/`.)

Line references below are to that commit.

## 1. Endpoint

| | |
|---|---|
| Method and path | `POST /v1/webhooks/payment` (`ledger_stack.py` 229–233) |
| API | HTTP API named `financial-ledger-api`, `$default` stage, auto-deploy |
| Throttling | 500 requests/s, burst 1000 (stage-wide, 248) |
| Timeout | the Lambda times out after 10 s (131); API Gateway's own limit is 30 s |
| Outputs | **none**: the stack defines no `CfnOutput`. The base URL is found with `aws apigatewayv2 get-apis --query "Items[?Name=='financial-ledger-api'].ApiEndpoint"` and passed to the finance stack through `cdk.json` context. |

## 2. Authentication (`common/signature.py`, `ingestion.py` 36–63, 154–174)

- **Algorithm:** HMAC-SHA256 over the **exact raw request body bytes**, hex-encoded
  (lowercase, 64 characters).
- **Key:** the secret `ledger/webhook-signing-secret`. Its `SecretString` is JSON; the
  key is the `signing_key` field (`{"key": "", "signing_key": "<48 alphanumerics>"}`,
  `ledger_stack.py` 88–99). The key is used as UTF-8 bytes.
- **Header:** `X-Signature-256` (matched case-insensitively). `stripe-signature` is also
  accepted; the adapter uses `X-Signature-256` only. A `sha256=` prefix is accepted and
  stripped; the adapter sends the bare hex digest.
- **Comparison:** `hmac.compare_digest` (constant time).
- **Base64 bodies:** decoded before verification when API Gateway sets `isBase64Encoded`.
  The adapter sends `application/json`, which API Gateway passes as text.
- **Key caching:** the key is cached in a module global for the life of a warm container
  (33–47). After a rotation, warm ledger containers keep accepting only the old key until
  they recycle, so a rotation needs both keys briefly accepted, which the ledger doesn't
  support. Rotation is therefore a planned, coordinated step (out of scope for v1).
- **Signing must use the same bytes that are sent.** The adapter serialises once
  (`json.dumps(payload, separators=(",", ":"))`, as `generate_hmac.py` does), signs those
  bytes, and sends those bytes.

## 3. Request payload (`ingestion.py` 72–78, 127–146)

A JSON object. Required fields (missing any → 400):

| Field | Type the ledger expects | Notes |
|---|---|---|
| `event_id` | string | **The deduplication key** (section 5). Any string; not length-checked. |
| `amount` | number or numeric string | See section 4. Must be > 0. |
| `currency` | string | **Not validated** and not checked against the accounts (accounts have no currency). Stored on the transaction item only. |
| `source_account` | string | The `<id>` in `ACCOUNT#<id>`. |
| `destination_account` | string | The `<id>` in `ACCOUNT#<id>`. |

Extra fields are accepted and ignored (not stored). There is no field for a reference,
description or tenant, so everything the finance service needs to trace a transfer must
be encoded in `event_id`.

## 4. Amount parsing: the float issue

The body is parsed with plain `json.loads` (177), so **a JSON number becomes a Python
`float`** before `Decimal(str(...))` (74, 141). Reproduced with the ledger's own code
path:

| Body | Parsed as | Stored amount |
|---|---|---|
| `{"amount": 250.10}` | `float` | `250.1` |
| `{"amount": "250.10"}` | `str` | `250.10` |
| `{"amount": 1234567890123456.78}` | `float` | `1234567890123456.8` (**wrong**) |
| `{"amount": "1234567890123456.78"}` | `str` | `1234567890123456.78` |

Numbers are exact up to about 15 significant digits and silently rounded beyond that.
**Strings never pass through a float and are exact at any size.** The ledger's own tests
and `generate_hmac.py` send strings.

Other parsing facts:

- Amounts are **not quantised**: `"250.005"` is accepted and stored as is.
- `"1e2"` is accepted (as 100).
- `"NaN"` makes `amount <= 0` raise `InvalidOperation` outside the `try` (144), so the
  Lambda fails and API Gateway returns 500. `"Infinity"` passes validation, then fails
  in DynamoDB (500).

**Adapter rule:** the amount is always sent as a **JSON string** with exactly two
decimal places, produced from integer minor units with
`(Decimal(minor) / 100).quantize(Decimal("0.01"))` (the exponent comes from the
currency; ZAR is 2). It is never a JSON number, never derived from a float, and always
a positive finite value checked before sending. That makes the float path unreachable
from this service.

## 5. Deduplication (`ingestion.py` 66–68, 80–98, 189–194)

Deduplicated on **`event_id`**, in two layers:

1. A `GetItem` on `EVENT#<event_id>` before the transfer. If it exists → **200**
   `{"status": "already_processed", "event_id": …}`. This read is **eventually
   consistent** (the default), so it can miss a transaction committed moments earlier.
2. The transaction's `Put` of `EVENT#<event_id>` has `attribute_not_exists(PK)`, so a
   duplicate that slips past step 1 cancels the whole transaction → **422**.

Two consequences for the adapter:

- **A duplicate is recognised by `event_id` alone.** Re-sending an `event_id` with a
  *different* amount returns 200 `already_processed` without comparing anything. The
  adapter therefore uses a deterministic `postingId` as `event_id` and never changes a
  posting's payload; reconciliation (section 9) compares amounts.
- **422 is ambiguous**: insufficient balance, a missing source account, and a duplicate
  racing an earlier attempt all return the same 422 (the transaction's
  `CancellationReasons` aren't inspected). See section 7 for how the adapter resolves it.

## 6. Responses

All bodies are JSON with `Content-Type: application/json`.

| Status | Body | Meaning | Adapter outcome |
|---|---|---|---|
| 200 | `{"status": "completed", "event_id": …}` | Transfer applied | `POSTED` |
| 200 | `{"status": "already_processed", "event_id": …}` | Duplicate `event_id` | `POSTED` (idempotent retry) |
| 400 | `{"error": "Bad Request: malformed JSON payload"}` or `"Bad Request: <reason>"` | Missing field, non-numeric or non-positive amount | `REJECTED`, `VALIDATION_FAILED` (permanent) |
| 401 | `{"error": "Unauthorized: invalid signature"}` | Signature missing or wrong | `REJECTED`, `SIGNATURE_REJECTED` (permanent) |
| 422 | `{"error": "Unprocessable Entity: transaction failed (insufficient balance or duplicate event)"}` | Transaction cancelled (section 5) | Ambiguous: resolved as in section 7 |
| 500 | `{"error": "Internal Server Error"}` | Any other DynamoDB error (throttling, validation) or an uncaught exception | Retry |
| 503/504, timeouts, connection errors | API Gateway or transport | — | Retry |
| 429 | API Gateway throttling | — | Retry with backoff |

**A missing destination account** is a special case: the update
`SET balance = balance + :amount` on an item that doesn't exist refers to a missing
attribute. Whether DynamoDB reports that as a cancelled transaction (422) or a
validation error (500) can't be confirmed without the real service; the integration
tests will pin it down. Onboarding creates every account before any posting, so in
normal operation this doesn't arise.

## 7. Resolving the 422 ambiguity (proposed)

A 422 is either a permanent rejection (insufficient funds, unknown source account) or a
duplicate that raced an earlier attempt which did commit. The adapter can tell them
apart **without** reading the ledger table:

- On 422, wait a few seconds and send the **same request** again, up to two more times.
  If the earlier attempt committed, the ledger's pre-check now sees it and returns
  200 `already_processed` → `POSTED`. If every attempt returns 422 →
  `REJECTED_INSUFFICIENT_FUNDS` (permanent, alarmed).
- The daily reconciliation independently catches any posting this service marked
  rejected that the ledger actually applied (section 9).

The alternative is granting `posting_worker` a read of `EVENT#` items in the ledger
table, which the finance spec reserves for `reconciliation_job`. The retry approach keeps
the spec's permission boundary.

## 8. Data model (`ingestion.py` 80–124; README "Trying it out")

**Account:** `PK = ACCOUNT#<id>`, `SK = METADATA`, `balance` (Number, decimal major
units). No currency, owner or tenant attributes. There is **no API to create an
account**: accounts are created with `PutItem`, as the README shows:

```json
{"PK": {"S": "ACCOUNT#<id>"}, "SK": {"S": "METADATA"}, "balance": {"N": "0"}}
```

Balances can't go negative: the source update has `ConditionExpression
balance >= :amount`; the destination update has no condition.

**Transaction:** written in the same `TransactWriteItems` as both balance updates.

| Attribute | Type | Value |
|---|---|---|
| `PK`, `SK` | S | `EVENT#<event_id>` (both) |
| `GSI1PK` | S | `STATUS#COMPLETED` |
| `GSI1SK` | S | `TS#<epoch seconds>` (a string; sorts correctly while epochs have 10 digits) |
| `status` | S | `COMPLETED` |
| `timestamp` | N | epoch seconds (one-second resolution) |
| `amount` | N | as parsed (section 4) |
| `currency`, `source_account`, `destination_account` | S | from the request |

**`GSI1`:** `GSI1PK` / `GSI1SK`, projection `ALL` (`ledger_stack.py` 78–83). Every
completed transaction, for every tenant, is in one partition, `STATUS#COMPLETED`.

**Other:** the table is `FinancialLedger` (fixed name), on-demand, PITR on, stream
`NEW_AND_OLD_IMAGES`, retained on stack deletion.

## 9. Reconciliation queries (for `reconciliation_job`, read-only)

- **Transactions for a day:** `Query` `GSI1` with
  `GSI1PK = STATUS#COMPLETED AND GSI1SK BETWEEN "TS#<start>" AND "TS#<end>"`, then keep
  items whose `event_id` starts with this school's prefix (`<sid>:`), since the ledger
  has no tenant attribute. GSI reads are eventually consistent, so the job reconciles
  the *previous* day, not the current one.
- **Account balances:** `GetItem` on `ACCOUNT#<sid>:<name>` / `METADATA`.
- **Comparison:** each `POSTED` posting must have exactly one ledger transaction with the
  same `event_id`, amount, accounts and currency; each ledger transaction with this
  school's prefix must have a `POSTED` posting; and each account's balance must equal its
  opening balance plus the net of its postings.

## 10. The audit archive (`cdc_archiver.py`)

The stream consumer writes completed `EVENT#` items to Parquet in
`ledger-audit-archive-<account>-<region>`. It converts every non-integer amount to a
**`float`** (`_decimal_to_number`, 38–42), so the long-term cash audit trail stores
amounts like `250.1` as binary floating point. Values within about 15 significant digits
read back correctly, but the archive's column type is float, not decimal. This doesn't
affect the finance service (it never reads the archive), but it is a weakness of the
ledger's audit trail.

## 11. Other facts

- **No tenant concept:** tenancy lives entirely in account IDs (`<sid>:EXTERNAL`, …) and
  `event_id` prefixes (`<sid>:payment:<paymentId>`, …).
- **Runtime:** Python 3.12, Powertools v2 layer (the ledger's own stack).
- **Region:** the stack itself uses `Aws.REGION`; the README's commands assume
  `us-east-1`. The finance stack takes the ledger's URL, secret ARN and table name from
  `cdk.json`, so it doesn't depend on either.
- **Retained resources:** the table and the archive bucket survive `cdk destroy`.

## 12. Decisions to confirm

1. **The float parsing (section 4).** The spec says to stop here if the ledger parses
   amounts as `float`. It does, for JSON numbers. My recommendation is **not** to change
   the ledger for this service: sending amounts as strings makes the float path
   unreachable, and the ledger's own tooling already does so. A one-line fix in the
   ledger (`json.loads(raw_body_bytes, parse_float=Decimal)`) would protect any future
   client and is worth doing separately, together with the archive's float column
   (section 10), but neither blocks this work.
2. **The 422 ambiguity (section 7):** resolve by retrying the same request, keeping the
   ledger-table read for `reconciliation_job` only, as the spec intends.
3. **`event_id` formats:** `<sid>:payment:<paymentId>`,
   `<sid>:settlement:<settlementId>:net` / `:fee`, `<sid>:cash-banked:<id>`,
   `<sid>:other-income:<id>`, `<sid>:payslip:<runId>:<staffId>:net`,
   `<sid>:remittance:<runId>:<ruleId>`. All deterministic, so a retry always sends the
   same `event_id`.
