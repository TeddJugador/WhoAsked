from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from finance.events import (
    CATALOGUE,
    PRODUCER,
    DetailType,
    EventSchemaError,
    build_detail,
    check_data,
)
from finance.outbox import OUTBOX_TTL, new_event, outbox_put
from finance.outbox_enrich import OutboxRecordError, unwrap, unwrap_batch

NOW = datetime(2027, 2, 1, 8, 30, 0, 123000, tzinfo=UTC)

INVOICE: dict[str, Any] = {
    "invoiceId": "INV-01JD2K7R4M",
    "accountId": "ACC-STU-2026-00412",
    "studentId": "STU-2026-00412",
    "termCode": "2027-T1",
    "billingRunId": "BRUN-01JD2H9X0C",
    "grossCents": 1450000,
    "discountCents": 145000,
    "amountCents": 1305000,
    "currency": "ZAR",
    "dueDate": "2027-02-14",
}


# --- the catalogue --------------------------------------------------------------------


def test_every_catalogue_event_is_covered() -> None:
    """docs/EVENT_CATALOGUE.md lists exactly these nine finance events."""
    assert {str(t) for t in CATALOGUE} == {
        "InvoiceIssued",
        "CreditNoteIssued",
        "PaymentSettled",
        "PaymentFailed",
        "InvoiceOverdue",
        "PayrollRunApproved",
        "PayslipsPublished",
        "PayoutFailed",
        "LedgerPostingRejected",
    }


