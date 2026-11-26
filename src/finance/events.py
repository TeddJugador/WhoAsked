"""The finance events in docs/EVENT_CATALOGUE.md ("Finance — school.finance"), exactly.

Every event's ``data`` must have exactly the catalogue's fields: an extra field fails,
so salaries, deductions, bank details and discount reasons can't reach the bus by
accident, and a missing field fails, so consumers always get the documented shape.
Money is integer cents; a float anywhere in an event is refused.

Standard library only: the outbox pipe's enrichment Lambda imports this module and
runs without a dependencies layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

SOURCE = "school.finance"
PRODUCER = "finance-service"
SCHEMA_VERSION = "1.0"


class DetailType(StrEnum):
    INVOICE_ISSUED = "InvoiceIssued"
    CREDIT_NOTE_ISSUED = "CreditNoteIssued"
    PAYMENT_SETTLED = "PaymentSettled"
    PAYMENT_FAILED = "PaymentFailed"
    INVOICE_OVERDUE = "InvoiceOverdue"
    PAYROLL_RUN_APPROVED = "PayrollRunApproved"
    PAYSLIPS_PUBLISHED = "PayslipsPublished"
    PAYOUT_FAILED = "PayoutFailed"
    LEDGER_POSTING_REJECTED = "LedgerPostingRejected"


CATALOGUE: dict[DetailType, frozenset[str]] = {
    DetailType.INVOICE_ISSUED: frozenset(
        {
            "invoiceId",
            "accountId",
            "studentId",
            "termCode",
            "billingRunId",
            "grossCents",
            "discountCents",
            "amountCents",
            "currency",
            "dueDate",
        }
    ),
    DetailType.CREDIT_NOTE_ISSUED: frozenset(
        {"creditNoteId", "invoiceId", "accountId", "studentId", "amountCents", "currency"}
    ),
    DetailType.PAYMENT_SETTLED: frozenset(
        {
            "paymentId",
            "accountId",
            "studentId",
            "amountCents",
            "currency",
            "method",
            "provider",
            "providerReference",
            "balanceAfterCents",
        }
    ),
    DetailType.PAYMENT_FAILED: frozenset(
        {
            "paymentId",
            "accountId",
            "studentId",
            "amountCents",
            "currency",
            "provider",
            "failureCode",
        }
    ),
    DetailType.INVOICE_OVERDUE: frozenset(
        {"invoiceId", "accountId", "studentId", "outstandingCents", "currency", "daysOverdue"}
    ),
    DetailType.PAYROLL_RUN_APPROVED: frozenset({"runId", "period", "staffCount", "approvedBy"}),
    DetailType.PAYSLIPS_PUBLISHED: frozenset({"runId", "period", "staffIds"}),
    DetailType.PAYOUT_FAILED: frozenset({"runId", "staffId", "failureCode"}),
    DetailType.LEDGER_POSTING_REJECTED: frozenset({"postingId", "reasonCode"}),
}

POSTING_REJECTION_CODES = frozenset(
    {"INSUFFICIENT_FUNDS", "VALIDATION_FAILED", "SIGNATURE_REJECTED"}
)
PAYMENT_METHODS = frozenset(
    {"PAYSTACK", "CASH", "BANK_DEPOSIT", "MOBILE_MONEY", "SALARY_DEDUCTION"}
)


class EventSchemaError(ValueError):
    """An event doesn't match the catalogue. Messages name fields, never values."""


def _no_floats(value: Any, path: str) -> None:  # noqa: ANN401 - any JSON value
    if isinstance(value, float):
        raise EventSchemaError(f"{path} is a float; money and counts are integers")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _no_floats(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _no_floats(item, f"{path}[{index}]")


def check_data(detail_type: str, data: Mapping[str, Any]) -> DetailType:
    """Raise unless ``data`` has exactly the catalogue's fields for ``detail_type``."""
    try:
        kind = DetailType(detail_type)
    except ValueError:
        raise EventSchemaError(f"{detail_type!r} is not a finance event") from None
    expected = CATALOGUE[kind]
    extra, missing = set(data) - expected, expected - set(data)
    if extra:
        raise EventSchemaError(f"{kind}: fields not in the catalogue: {sorted(extra)}")
    if missing:
        raise EventSchemaError(f"{kind}: missing fields: {sorted(missing)}")
    _no_floats(dict(data), "data")
    for field, value in data.items():
        if field.endswith("Cents") and (not isinstance(value, int) or isinstance(value, bool)):
            raise EventSchemaError(f"{kind}: {field} must be an integer number of cents")
    if (
        kind is DetailType.LEDGER_POSTING_REJECTED
        and data["reasonCode"] not in POSTING_REJECTION_CODES
    ):
        raise EventSchemaError(f"{kind}: unknown reasonCode")
    if kind is DetailType.PAYMENT_SETTLED and data["method"] not in PAYMENT_METHODS:
        raise EventSchemaError(f"{kind}: unknown payment method")
    return kind


def iso_ms(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_detail(
    detail_type: str,
    *,
    event_id: str,
    school_id: str,
    correlation_id: str,
    occurred_at: datetime,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    """The event ``detail``: the catalogue's metadata block plus checked ``data``."""
    check_data(detail_type, data)
    return {
        "metadata": {
            "eventId": event_id,
            "schemaVersion": SCHEMA_VERSION,
            "occurredAt": iso_ms(occurred_at),
            "correlationId": correlation_id,
            "schoolId": school_id,
            "producer": PRODUCER,
        },
        "data": dict(data),
    }
