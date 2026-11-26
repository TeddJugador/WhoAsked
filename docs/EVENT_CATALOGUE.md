# School Management System — Event Catalogue

All services communicate asynchronously through a single Amazon EventBridge custom bus, `SchoolEventBus`. Services never call each other's databases or Lambdas directly. A service publishes a fact about something that happened in its own domain, and any interested service subscribes with an EventBridge rule.

## 1. Conventions (apply to every event)

### Envelope

EventBridge supplies `source`, `detail-type` and `time`. Every `detail` payload additionally carries a common metadata block:

```json
{
  "metadata": {
    "eventId": "01JC8Z3V7K4Q2M9X5N6P0R1S2T",
    "schemaVersion": "1.0",
    "occurredAt": "2026-11-03T09:14:22.512Z",
    "correlationId": "import-7f3a9c2e1b4d",
    "schoolId": "SCH-001",
    "producer": "ingestion-service"
  },
  "data": { }
}
```

| Field | Rule |
|---|---|
| `eventId` | ULID, unique per event. Consumers deduplicate on it. |
| `schemaVersion` | `MAJOR.MINOR`. Additive changes bump MINOR. Breaking changes publish a new `detail-type` (e.g. `PaymentSettled.v2`) alongside the old one during migration. |
| `occurredAt` | ISO 8601 UTC, when the fact happened (not when it was published). |
| `correlationId` | Carried through every downstream event and log line so one upload or payment can be traced end to end. |
| `schoolId` | Tenant key. Every rule and every table is partitioned by it. The only exception is the sentinel `UNKNOWN`, used solely on `ImportFailed` when an upload's key has no valid school ID; tenant-scoped rules therefore never match it. |

### Naming

- `source`: `school.<service>` — e.g. `school.ingestion`, `school.finance`.
- `detail-type`: past-tense PascalCase fact — `PaymentSettled`, never `SettlePayment`. Events describe what happened; they are not commands.

### Data rules

- **IDs, not personal data.** Events carry identifiers (`studentId`, `guardianId`), never names, emails, ID numbers or marks. Consumers that need details fetch them from the owning service's API. Most data subjects are minors, and this keeps POPIA exposure confined to the services that own the data and out of event archives, logs and DLQs.
- **Money** is an integer number of cents plus an ISO 4217 currency code (`"amountCents": 125000, "currency": "ZAR"`). No floats, ever.
- **Large payloads use the claim-check pattern:** the event carries an S3 key, not the data itself (EventBridge caps events at 256 KB).
- **Claim-check files can contain personal data** (e.g. a roster's `valid.jsonl` holds learners' names and guardian contacts), so only the consuming service that owns that data reads them, and they are never copied into events, logs or analytics. They are staging, not storage: the producer keeps them for **at least 45 days** (longer than the 30-day bus archive plus margin, so a replayed event still finds its file) and then deletes them. A consumer that needs the data longer copies it into its own storage when it processes the event.

### Delivery guarantees consumers must assume

- **At least once:** the same event may arrive twice. Every consumer is idempotent on `eventId`.
- **No ordering:** `PaymentSettled` may arrive before `InvoiceIssued`. Consumers handle out-of-order arrival (e.g. upsert, or park and retry).
- **Failure isolation:** every rule target has its own SQS DLQ and a retry policy. One broken consumer never blocks the others.
- **Replay:** an EventBridge archive on the bus (retention 30 days) allows replaying events into a fixed consumer.

## 2. Events by publisher

### Student records — `school.records`

The source of truth for students, guardians, classes and enrolments.

**`StudentEnrolled`**

```json
"data": {
  "studentId": "STU-2026-00412",
  "academicYear": 2027,
  "gradeCode": "10",
  "classCode": "10B",
  "guardianIds": ["GRD-00981", "GRD-00982"],
  "enrolmentDate": "2027-01-15"
}
```