def test_catalogue_fields_match_the_document() -> None:
    """Every field named for a finance event in the catalogue document is in CATALOGUE,
    so the two can't drift apart."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[3] / "docs" / "EVENT_CATALOGUE.md").read_text(
        encoding="utf-8"
    )
    section = text.split("### Finance — `school.finance`", 1)[1].split("\n### ", 1)[0]
    for kind, fields in CATALOGUE.items():
        start = section.index(f"**`{kind}`**")
        nxt = min([i for i in (section.find("**`", start + 5),) if i > 0] or [len(section)])
        block = section[start:nxt]
        named = set(re.findall(r"`([a-z][A-Za-z]+)(?:\[\])?`", block)) | set(
            re.findall(r'"([a-z][A-Za-z]+)":', block)
        )
        named -= {"null"}
        assert named & fields == fields, f"{kind}: {sorted(fields - named)} not in the document"


def test_invoice_issued_matches_the_catalogue_example() -> None:
    assert check_data("InvoiceIssued", INVOICE) is DetailType.INVOICE_ISSUED


@pytest.mark.parametrize(
    ("extra", "value"),
    [
        ("discountReason", "STAFF_CHILD"),  # reveals employment
        ("discounts", [{"type": "SIBLING"}]),  # reveals family composition
        ("staffId", "USR-staff-01"),
    ],
)
def test_fields_outside_the_catalogue_are_refused(extra: str, value: Any) -> None:
    with pytest.raises(EventSchemaError, match="not in the catalogue") as excinfo:
        check_data("InvoiceIssued", {**INVOICE, extra: value})
    assert str(value) not in str(excinfo.value)  # names fields, never values


@pytest.mark.parametrize(
    ("detail_type", "data"),
    [
        (
            "PayrollRunApproved",
            {
                "runId": "PRUN-1",
                "period": "2027-02",
                "staffCount": 12,
                "approvedBy": "u",
                "totalNetCents": 1,
            },
        ),
        (
            "PayslipsPublished",
            {"runId": "PRUN-1", "period": "2027-02", "staffIds": ["u1"], "netPayCents": [1]},
        ),
        (
            "PayoutFailed",
            {"runId": "PRUN-1", "staffId": "u1", "failureCode": "X", "bankAccount": "123"},
        ),
    ],
)
def test_payroll_events_cannot_carry_amounts_or_bank_details(
    detail_type: str, data: dict[str, Any]
) -> None:
    with pytest.raises(EventSchemaError):
        check_data(detail_type, data)


def test_missing_fields_are_refused() -> None:
    incomplete = {k: v for k, v in INVOICE.items() if k != "dueDate"}
    with pytest.raises(EventSchemaError, match="missing fields"):
        check_data("InvoiceIssued", incomplete)


@pytest.mark.parametrize("bad", [1305000.0, "1305000", True])
def test_money_must_be_integer_cents(bad: Any) -> None:
    with pytest.raises(EventSchemaError):
        check_data("InvoiceIssued", {**INVOICE, "amountCents": bad})


def test_no_floats_anywhere() -> None:
    data = {"runId": "PRUN-1", "period": "2027-02", "staffIds": ["u1"]}
    check_data("PayslipsPublished", data)
    with pytest.raises(EventSchemaError, match="float"):
        check_data(
            "PayrollRunApproved",
            {"runId": "r", "period": "2027-02", "staffCount": 1.0, "approvedBy": "u"},
        )


@pytest.mark.parametrize(
    ("detail_type", "field", "value"),
    [("LedgerPostingRejected", "reasonCode", "OOPS"), ("PaymentSettled", "method", "CHEQUE")],
)
def test_enumerated_codes(detail_type: str, field: str, value: str) -> None:
    base: dict[str, Any] = {
        "LedgerPostingRejected": {
            "postingId": "SCH-001:payment:PAY-1",
            "reasonCode": "INSUFFICIENT_FUNDS",
        },
        "PaymentSettled": {
            "paymentId": "PAY-1",
            "accountId": "ACC-1",
            "studentId": "STU-1",
            "amountCents": 100,
            "currency": "ZAR",
            "method": "CASH",
            "provider": None,
            "providerReference": None,
            "balanceAfterCents": 0,
        },
    }[detail_type]
    check_data(detail_type, base)
    with pytest.raises(EventSchemaError):
        check_data(detail_type, {**base, field: value})


def test_unknown_detail_types_are_refused() -> None:
    with pytest.raises(EventSchemaError, match="not a finance event"):
        check_data("InvoicePaidByMagic", {})


def test_detail_has_the_catalogue_envelope() -> None:
    detail = build_detail(
        "InvoiceIssued",
        event_id="01JD0000000000000000000000",
        school_id="SCH-001",
        correlation_id="BRUN-01JD2H9X0C",
        occurred_at=NOW,
        data=INVOICE,
    )
    assert detail == {
        "metadata": {
            "eventId": "01JD0000000000000000000000",
            "schemaVersion": "1.0",
            "occurredAt": "2027-02-01T08:30:00.123Z",
            "correlationId": "BRUN-01JD2H9X0C",
            "schoolId": "SCH-001",
            "producer": "finance-service",
        },
        "data": INVOICE,
    }


# --- outbox ------------------------------------------------------------------------------


def test_outbox_put_shape() -> None:
    event = new_event(
        "InvoiceIssued",
        school_id="SCH-001",
        correlation_id="BRUN-1",
        data=INVOICE,
        now=NOW,
        new_id=lambda: "01JDEVENT",
    )
    put = outbox_put("Finance", event, NOW)["Put"]
    assert put["TableName"] == "Finance"
    assert put["ConditionExpression"] == "attribute_not_exists(PK)"
    item = put["Item"]
    assert (item["PK"], item["SK"]) == ({"S": "OUTBOX#01JDEVENT"}, {"S": "EVENT"})
    assert item["detailType"] == {"S": "InvoiceIssued"}
    assert json.loads(item["detail"]["S"]) == event.detail
    assert int(item["expiresAt"]["N"]) == int((NOW + OUTBOX_TTL).timestamp())


def test_new_event_ids_are_ulids() -> None:
    event = new_event(
        "InvoiceIssued", school_id="SCH-001", correlation_id="c", data=INVOICE, now=NOW
    )
    assert len(event.event_id) == 26
    assert event.detail["metadata"]["eventId"] == event.event_id


def test_new_event_refuses_bad_data() -> None:
    with pytest.raises(EventSchemaError):
        new_event(
            "InvoiceIssued",
            school_id="SCH-001",
            correlation_id="c",
            data={**INVOICE, "reason": "x"},
            now=NOW,
        )


# --- the pipe's enrichment step ------------------------------------------------------------


def _stream_record(put: dict[str, Any]) -> dict[str, Any]:
    """What a DynamoDB stream delivers to the pipe for an INSERT of ``put``'s item."""
    item = put["Put"]["Item"]
    return {
        "eventName": "INSERT",
        "eventSource": "aws:dynamodb",
        "dynamodb": {
            "Keys": {"PK": item["PK"], "SK": item["SK"]},
            "NewImage": item,
            "StreamViewType": "NEW_IMAGE",
        },
    }


def test_enrichment_round_trips_an_outbox_item() -> None:
    event = new_event(
        "InvoiceIssued", school_id="SCH-001", correlation_id="c", data=INVOICE, now=NOW
    )
    (result,) = unwrap_batch([_stream_record(outbox_put("Finance", event, NOW))])
    assert result == {"detailType": "InvoiceIssued", "detail": event.detail}
    assert isinstance(result["detail"], dict)  # an object, which the pipe passes through as JSON


