"""Opt-in: the outbox pipe on real AWS (``pytest -m integration``).

Writes an outbox item into the deployed Finance table, exactly as a business change
would, and waits for the event on SchoolEventBus: the stream, the pipe's filter, the
publisher Lambda and PutEvents, end to end. School SCH-999.
"""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from botocore.exceptions import ClientError

from finance.outbox import new_event, outbox_put
from tests.integration.capture import capture_events

pytestmark = pytest.mark.integration

SCHOOL = "SCH-999"
TIMEOUT_SECONDS = 120


def test_outbox_item_is_published_with_its_own_detail_type() -> None:
    try:
        outputs = boto3.client("cloudformation").describe_stacks(StackName="SchoolFinanceStack")[
            "Stacks"
        ][0]
    except ClientError as exc:
        pytest.skip(f"finance stack not available: {exc.response['Error']['Code']}")
    table = next(
        o["OutputValue"] for o in outputs["Outputs"] if o["OutputKey"] == "FinanceTableName"
    )

    now = datetime.now(UTC)
    event = new_event(
        "PayrollRunApproved",
        school_id=SCHOOL,
        correlation_id="integration-test",
        data={
            "runId": "PRUN-INTEGRATION",
            "period": now.strftime("%Y-%m"),
            "staffCount": 2,
            "approvedBy": "USR-it",
        },
        now=now,
    )
    with capture_events(source="school.finance", school_id=SCHOOL) as capture:
        put = outbox_put(table, event, now)["Put"]
        try:
            boto3.client("dynamodb").put_item(
                TableName=put["TableName"],
                Item=put["Item"],
                ConditionExpression=put["ConditionExpression"],
            )
        except ClientError as exc:
            pytest.skip(f"cannot write to the Finance table: {exc.response['Error']['Code']}")
        received = capture.wait_for(
            lambda e: e["detail"]["metadata"].get("eventId") == event.event_id,
            timeout=TIMEOUT_SECONDS,
            what=f"outbox event {event.event_id}",
        )

    assert received["source"] == "school.finance"
    assert received["detail-type"] == "PayrollRunApproved"  # taken from the record
    assert received["detail"] == event.detail  # a JSON object, unchanged