Subscribers: **finance** opens a fee account (and, if the current term's billing run is already issued, adds the student to its late additions for the bursar to invoice); **gradebook** adds the student to the class roster; **notifications** sends a welcome message to each guardian.

**`StudentWithdrawn`** — `studentId`, `effectiveDate`, `reasonCode` (`TRANSFER`, `GRADUATED`, `OTHER`).
Subscribers: **finance** produces a final statement and stops future invoices; **gradebook** archives the student's records; **notifications** stops scheduled messages.

**`ClassAssignmentChanged`** — `studentId`, `academicYear`, `fromClassCode`, `toClassCode`.
Subscribers: **gradebook** moves the student between class rosters; **finance** uses the new class for class-level fee overrides from the next billing run.

**`GuardianLinked`** — `studentId`, `guardianId`, `relationship`, `isPrimaryContact`.
Subscribers: **notifications** registers the guardian's contact preferences; **finance** updates its guardian and family projections, and sets the billing contact when `isPrimaryContact` is true (students sharing a primary contact form a family for sibling discounts).

### File ingestion — `school.ingestion`

Validates and stages bulk uploads. It does not own domain data: the domain services consume its validated output.

**`ImportCompleted`**

```json
"data": {
  "importId": "imp-7f3a9c2e1b4d",
  "datasetType": "MARKS",
  "sourceFileKey": "raw/marks/SCH-001/term4_grade10_maths.csv",
  "sourceVersionId": "3HL4kqtJlcpXroDTDmJ.rmSpXd3dIbrHY",
  "status": "COMPLETED_WITH_ERRORS",
  "totalRows": 312,
  "validRows": 309,
  "invalidRows": 3,
  "validRecordsKey": "processed/marks/SCH-001/imp-7f3a9c2e1b4d/valid.jsonl",
  "summaryKey": "processed/marks/SCH-001/imp-7f3a9c2e1b4d/summary.json",
  "uploadedBy": "USR-teacher-0042"
}
```

- `datasetType` is the uppercase form of the upload path's dataset segment: `ROSTER`, `MARKS`, `FEE_SCHEDULE` (and `DOCUMENTS` on `ImportFailed`).
- `importId` is derived from the bucket, key and S3 `versionId`, so redelivery of the same upload never produces a second import, while re-uploading an identical file does.
- `status` is `COMPLETED` or `COMPLETED_WITH_ERRORS`; `validRows` is always at least 1 (an import with no valid rows publishes `ImportFailed` instead).
- `validRecordsKey` and `summaryKey` are claim-check files and expire after 45 days (see Data rules).

Subscribers filter on `datasetType` in their rule pattern: **records** consumes `ROSTER`; **gradebook** consumes `MARKS`; **finance** consumes `FEE_SCHEDULE` (as a draft schedule a bursar must review and publish). **Notifications** tells the uploader the outcome. **Analytics** records import volume.

**`ImportFailed`**

```json
"data": {
  "importId": "imp-2d81c0f4a9e6",
  "datasetType": "MARKS",
  "sourceFileKey": "raw/marks/SCH-001/term4.csv",
  "sourceVersionId": "Ys8wq0Jt2vXzT1nKe4QpLm7aRbC9dF0u",
  "reasonCode": "HEADER_MISMATCH",
  "summaryKey": "processed/marks/SCH-001/imp-2d81c0f4a9e6/summary.json",
  "uploadedBy": "USR-teacher-0042"
}
```

| `reasonCode` | Meaning |
|---|---|
| `UNSUPPORTED_FORMAT` | Unknown dataset type, invalid school ID in the key, an extension that doesn't belong to the dataset, or (documents) content that doesn't match the extension. |
| `FILE_TOO_LARGE` | Above the dataset's size limit (20 MB tabular, 25 MB documents in v1). |
| `HEADER_MISMATCH` | CSV header doesn't contain exactly the schema's columns. |
| `MISSING_METADATA` | Missing `uploaded-by` metadata, or (documents) missing or invalid `audience`. |
| `UNREADABLE` | Not valid UTF-8, malformed JSON/JSONL anywhere in the file, or the object version was deleted before processing. |
| `NO_VALID_ROWS` | No data rows, or every row failed validation; the summary lists the errors. |
| `MOVED_TO_DLQ` | Reserved for phase 2: the file failed processing repeatedly and was moved to the DLQ. |

- `datasetType` is `UNKNOWN` when the key's dataset segment isn't a supported type; `metadata.schoolId` is `UNKNOWN` when the key's school ID is invalid. In those cases `summaryKey` is under `processed/_rejected/<importId>/`.
- `uploadedBy` is `null` when that metadata was missing.

Subscribers: **notifications** alerts the uploader, and the admin group when `uploadedBy` is `null` or the reason is `MOVED_TO_DLQ`; **analytics** records failure reasons.

**`DocumentAccepted`**

Published for the `documents` dataset (PDF, DOCX, Markdown, plain text) instead of `ImportCompleted`. Documents are not row data, so ingestion only checks type, size and required metadata, then stages an immutable copy for the knowledge service.

```json
"data": {
  "documentId": "doc-4be1a07c93d2",
  "version": "v-9f2c41e07ab3",
  "importId": "imp-1c7d0e93fa21",
  "title": "Learner Code of Conduct 2027",
  "audience": ["STAFF", "PARENT"],
  "contentType": "application/pdf",
  "sizeBytes": 1843200,
  "sha256": "9f2c41e07ab3…",
  "sourceFileKey": "raw/documents/SCH-001/policies/code_of_conduct.pdf",
  "sourceVersionId": "kP2vR9tQx4LmZ7wYc0nA3bJ8sE5fH1dG",
  "stagedKey": "processed/documents/SCH-001/doc-4be1a07c93d2/v-9f2c41e07ab3/source.pdf",
  "uploadedBy": "USR-admin-0003"
}
```

- `documentId` is stable across re-uploads of the same path, so uploading a new handbook replaces the old one rather than creating a duplicate.
- `version` is derived from the file's content hash.
- `title` and `audience` come from S3 object metadata; a missing `audience` → `ImportFailed` with `MISSING_METADATA`.

- `stagedKey` is a claim-check copy that expires after 45 days. The knowledge service owns documents once accepted, so it copies the source into its own storage as part of indexing (it needs the source again for re-indexing, e.g. after an embedding model change).

Subscribers: **knowledge** copies and indexes the document.

### Finance — `school.finance`

Fees (schedules, discounts, invoices, payments) and payroll. It wraps the existing `FinancialLedgerAPI`, which serves as the cash book: only real money movements are posted there, the ledger publishes nothing itself, and no other service calls it or reads its table. Payment-provider webhooks are received and verified by the finance service (provider signature plus a server-side verification call) before anything is posted.

All amounts are integer minor units (the `…Cents` fields) in the school's currency (`ZAR` in v1). **Salaries, deductions, payslip contents, bank details and discount reasons are never in events.**

**`InvoiceIssued`**

```json
"data": {
  "invoiceId": "INV-01JD2K7R4M",
  "accountId": "ACC-STU-2026-00412",
  "studentId": "STU-2026-00412",
  "termCode": "2027-T1",
  "billingRunId": "BRUN-01JD2H9X0C",
  "grossCents": 1450000,
  "discountCents": 145000,
  "amountCents": 1305000,
  "currency": "ZAR",
  "dueDate": "2027-02-14"
}
```

- `amountCents` is the net amount due (gross minus discounts), kept under this name for compatibility.
- `discountCents` is the total only. Which discounts applied (sibling, staff child, bursary) is never published: it reveals family composition and employment.
- `billingRunId` is `null` for an invoice issued outside a billing run (e.g. a late addition issued individually).

Subscribers: **notifications** sends the invoice to the billing contact; **analytics** tracks billed amounts.

**`CreditNoteIssued`** — `creditNoteId`, `invoiceId`, `accountId`, `studentId`, `amountCents`, `currency`.
A correction to an issued invoice (invoices are never edited). Subscribers: **notifications** tells the billing contact; **analytics**.

**`PaymentSettled`**

```json
"data": {
  "paymentId": "PAY-01JC8Z9Q",
  "accountId": "ACC-STU-2026-00412",
  "studentId": "STU-2026-00412",
  "amountCents": 125000,
  "currency": "ZAR",
  "method": "PAYSTACK",
  "provider": "PAYSTACK",
  "providerReference": "T685312322670591",
  "balanceAfterCents": 0
}
```

- `method` is `PAYSTACK`, `CASH`, `BANK_DEPOSIT`, `MOBILE_MONEY` or `SALARY_DEDUCTION`.
- `provider` and `providerReference` are set for online payments only; otherwise `null`. Manual payments' external references stay in the finance service.
- For `SALARY_DEDUCTION`, the event does not identify the staff member whose salary paid it.
- Published once the payment is posted to the ledger (where cash moved) and allocated to invoices.

Subscribers: **notifications** sends a receipt; **analytics** updates collections data.

**`PaymentFailed`** — `paymentId`, `accountId`, `studentId`, `amountCents`, `currency`, `provider`, `failureCode`.
Subscribers: **notifications** asks the billing contact to retry; **analytics**.

**`InvoiceOverdue`** — `invoiceId`, `accountId`, `studentId`, `outstandingCents`, `currency`, `daysOverdue`.
Produced by a daily EventBridge Scheduler job inside finance, **only on the configured reminder days** (default 7, 14 and 30 days past due), once per invoice per threshold. Subscribers: **notifications** sends a reminder; **analytics** tracks arrears.

**`PayrollRunApproved`** — `runId`, `period` (`YYYY-MM`), `staffCount`, `approvedBy`.
A `finance_approver` (never the person who submitted the run) approved a payroll run; its payslips are now frozen. No amounts. Subscribers: **analytics**.

**`PayslipsPublished`** — `runId`, `period`, `staffIds[]`.
The run is paid (recorded as paid outside the system by default, or paid out through a provider when that mode is enabled) and each staff member can view their payslip through the finance API. No amounts. Subscribers: **notifications** tells each staff member their payslip is available.

**`PayoutFailed`** — `runId`, `staffId`, `failureCode`.
Only in the optional automated-payout mode: one staff member's transfer failed and needs manual handling. Never retried automatically. Subscribers: **notifications** alerts the finance roles only (not the staff member automatically); **analytics**.

**`LedgerPostingRejected`** — `postingId`, `reasonCode` (`INSUFFICIENT_FUNDS`, `VALIDATION_FAILED`, `SIGNATURE_REJECTED`).
The ledger refused a cash posting (e.g. a record-only salary payment while the recorded bank balance is too low). Operations are alerted by the finance service's own alarms. Subscribers: **analytics**.

### Gradebook — `school.gradebook`

**`MarksRecorded`** — `assessmentId`, `classCode`, `termCode`, `recordCount`, `importId` (when marks came from a bulk import).
Subscribers: **analytics**.

**`ReportCardsPublished`** — `termCode`, `classCode`, `studentIds[]`, `publishedBy`.
Subscribers: **notifications** tells each guardian report cards are available. Download links are presigned on request, never embedded in the event, because presigned URLs expire.

**`FeedbackDraftsReady`** — `assessmentId`, `teacherId`, `draftCount`.
Raised when the AI-assisted feedback job finishes. Subscribers: **notifications** tells the teacher drafts are ready for review.

### Knowledge (RAG) — `school.knowledge`

Indexes school documents and answers questions grounded in them. Owns the document registry and the vector index. `audience` is an authorization boundary: a parent's question is only ever answered from chunks of documents whose audience includes `PARENT`.

**`DocumentIndexed`**

```json
"data": {
  "documentId": "doc-4be1a07c93d2",
  "version": "v-9f2c41e07ab3",
  "replacedVersion": "v-31aa8c5d0e7f",
  "importId": "imp-1c7d0e93fa21",
  "title": "Learner Code of Conduct 2027",
  "audience": ["STAFF", "PARENT"],
  "chunkCount": 46,
  "embeddingModel": "<provider>/<model>",
  "embeddingDimension": 768,
  "indexName": "docs-gemini-768-v1"
}
```

`replacedVersion` is `null` for a first upload. Subscribers: **notifications** tells the uploader the document is live; **analytics**.

**`DocumentIndexingFailed`** — `documentId`, `version`, `importId`, `reasonCode` (`NO_EXTRACTABLE_TEXT`, `EXTRACTION_FAILED`, `EMBEDDING_PROVIDER_ERROR`, `VECTOR_WRITE_FAILED`).
`NO_EXTRACTABLE_TEXT` usually means a scanned PDF (OCR is out of scope for v1). Subscribers: **notifications** alerts the uploader, and the admin group for provider or vector-store errors; **analytics**.

**`DocumentRetired`** — `documentId`, `version`, `retiredBy`.
An admin removed a document from search; its vectors are deleted. Subscribers: **analytics**.

**`QuestionAnswered`**

```json
"data": {
  "queryId": "qry-01JCB2M4N8",
  "askerId": "USR-parent-0217",
  "askerRole": "PARENT",
  "outcome": "ANSWERED",
  "chunksRetrieved": 6,
  "citedDocumentIds": ["doc-4be1a07c93d2"],
  "topScore": 0.83,
  "latencyMs": 2140,
  "embeddingModel": "<provider>/<model>",
  "generationProvider": "CLAUDE",
  "generationModel": "<model>",
  "inputTokens": 3120,
  "outputTokens": 214
}
```

`outcome` is `ANSWERED`, `NO_RELEVANT_SOURCES` or `PROVIDER_ERROR`. **The question and answer text are never in the event:** people type names and personal details into questions. Subscribers: **analytics** (answer rate, cost per question, documents that are never cited, gaps where questions find no sources).

**`AnswerFeedbackGiven`** — `queryId`, `helpful` (bool), `reasonCode` (optional: `WRONG`, `INCOMPLETE`, `NOT_RELEVANT`, `OUTDATED`).
Subscribers: **analytics**.

### Notifications — `school.notifications`

**`NotificationDelivered`** / **`NotificationFailed`** — `notificationId`, `channel` (`EMAIL`, `SMS`), `templateId`, `recipientId`, `triggerEventId`, and `failureCode` on failure.
Subscribers: **analytics** (delivery rates). Optional for v1.

## 3. Subscription matrix

Rows are events, columns are consuming services.

| Event | Records | Finance | Gradebook | Knowledge | Notifications | Analytics |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| StudentEnrolled | | ✓ | ✓ | | ✓ | ✓ |
| StudentWithdrawn | | ✓ | ✓ | | ✓ | ✓ |
| ClassAssignmentChanged | | ✓ | ✓ | | | |
| GuardianLinked | | ✓ | | | ✓ | |
| ImportCompleted (ROSTER) | ✓ | | | | ✓ | ✓ |
| ImportCompleted (MARKS) | | | ✓ | | ✓ | ✓ |
| ImportCompleted (FEE_SCHEDULE) | | ✓ | | | ✓ | ✓ |
| ImportFailed | | | | | ✓ | ✓ |
| DocumentAccepted | | | | ✓ | | ✓ |
| DocumentIndexed | | | | | ✓ | ✓ |
| DocumentIndexingFailed | | | | | ✓ | ✓ |
| DocumentRetired | | | | | | ✓ |
| QuestionAnswered | | | | | | ✓ |
| AnswerFeedbackGiven | | | | | | ✓ |
| InvoiceIssued | | | | | ✓ | ✓ |
| CreditNoteIssued | | | | | ✓ | ✓ |
| PaymentSettled | | | | | ✓ | ✓ |
| PaymentFailed | | | | | ✓ | ✓ |
| InvoiceOverdue | | | | | ✓ | ✓ |
| PayrollRunApproved | | | | | | ✓ |
| PayslipsPublished | | | | | ✓ | |
| PayoutFailed | | | | | ✓ | ✓ |
| LedgerPostingRejected | | | | | | ✓ |
| MarksRecorded | | | | | | ✓ |
| ReportCardsPublished | | | | | ✓ | |
| FeedbackDraftsReady | | | | | ✓ | |

Ingestion consumes no events; it is driven by S3 uploads.

Analytics can subscribe with a single catch-all rule (`source` prefix `school.`) that delivers everything to Firehose, which writes Parquet to the analytics bucket.

## 4. Example end-to-end flows

**Term marks upload.** Teacher uploads `raw/marks/SCH-001/term4.csv` → ingestion validates and writes `valid.jsonl` → `ImportCompleted (MARKS)` → gradebook loads marks and emits `MarksRecorded` → notifications emails the teacher the import summary.

**New learner joins.** Admin uploads a roster file → `ImportCompleted (ROSTER)` → records creates students and emits `StudentEnrolled` per student → finance opens fee accounts; at the term's billing run it computes each student's fees and discounts and emits `InvoiceIssued` → notifications emails guardians.

**Fee payment.** Parent starts a payment in the portal → Paystack webhook hits the finance service → signature verified and the transaction confirmed with Paystack's verify API → finance posts the cash movement to the ledger (idempotent on the posting ID) and allocates the payment to the oldest open invoice → `PaymentSettled` → notifications sends the receipt; the ledger's CDC stream archives the cash record to S3 as Parquet.

**Monthly payroll (record-only, the default).** Bursar drafts the run → finance calculates payslips from pay profiles and the school's configured deduction rules → bursar submits → a different user approves → `PayrollRunApproved` → the school pays staff through its bank as usual and the bursar records the run as paid → finance posts each net payment to the ledger and records any salary-deduction fee payments → `PayslipsPublished` → notifications tells each staff member their payslip is ready.

**New school policy.** Admin uploads `raw/documents/SCH-001/policies/code_of_conduct.pdf` with title and audience metadata → ingestion validates and stages it → `DocumentAccepted` → knowledge copies the staged source into its own storage, then extracts, chunks, embeds and writes vectors, then deletes the previous version's vectors → `DocumentIndexed` → notifications tells the admin it is live. A parent then asks "What happens if my child is late three times?" → the answer cites the policy and page; `QuestionAnswered` goes to analytics.

## 5. Governance

- Each event's JSON Schema lives in `schemas/<source>/<DetailType>.v<major>.json` in the monorepo and is registered in the EventBridge Schema Registry. CI validates every published example against its schema.
- The owning service's team (you, for now) is the only one allowed to change a schema. Consumers ask; they don't edit.
- Add a new event to this catalogue before writing the code that publishes it.