def test_enrichment_refuses_malformed_records() -> None:
    event = new_event(
        "InvoiceIssued", school_id="SCH-001", correlation_id="c", data=INVOICE, now=NOW
    )
    record = _stream_record(outbox_put("Finance", event, NOW))
    for broken in (
        {**record, "dynamodb": {}},
        {
            **record,
            "dynamodb": {
                **record["dynamodb"],
                "NewImage": {**record["dynamodb"]["NewImage"], "detail": {"S": "{not json"}},
            },
        },
    ):
        with pytest.raises(OutboxRecordError):
            unwrap(broken)


def test_enrichment_rechecks_the_catalogue() -> None:
    """An outbox item that bypassed new_event (written by hand, say) is still checked."""
    detail = build_detail(
        "InvoiceIssued",
        event_id="e",
        school_id="SCH-001",
        correlation_id="c",
        occurred_at=NOW,
        data=INVOICE,
    )
    detail["data"]["discountReason"] = "STAFF_CHILD"
    record = {
        "dynamodb": {
            "NewImage": {"detailType": {"S": "InvoiceIssued"}, "detail": {"S": json.dumps(detail)}}
        }
    }
    with pytest.raises(OutboxRecordError, match="not in the catalogue"):
        unwrap(record)
    detail["data"].pop("discountReason")
    detail["metadata"]["producer"] = "someone-else"
    record["dynamodb"]["NewImage"]["detail"] = {"S": json.dumps(detail)}
    with pytest.raises(OutboxRecordError, match="producer"):
        unwrap(record)
    assert PRODUCER == "finance-service"


# --- the pipe's target: the publisher --------------------------------------------------------


class _Events:
    """Stands in for the boto3 events client."""

    def __init__(self, fail_codes: list[str | None] | None = None) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self.fail_codes = fail_codes

    def put_events(self, Entries: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(Entries)
        if self.fail_codes is None:
            return {"FailedEntryCount": 0, "Entries": [{"EventId": "x"} for _ in Entries]}
        results = [
            {"ErrorCode": c, "ErrorMessage": "rejected"} if c else {"EventId": "x"}
            for c in self.fail_codes
        ]
        return {"FailedEntryCount": sum(1 for c in self.fail_codes if c), "Entries": results}


def _records(count: int) -> list[dict[str, Any]]:
    events = [
        new_event("InvoiceIssued", school_id="SCH-001", correlation_id="c", data=INVOICE, now=NOW)
        for _ in range(count)
    ]
    return [_stream_record(outbox_put("Finance", e, NOW)) for e in events]


def test_publisher_sets_source_detail_type_and_detail() -> None:
    from finance.outbox_publish import publish

    client = _Events()
    records = _records(1)
    assert publish(client, records, "SchoolEventBus") == ["InvoiceIssued"]
    (entry,) = client.calls[0]
    assert entry["Source"] == "school.finance"
    assert entry["DetailType"] == "InvoiceIssued"
    assert entry["EventBusName"] == "SchoolEventBus"
    detail = json.loads(entry["Detail"])
    assert detail["metadata"]["producer"] == "finance-service"
    assert detail["data"] == INVOICE


def test_publisher_sends_at_most_ten_entries_per_call() -> None:
    from finance.outbox_publish import publish

    client = _Events()
    publish(client, _records(23), "SchoolEventBus")
    assert [len(call) for call in client.calls] == [10, 10, 3]


def test_rejected_entries_fail_the_batch_with_codes_not_data() -> None:
    from finance.outbox_publish import PublishError, publish

    client = _Events(fail_codes=[None, "AccessDeniedException"])
    with pytest.raises(PublishError, match="AccessDeniedException") as excinfo:
        publish(client, _records(2), "SchoolEventBus")
    assert "1305000" not in str(excinfo.value)
    assert "STU-2026-00412" not in str(excinfo.value)


def test_malformed_records_are_never_published() -> None:
    from finance.outbox_publish import publish

    client = _Events()
    with pytest.raises(OutboxRecordError):
        publish(client, [{"dynamodb": {}}], "SchoolEventBus")
    assert client.calls == []


def test_publisher_handler_logs_types_and_counts_only(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from finance.handlers import outbox_publisher

    client = _Events()
    monkeypatch.setattr(outbox_publisher, "_events", lambda: client)
    monkeypatch.setenv("EVENT_BUS_NAME", "SchoolEventBus")
    with caplog.at_level("INFO"):
        result = outbox_publisher.handler(_records(2), None)
    assert result == {"published": 2}
    assert "InvoiceIssued" in caplog.text
    assert "1305000" not in caplog.text
    assert "STU-2026-00412" not in caplog.text
