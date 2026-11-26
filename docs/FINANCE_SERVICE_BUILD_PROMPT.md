# Build Prompt: Finance Service (School Fees and Payroll)

> **How to use this file:** give it to a coding agent (e.g. Claude Code) as the task specification, together with `EVENT_CATALOGUE.md`, `INGESTION_BUILD_PROMPT.md` (shared conventions) and read access to the existing `FinancialLedgerAPI` repository. Section 13 tells the agent how to work.

## 1. Context

You are building the **finance service** for a multi-tenant school management system on AWS. Services communicate only through the shared EventBridge bus `SchoolEventBus`, as defined in `EVENT_CATALOGUE.md`.

The service covers two areas:

- **Fees:** fee schedules per grade (with optional per-class overrides), sibling discounts, staff-child discounts, manual bursaries, termly billing runs, invoices, credit notes, online payments (Paystack), manually recorded payments (cash, bank deposit, mobile money), payment allocation, statements and overdue reminders.
- **Payroll:** staff pay profiles, pay grades, configurable deduction rules (no country's tax rules are hard-coded), monthly payroll runs with maker-checker approval, payslips, and payout. **Payout is record-only by default:** the school pays staff outside the system and records that it did. Automated payouts through a provider API are an optional, disabled-by-default mode.

### The existing ledger is wrapped, not changed

Money that actually moves is recorded in the existing **`FinancialLedgerAPI`** (a separate repo and stack, deployed independently). **Do not modify that repo.** This service is its only client and wraps it behind an adapter. Its known properties, from its README (confirm each against its source code before writing the adapter):

- One HTTP endpoint, `POST /v1/webhooks/payment`, which moves an amount from a source account to a destination account. Requests are HMAC-signed with a key in Secrets Manager (`ledger/webhook-signing-secret`); `scripts/generate_hmac.py` shows the signing scheme and payload.
- Duplicate requests are recognised and not applied twice (find out which payload field it deduplicates on).
- A transfer is rejected if the source balance is insufficient; balances never go negative.
- Both balance updates and the transaction record are written in one `TransactWriteItems`.
- Amounts are **decimal major units** (e.g. `250.00`), not integer minor units.
- DynamoDB table `FinancialLedger`: accounts at `PK = ACCOUNT#<id>`, `SK = METADATA`, with `balance`; completed transactions queryable on `GSI1` with `GSI1PK = STATUS#COMPLETED`.
- A DynamoDB Streams consumer archives completed transactions to S3 as Parquet (the cash audit trail).
- No tenant concept, no EventBridge events, some ARNs hard-coded to `us-east-1`, and the table and archive bucket are retained on stack deletion.

### How the two fit together

The ledger is the **cash book**: it records only real movements of money between cash-holding accounts (provider balance, bank, cash office, and the outside world). Everything that is not a cash movement lives in this service: fee schedules, invoices, discounts, credit notes, what a family owes, payroll calculations, payslips and withheld deductions. Together, the ledger and this wrapper form one service boundary: no other service calls the ledger or reads its table.

This split is forced by the ledger's design (transfers only, no negative balances, no multi-line journal entries) and is also a standard accounting split: a cash book plus sub-ledgers for receivables and payroll.

### Data boundaries (non-negotiable)

- **Salaries, deductions, payslips and bank details are never in events, logs or metrics.** Events carry IDs, counts and statuses. A payroll run's total is visible only through the API to finance roles.
- **Discount reasons are never in events.** A staff-child discount reveals employment and a sibling discount reveals family composition; invoices on the bus carry the total discount only.
- **In record-only payout mode, no bank details are stored at all.**
- Parents see only their own children's fee accounts. Staff see only their own payslips.

## 2. Technical stack (fixed)

Same conventions as the ingestion and knowledge services:

- **IaC:** AWS CDK v2 in Python.
- **Runtime:** the newest Python Lambda runtime that both AWS Lambda and Powertools support at build time (3.13 or later), `arm64`, with an arm64 dependencies layer built via `pip install --platform manylinux2014_aarch64 --only-binary=:all:` (see the ingestion spec).
- **Libraries:** Powertools (logging, metrics, tracing, event handler, idempotency, batch processing, parameters), `pydantic` v2, `httpx`, `python-ulid`, `boto3`. Use `decimal.Decimal` only inside the ledger adapter and the calculation modules' rounding helpers; everywhere else money is `int` minor units. Never `float`.
- **Storage:** DynamoDB (on-demand, PITR on), with a customer-managed KMS key for the finance tables.
- **API:** API Gateway HTTP API. Cognito JWT authorizer (the shared user pool) on every route except the payment-provider webhook.
- **Messaging:** transactional outbox (section 4) → EventBridge Pipes → `SchoolEventBus`; SQS consumers with DLQs for inbound events.
- **Quality:** `pytest`, `hypothesis` (property-based tests for every calculation), `moto`, CDK `assertions`, `cdk-nag`, `ruff`, `mypy --strict` on `src/`.

## 3. Configuration per school

A `SchoolFinanceConfig` item per school, editable only by `admin`, versioned (every change writes a new version with `changedBy` and `changedAt`):

| Setting | Default | Notes |
|---|---|---|
| `currency` | `ZAR` | ISO 4217. **`ZAR` is the only accepted value in v1**, matching the ingestion service's `fee_schedule` rule. Keep it as a setting rather than a constant so a later version can open it up. Amounts are integer minor units. |
| `roundingIncrementMinor` | `1` | Discounts and prorated amounts round to this increment (e.g. `100` to round to whole units). Half-up. |
| `invoiceDueDays` | `30` | Days from issue to due date. |
| `overdueReminderDays` | `[7, 14, 30]` | Days past due on which `InvoiceOverdue` is published. |
| `discountStacking` | `BEST_SINGLE` | `BEST_SINGLE`: each student gets only their largest applicable discount per fee line. `ADDITIVE`: discounts add, capped at `maxTotalDiscountPercent`. |
| `maxTotalDiscountPercent` | `100` | Used with `ADDITIVE`. |
| `manualAdjustmentApprovalThresholdMinor` | `0` | Credit notes and manual discounts above this need a `finance_approver`. `0` means all need approval. |
| `payoutMode` | `RECORD_ONLY` | `RECORD_ONLY` or `PROVIDER`. See section 8. |
| `payoutProvider` | `null` | Required when `payoutMode` is `PROVIDER`. |
| `payoutLimits` | `null` | Required when `payoutMode` is `PROVIDER`: max per payslip and max per run. |
| `payrollProration` | `CALENDAR_DAYS` | How partial months are prorated for joiners and leavers. |

Switching `payoutMode` to `PROVIDER` requires `payoutProvider`, `payoutLimits`, and an approval from a second user in `finance_approver`; the request and approval are both audited.

## 4. Infrastructure (`FinanceStack`)

Receives the bus, ops topic, user pool, the ingestion bucket (read-only, `processed/fee_schedule/*`), and the ledger's coordinates (API URL, signing-secret ARN, table name) as constructor props. Ledger coordinates come from `cdk.json` context, never hard-coded.

### Cognito (extend `SharedStack`)

Add groups `bursar` (prepares: fee schedules, billing runs, manual payments, payroll drafts) and `finance_approver` (approves: payroll runs, large adjustments, payout-mode changes). A user may hold both, but **no one may approve their own submission**; this is enforced in code by comparing the submitter's and approver's `sub` claims, not by group membership.

### DynamoDB

**`Finance`** (single table, `PK`/`SK`, KMS CMK, PITR, stream `NEW_IMAGE`). Main item types:

| Item | PK | SK |
|---|---|---|
| School config (versioned) | `SCHOOL#<sid>` | `CONFIG#v<n>` |
| Fee schedule (versioned) | `SCHOOL#<sid>#FEES#<termCode>` | `SCHED#v<n>` |
| Discount rule (versioned) | `SCHOOL#<sid>#DISCOUNT#<ruleId>` | `v<n>` |
| Fee account | `SCHOOL#<sid>#STU#<studentId>` | `ACCOUNT` |
| Invoice / credit note / payment / allocation | same | `INV#<id>` / `CRN#<id>` / `PAY#<id>` / `ALLOC#<paymentId>#<invoiceId>` |
| Family projection | `SCHOOL#<sid>#FAMILY#<familyId>` | `STU#<studentId>` |
| Guardian → students projection | `SCHOOL#<sid>#GRD#<guardianId>` | `STU#<studentId>` |
| Billing run | `SCHOOL#<sid>#BILLRUN#<runId>` | `META` / `DRAFT#<studentId>` |
| Pay grade (versioned) | `SCHOOL#<sid>#PAYGRADE#<code>` | `v<n>` |
| Deduction rule (versioned) | `SCHOOL#<sid>#DEDUCTION#<ruleId>` | `v<n>` |
| Staff pay profile | `SCHOOL#<sid>#STAFF#<staffId>` | `PROFILE` |
| Payroll run / payslip | `SCHOOL#<sid>#PAYRUN#<runId>` | `META` / `SLIP#<staffId>` |
| Ledger posting | `SCHOOL#<sid>#POSTING#<postingId>` | `META` |
| Outbox | `OUTBOX#<eventId>` | `EVENT` (TTL 7 days) |

GSIs as needed for: invoices by due date and status (overdue job), payments by provider reference (webhook lookup), payslips by staff (staff self-service), postings by status (reconciliation). Name each and state its query in the code.

**Rules for money items:** issued invoices, credit notes, payments, allocations, approved payroll runs and payslips are **immutable**. Corrections are new items (a credit note, a reversing allocation, a new payroll run). The fee account's cached `balanceMinor` is updated in the same `TransactWriteItems` as the item that changes it, with a condition on its `version` attribute.

**`FinanceAudit`**: append-only (`PK = SCHOOL#<sid>`, `SK = <ISO timestamp>#<ulid>`), recording actor, action, target, and before/after hashes. Grant `PutItem` only; attach an explicit IAM `Deny` on `DeleteItem` and `UpdateItem` for every role.

**`FinanceIdempotency`**: Powertools idempotency table.

### Transactional outbox

Every state change that produces an event writes the domain items **and** an `OUTBOX#` item in one `TransactWriteItems`. An EventBridge Pipe reads the `Finance` table stream, filters to `INSERT` of `OUTBOX#` items, and targets `SchoolEventBus` with an input transformer that emits the stored `detail-type` and `detail`. This guarantees no event is published for a change that didn't commit, and no committed change loses its event. Pipe failures go to a DLQ with an alarm.

### Inbound events

One SQS queue (with DLQ, `maxReceiveCount: 3`) fed by EventBridge rules for: `StudentEnrolled`, `StudentWithdrawn`, `GuardianLinked`, `ClassAssignmentChanged` (source `school.records`), and `ImportCompleted` with `datasetType: FEE_SCHEDULE` (source `school.ingestion`). Consumer Lambda: batch size 10, partial batch failures, idempotent on `metadata.eventId`, and tolerant of out-of-order arrival (upserts; events for unknown students are parked and retried).

### Lambdas

| Function | Trigger | Notes |
|---|---|---|
| `api` | HTTP API | All authenticated routes (section 9). |
| `paystack_webhook` | HTTP API route without JWT | Section 6. Its own minimal role. |
| `event_consumer` | SQS | Section 5. |
| `billing_worker` | Step Functions (Map over students) or SQS fan-out | Computes billing-run drafts. |
| `payroll_worker` | invoked by `api` | Computes payroll drafts (≤ a few hundred staff; one invocation is fine). |
| `posting_worker` | SQS `ledger-postings` queue | The only code that calls the ledger (section 7). |
| `overdue_job` | EventBridge Scheduler, daily | Publishes `InvoiceOverdue` on threshold days only. |
| `reconciliation_job` | EventBridge Scheduler, daily | Section 7. |
| `payout_webhook` | HTTP route without JWT | Only deployed when a payout provider is configured. |

### Alarms → ops topic

DLQs non-empty (inbound, postings, Pipe); any `PostingRejected`; reconciliation mismatch > 0; webhook signature failures > 5 in 5 minutes; payout failures > 0.

### IAM

As in the other services: no `Action: *`, prefix wildcards only on this service's own resources, no AWS managed policies. In addition:

- Only `posting_worker` and `reconciliation_job` may read the ledger's signing secret or table. `posting_worker` gets `secretsmanager:GetSecretValue` on that one secret; `reconciliation_job` gets `dynamodb:GetItem`/`Query` on the ledger table and `GSI1`, read-only.
- The webhook functions can read only their provider's secret and write only payment/webhook items.
- Payout provider secrets exist and are readable only when `payoutMode` is `PROVIDER`.

## 5. Fees

### 5.1 Fee schedules

- A fee schedule is per school and term: a list of fee items per `gradeCode` (`TUITION`, levies, and optional items such as transport or lunch, each marked `mandatory` or `optional`), with optional **per-class overrides** (`classCode` → item amounts), so classes in the same grade can have different rates.
- **From ingestion:** on `ImportCompleted` (`FEE_SCHEDULE`), read `validRecordsKey` (a claim-check file that expires after 45 days; copy what you need now) and create a **draft** schedule version. A bursar reviews it and publishes it through the API. Imported rows never change a published schedule directly.
- **From the API:** a bursar can create and edit drafts, including class overrides (the ingestion `fee_schedule` dataset is grade-level only).
- Rows in a currency other than the school's configured currency are rejected with a validation error.
- A published version is immutable; a correction is a new version. Billing runs record which version they used.

### 5.2 Families and siblings

- The service keeps projections from `StudentEnrolled` (`guardianIds`) and `GuardianLinked`.
- **Default family:** students who share the same primary-contact guardian (`isPrimaryContact: true`) form a family; `familyId` is that guardian's ID.
- A bursar can override this with an explicit family grouping (e.g. separated parents who each list a different primary contact), stored as an override item and audited. Overrides win over the default.
- Withdrawn students leave the family's active set.

### 5.3 Discounts

Discount rules are versioned per school, each with `appliesToFeeItems` (e.g. tuition only, never lunch or transport unless configured):

| Type | Parameters | Eligibility |
|---|---|---|
| `SIBLING` | rates by position, e.g. `{2: 10%, 3: 15%, "4+": 20%}` | Position within the family's active students, ordered by gross amount of the discounted items descending, then enrolment date, then `studentId`. Position 1 pays full price, so discounts land on the cheaper children. |
| `STAFF_CHILD` | `percent` (or `fixedMinor`) | The student has a guardian linked to an **active** staff pay profile (section 8.1, `guardianIds`). Ends at the first billing run after the staff member's end date. |
| `MANUAL` | `percent` or `fixedMinor`, `reason`, optional end term | Bursaries and one-off arrangements. Subject to `manualAdjustmentApprovalThresholdMinor`. |

**Calculation** (`src/finance/fees/discounts.py`, a pure function):

1. Gross lines = mandatory items for the student's grade (class override applied) + optional items the student is opted into.
2. For each line, collect applicable discounts.
3. `BEST_SINGLE`: keep the largest by amount (ties: `STAFF_CHILD`, then `SIBLING`, then `MANUAL`). `ADDITIVE`: sum, then cap at `maxTotalDiscountPercent` of the line.
4. Discount amount = `round_half_up(gross × rate, roundingIncrementMinor)`; never more than the line's gross.
5. Each applied discount becomes its own negative invoice line, with `ruleId` and rule version, so every invoice explains itself.

A staff child counts as a sibling for position ordering in both stacking modes.

### 5.4 Billing runs

1. A bursar creates a billing run for a term, choosing a published schedule version.
2. `billing_worker` computes a draft invoice per active student, plus a **preview report**: totals, discount totals by type, and exceptions (students with no schedule for their grade, families over the discount cap, students missing a primary contact).
3. The bursar reviews and issues. Issuing writes each invoice, updates the fee account, and writes an `InvoiceIssued` outbox item, one student per transaction. A run is idempotent on `runId + studentId`, so a partially issued run can be resumed.
4. **Mid-term enrolment:** `StudentEnrolled` opens a fee account. If the current term's run is already issued, the student is added to a "late additions" list on that run for the bursar to issue (proration is a manual decision in v1).
5. Discounts are computed at issue time. A later change in family composition (a sibling enrols mid-term) does not rewrite issued invoices; the bursar can issue a credit note.

### 5.5 Credit notes and statements

- A credit note references an invoice, has lines and a reason, and needs approval above the threshold. It reduces the fee account balance.
- `StudentWithdrawn` stops future invoicing and produces a final statement (available through the API; nothing on the bus beyond the catalogue's existing events).

### 5.6 Payments and allocation

**Methods:** `PAYSTACK` (online), `CASH`, `BANK_DEPOSIT`, `MOBILE_MONEY` (manually recorded with a reference), `SALARY_DEDUCTION` (section 8.4).

- **Online:** a parent requests a payment for their child's account (amount ≤ outstanding unless prepayment is allowed). The service creates a pending payment, initialises a Paystack transaction with `reference = paymentId`, and returns the authorisation URL. Settlement happens only through the webhook (section 6), never because the browser said so.
- **Manual:** a bursar records the method, amount, date and external reference. The `(method, externalReference)` pair must be unique per school.
- **Allocation:** oldest outstanding invoice first (by due date, then issue date); any remainder becomes account credit, which is applied automatically to the next invoice issued. Allocations are items and are never edited; reversing a payment writes reversing allocations.
- **Ledger posting** (section 7) happens before allocation is final: a payment is `PENDING_LEDGER` → `POSTED` → `ALLOCATED`. `PaymentSettled` is published once allocated.

### 5.7 Overdue

`overdue_job` publishes `InvoiceOverdue` only when an invoice's days past due equal one of `overdueReminderDays`, idempotent on `invoiceId + threshold`. Invoices fully covered by payments or credit notes are skipped.

## 6. Paystack webhook (`paystack_webhook`)

Before writing this, read Paystack's current webhook and transaction-verification documentation and state the signature header, algorithm and event names you will use. As far as is known: the signature is an HMAC-SHA512 of the raw request body using the secret key, in the `x-paystack-signature` header; successful charges arrive as `charge.success`.

1. Verify the signature over the **raw body** with a constant-time compare. Failure → 401, metric, no further processing.
2. Ignore events this service doesn't handle (200, logged by type only).
3. Look up the pending payment by `reference`. Unknown reference → 200 and an alarm metric (don't make Paystack retry forever).
4. **Verify server-side** by calling Paystack's verify-transaction endpoint with the reference. Amount, currency and status from that response must match the pending payment; otherwise mark it `DISPUTED` and alarm.
5. Idempotent on the provider event (duplicate webhooks are normal).
6. Enqueue the ledger posting (section 7). Respond 200 quickly; never call the ledger inside the webhook request.

A failed charge marks the payment `FAILED` and publishes `PaymentFailed`.

## 7. Ledger adapter and postings

### 7.1 Chart of ledger accounts (per school)

Created once per school by `scripts/onboard_school.py`, using the ledger's own documented method (`PutItem` on its table). This is the only write to the ledger table outside its API, and it is a manual, audited setup step, not runtime code.

| Account ID | Purpose |
|---|---|
| `<sid>:EXTERNAL` | The outside world: parents, staff, tax authorities, other income. Because the ledger forbids negative balances and has no concept of external money, this account is seeded with a very large opening balance (e.g. 10¹³ major units) and acts as a contra account. Its absolute balance is meaningless; only its movements are reported. |
| `<sid>:PROVIDER:PAYSTACK` | Money collected online but not yet settled to the bank. |
| `<sid>:BANK:OPERATING` | The school's bank account. |
| `<sid>:CASH` | Cash office. |

### 7.2 Posting rules

| Business event | Transfer (source → destination) |
|---|---|
| Online payment confirmed | `EXTERNAL` → `PROVIDER:PAYSTACK` (gross) |
| Paystack settlement recorded | `PROVIDER:PAYSTACK` → `BANK:OPERATING` (net settled), and `PROVIDER:PAYSTACK` → `EXTERNAL` (provider fee) |
| Cash payment | `EXTERNAL` → `CASH` |
| Bank deposit / mobile money | `EXTERNAL` → `BANK:OPERATING` |
| Cash banked | `CASH` → `BANK:OPERATING` |
| Opening balance / other income | `EXTERNAL` → `BANK:OPERATING` or `CASH` (admin, with reason) |
| Net salary paid (per payslip) | `BANK:OPERATING` → `EXTERNAL` |
| Withheld deductions remitted (per rule, per run) | `BANK:OPERATING` → `EXTERNAL` |
| Salary deduction for fees | **No posting**: no cash moves. It is an allocation inside this service. |

Invoices, discounts and credit notes are never posted: they are not cash.

Settlement is recorded by a bursar in v1 (amounts from the Paystack dashboard or settlement report); automating it from the provider's settlement API is phase 2.

### 7.3 How postings run

- Every posting is a `POSTING#` item with a deterministic `postingId` (e.g. `<sid>:payment:<paymentId>`, `<sid>:payslip:<runId>:<staffId>:net`), written in the same transaction as the business change, then enqueued on `ledger-postings`.
- `posting_worker` calls the ledger with `postingId` as the ledger's deduplication key (or mapped into whichever field the ledger deduplicates on), so retries never double-post.
- The adapter converts integer minor units to the ledger's decimal format with `Decimal(minor) / 100` quantized to `0.01` (adjust for currencies with other exponents). **Read the ledger's request parsing:** if it parses amounts as `float`, stop and raise it with me. That would be a correctness bug in the ledger and the one case where changing it is justified.
- Ledger result → posting status: success → `POSTED`; insufficient funds → `REJECTED_INSUFFICIENT_FUNDS` (permanent; alarm; the business item stays pending with a visible reason, e.g. "record the bank balance or other income first"); signature or validation error → `REJECTED` (permanent; alarm); 5xx or timeout → retry via SQS.
- `src/finance/ledger/` defines a `LedgerPort` protocol with two implementations: `HttpLedger` (the real adapter) and `InMemoryLedger` (same rules: non-negative balances, idempotency, atomic transfer) for tests.

### 7.4 Reconciliation (`reconciliation_job`)

Daily, per school: compare this service's `POSTED` postings with the ledger's completed transactions (`GSI1`, read-only) over the previous day, plus each ledger account balance against the sum of its postings. Any difference → `ReconciliationMismatch` metric, alarm, and a report in the finance bucket. Before writing this, read the ledger's item shapes in its source to find the timestamp and reference fields to query on.

## 8. Payroll

### 8.1 Staff pay profiles

`staffId` (the staff member's user ID), `payGradeCode` or an explicit `baseMonthlyMinor`, recurring allowances, assigned deduction rules, `startDate`, optional `endDate`, `guardianIds` (for staff-child discounts, set by a bursar), and `payout`:

- `RECORD_ONLY` (default): no bank details.
- `PROVIDER`: a provider recipient reference (a token from the provider, e.g. a recipient code). Store raw bank details only if the provider has no tokenisation, and then encrypted with the CMK, readable only by the payout code path.

Staff identity (names, contact details) is not owned here. In v1 a bursar creates profiles through the API. When an HR/staff service exists, profiles will be created from its events.

### 8.2 Deduction rules (no hard-coded tax rules)

Tax and other deductions are **configuration**, not code. The service must not embed any country's tax bands or contribution rates. Rules are versioned with effective dates:

| Type | Parameters |
|---|---|
| `FIXED` | `amountMinor` per month |
| `PERCENT` | `percent` of `GROSS` or `TAXABLE`, optional `capMinor` |
| `BANDED` | progressive monthly bands `[{uptoMinor, percent}, …, {uptoMinor: null, percent}]`, applied to `TAXABLE` (income tax such as PAYE) |
| `FEE_ACCOUNT` | staff-elected monthly amount paid towards a linked child's fee account (section 8.4) |

Each rule has `order`, `preTax` (reduces taxable income, e.g. an employee pension share), `kind` (`EMPLOYEE` deduction or `EMPLOYER` contribution: employer contributions are computed and reported as cost but never deducted from pay), and `remitTo` (a free-text label such as the tax authority or pension fund, for remittance tracking).

Seed data in `fixtures/` must be clearly labelled as **example values, not real tax rates**. The README explains that the school's accountant must configure and verify the real rules.

### 8.3 Payroll runs

States: `DRAFT` → `SUBMITTED` → `APPROVED` → `PAID` → `CLOSED`, with `REJECTED` (back to editable draft) and `CANCELLED` (drafts only).

1. **Draft:** a bursar creates a run for a period (month). `payroll_worker` computes a payslip for every profile active in the period. The bursar can add one-off lines (bonus, overtime, unpaid leave) with reasons, and recompute.
2. **Calculation** (`src/finance/payroll/calculate.py`, a pure function, exhaustively tested):
   - `base` = grade rate or profile override, prorated per `payrollProration` for joiners and leavers.
   - `gross` = base + allowances + one-off additions − one-off reductions.
   - `taxable` = gross − pre-tax employee deductions.
   - Apply employee deductions in `order`; `BANDED` and `TAXABLE`-based rules use `taxable`.
   - `net` = gross − all employee deductions. A negative net (or below a configured minimum) is an **exception**: the run cannot be submitted until it is resolved.
   - Employer contributions are computed separately.
   - Rounding with `roundingIncrementMinor`, half-up, per line; the payslip stores every line, so the arithmetic is reproducible from the payslip alone.
3. **Submit** (bursar) → **approve or reject** (`finance_approver`, a different user). Approval freezes the run: payslips become immutable. A mistake after approval is fixed by a correction run.
4. **Pay**, depending on `payoutMode`:
   - `RECORD_ONLY` (default): a bursar records that the run was paid (date, bank reference). The service writes one net-pay posting per payslip and marks the run `PAID`. If the bank account's recorded balance is too low, postings are rejected and the run shows why (record other income or the opening balance first).
   - `PROVIDER`: section 8.5.
5. **Remittances:** when withheld deductions are paid to whoever they're owed to, a bursar records a remittance per rule per run; that produces a posting. Outstanding withheld amounts are reported until remitted.
6. **Payslips** become visible to each staff member once the run is `PAID`, and `PayslipsPublished` is emitted. v1 serves payslips as JSON for the UI to render; a PDF is phase 2.

### 8.4 Paying fees by salary deduction

A staff member with a linked child can authorise a monthly `FEE_ACCOUNT` deduction (fixed amount, capped at the child's outstanding balance at calculation time and at the staff member's net pay). When the run is marked `PAID`, the service records a `SALARY_DEDUCTION` payment on the child's fee account and allocates it as in section 5.6. No ledger posting is made, because no cash moves. Authorisation is per staff member, auditable and revocable.

### 8.5 Automated payouts (optional, off by default)

Behind a `PayoutProvider` protocol with `RecordOnlyPayout` (the default; does nothing), `FakePayout` (tests), and one real adapter. Before implementing the real adapter, check the provider's current documentation **and its availability for the school's country and currency**: do not assume a provider that serves South Africa also serves Malawi. State what you found before writing code.

When enabled:

- Only `APPROVED` runs can be paid out, and only by a user other than the approver or the submitter (three distinct people is ideal; two is the minimum, enforced).
- Per-payslip and per-run limits from `payoutLimits` are checked before any call; a breach blocks the whole run.
- One transfer per payslip, with `providerReference = <runId>:<staffId>`, so retries are idempotent at the provider.
- Statuses arrive by the provider's webhook (`payout_webhook`, signature-verified and confirmed by a status API call, exactly as in section 6). Success → net-pay posting; failure → `PayoutFailed`, and the payslip is marked for manual handling. Failed payouts are never retried automatically; retrying needs a new approval.
- The run becomes `PAID` when every payslip has succeeded or been resolved manually.

## 9. API (selected routes)

All routes take `school_id` from `custom:school_id` in the JWT and never from the request.

| Route | Roles | Purpose |
|---|---|---|
| `GET /fees/accounts` | parent | Fee accounts of the caller's own children (via guardian links), with balances |
| `GET /fees/accounts/{studentId}/statement` | parent (own child), bursar | Invoices, credit notes, payments, allocations |
| `POST /fees/payments` | parent | Start an online payment; returns the authorisation URL |
| `POST /fees/payments/manual` | bursar | Record a cash, bank or mobile-money payment |
| `POST /fees/schedules/{termCode}/drafts`, `.../publish` | bursar | Schedule drafts and publishing |
| `PUT /fees/discount-rules/{ruleId}` | bursar | New version of a rule |
| `POST /fees/billing-runs`, `GET .../{runId}/preview`, `POST .../{runId}/issue` | bursar | Billing runs |
| `POST /fees/credit-notes`, `POST .../{id}/approve` | bursar / finance_approver | Credit notes |
| `PUT /fees/families/{familyId}` | bursar | Family override |
| `POST /finance/settlements`, `POST /finance/cash-banked`, `POST /finance/other-income` | bursar | Cash-book entries |
| `PUT /payroll/profiles/{staffId}` | bursar | Pay profiles |
| `PUT /payroll/deduction-rules/{ruleId}`, `PUT /payroll/pay-grades/{code}` | bursar | Rules and grades |
| `POST /payroll/runs`, `.../{runId}/recalculate`, `.../submit` | bursar | Payroll drafts |
| `POST /payroll/runs/{runId}/approve`, `.../reject` | finance_approver | Approval |
| `POST /payroll/runs/{runId}/record-paid` or `.../payout` | bursar / third user | Pay, depending on `payoutMode` |
| `POST /payroll/remittances` | bursar | Record a remittance |
| `GET /payroll/payslips`, `GET /payroll/payslips/{runId}` | staff (own only) | Self-service |
| `POST /payroll/fee-deduction-authorisations` | staff | Authorise or revoke paying a child's fees by salary deduction |
| `PUT /finance/config`, `.../approve` | admin / finance_approver | School config; payout-mode change approval |

Every mutating route writes an audit item. Validation errors return 400 with field-level codes; authorisation failures return 403 without revealing whether the resource exists.

## 10. Events

The finance service publishes under `source = school.finance`. The events, their fields and their subscribers are defined in `EVENT_CATALOGUE.md` (section "Finance — `school.finance`"); implement them exactly as written there, and treat the catalogue as authoritative if it and this spec ever disagree:

- `InvoiceIssued` (with `grossCents`, `discountCents` as a total only, `billingRunId`; `amountCents` is the net amount)
- `CreditNoteIssued`
- `PaymentSettled` (with `method`; no link to a staff member for salary-deduction payments)
- `PaymentFailed`
- `InvoiceOverdue` (reminder days only)
- `PayrollRunApproved`, `PayslipsPublished` (no amounts)
- `PayoutFailed` (automated-payout mode only)
- `LedgerPostingRejected` (`reasonCode`: `INSUFFICIENT_FUNDS`, `VALIDATION_FAILED`, `SIGNATURE_REJECTED`)

Consumed: `StudentEnrolled`, `StudentWithdrawn`, `GuardianLinked`, `ClassAssignmentChanged`, `ImportCompleted (FEE_SCHEDULE)`.

## 11. Testing and CI

**Unit tests** (with `InMemoryLedger`, `FakePayout`, a fake Paystack client, `moto`):

- **Fees calculation (property-based):** a discount never exceeds its line; net is never negative; invoice total = sum of lines; `BEST_SINGLE` never yields more than one discount per line; `ADDITIVE` never exceeds the cap; sibling ordering is deterministic and position 1 is never discounted; rounding is half-up to the increment; identical inputs give identical invoices.
- **Fees scenarios:** two siblings, one of them a staff child, in each stacking mode; a class override; mid-term enrolment goes to late additions; staff leaves and the discount ends at the next run; family override.
- **Payments:** webhook signature failure, duplicate webhook, verify-API mismatch → `DISPUTED`, unknown reference; allocation oldest-first with overpayment → credit; manual payment reference uniqueness; reversal.
- **Payroll calculation (property-based):** net = gross − employee deductions; employer contributions never change net; `BANDED` equals a reference implementation; proration; negative net blocks submission.
- **Maker-checker:** self-approval rejected; post-approval edits rejected; payout by approver rejected in provider mode; payout limits block the whole run.
- **Ledger adapter:** minor → decimal conversion for many values; deterministic posting IDs; idempotent retries; insufficient funds → `REJECTED_INSUFFICIENT_FUNDS` and an alarm metric; salary-deduction payments create no posting.
- **Outbox:** a failed transaction writes no outbox item; every committed change has exactly one outbox item.
- **Privacy:** captured logs and published event payloads never contain salary, deduction, discount-reason or bank fields (assert by scanning for those keys and known test values).

**Infrastructure tests:** the JWT authorizer is on every route except the two webhooks; only `posting_worker` can read the ledger secret; the ledger table grant is read-only and only on `reconciliation_job`; the audit table has the explicit deny; Pipe filter and DLQ; KMS CMK on the finance tables; payout resources absent when `payoutMode` is `RECORD_ONLY`; cdk-nag passes with the suppression rules of the other services.

**Integration (opt-in, `pytest -m integration`):** against a deployed dev stage **and the real deployed ledger**: onboard a test school, issue one invoice, simulate a signed Paystack test-mode webhook, and assert the ledger shows `EXTERNAL → PROVIDER:PAYSTACK`, the invoice is paid, and `PaymentSettled` arrives on the bus. Then run a record-only payroll for two staff and assert the postings.

**CI:** as in the other services.

## 12. Out of scope for v1

- Refunds to parents through the provider (record them manually as other cash movements).
- Automatic settlement import from the provider.
- Automatic fee proration for mid-term joiners.
- Statutory filings and annual tax certificates (exports for the accountant are phase 2).
- Leave management, timesheets and overtime calculation (enter as one-off payroll lines).
- PDF invoices and payslips.
- General-ledger accounting exports.
- Multiple currencies per school.

## 13. Instructions to the coding agent

1. **Before writing any code, read the `FinancialLedgerAPI` repo** (`src/lambdas/ingestion.py`, `src/lambdas/common/signature.py`, `scripts/generate_hmac.py`, `stacks/ledger_stack.py`) and write `docs/ledger-contract.md`: the request payload, signature scheme, deduplication field, error responses, amount parsing, item shapes and GSI keys. Stop for my review. Do not modify the ledger repo.
2. Work in phases and stop for review after each: (a) the ledger contract doc; (b) `FinanceStack` infrastructure, outbox Pipe, Cognito groups, infra tests; (c) `LedgerPort` with `InMemoryLedger` and `HttpLedger`, posting worker, onboarding script; (d) fee schedules, families, discount calculation, billing runs; (e) payments, Paystack webhook, allocation, overdue job; (f) payroll profiles, rules, calculation, runs, record-only pay, remittances, salary deduction for fees; (g) reconciliation, alarms; (h) optional payout provider adapter; (i) README.
3. Run the full test suite and `cdk synth` at the end of every phase.
4. Do not hard-code account IDs, regions, currencies, tax rates, provider names in business logic, or the ledger's URL.
5. If this spec is ambiguous or seems wrong, say so and propose a resolution before implementing it.
6. Keep calculation code pure (no I/O) so it can be tested in isolation and reasoned about by an accountant.

## 14. README contents

- Architecture diagram (Mermaid): finance service, ledger, outbox Pipe, bus, Paystack, payout provider (optional).
- Why the ledger is a cash book and this service holds the sub-ledgers; the `EXTERNAL` contra account and why it is needed.
- Discount rules with worked examples (two siblings, one a staff child, in each stacking mode).
- Payroll calculation with a worked example, and the warning that seeded rules are examples, not real tax rates.
- Maker-checker rules and why payouts are record-only by default.
- Onboarding a school, configuring rules, running a term's billing and a month's payroll.
- Reconciliation and what to do about each alarm.
- Privacy decisions: what never appears in events or logs, and why.
