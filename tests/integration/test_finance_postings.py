"""Opt-in: a posting through the real ledger (``pytest -m integration``).

Onboards test school SCH-999 into the deployed FinancialLedgerAPI (safe to repeat),
writes a R1.00 other-income posting (EXTERNAL -> CASH) into the Finance table as a
business change would, and waits for: the stream, the postings pipe, the
ledger-postings queue, the posting worker, the signed HTTPS call, and POSTED. Then reads
the ledger's own transaction record. Each run moves R1.00 into SCH-999's CASH account.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from ulid import ULID

from finance.ledger.accounts import cash, external
from finance.onboarding import onboard
from finance.postings import Posting, new_posting_put, other_income_posting_id

pytestmark = pytest.mark.integration

SCHOOL = "SCH-999"
TIMEOUT_SECONDS = 180
CONTEXT = json.loads(
    (Path(__file__).resolve().parents[2] / "cdk.json").read_text(encoding="utf-8")
)["context"]


def _outputs() -> dict[str, str]:
    stack = boto3.client("cloudformation").describe_stacks(StackName="SchoolFinanceStack")[
        "Stacks"
    ][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}


def test_a_posting_reaches_the_real_ledger() -> None:
    try:
        outputs = _outputs()
    except ClientError as exc:
        pytest.skip(f"finance stack not available: {exc.response['Error']['Code']}")
    if "FinanceAuditTableName" not in outputs:
        pytest.skip("deploy phase (c) first")
    dynamodb = boto3.client("dynamodb")
    finance_table, ledger_table = outputs["FinanceTableName"], CONTEXT["financeLedgerTableName"]
    now = datetime.now(UTC)
    try:
        onboard(
            dynamodb=dynamodb,
            school_id=SCHOOL,
            providers=("PAYSTACK",),
            currency="ZAR",
            ledger_table=ledger_table,
            finance_table=finance_table,
            audit_table=outputs["FinanceAuditTableName"],
            actor=boto3.client("sts").get_caller_identity()["Arn"],
            now=now,
        )
    except ClientError as exc:
        pytest.skip(f"cannot onboard the test school: {exc.response['Error']['Code']}")

    posting = Posting(
        school_id=SCHOOL,
        posting_id=other_income_posting_id(SCHOOL, f"IT-{ULID()}"),
        amount_minor=100,  # R1.00
        currency="ZAR",
        source=external(SCHOOL),
        destination=cash(SCHOOL),
    )
    put = new_posting_put(finance_table, posting, now)["Put"]
    dynamodb.put_item(
        TableName=finance_table, Item=put["Item"], ConditionExpression=put["ConditionExpression"]
    )

    status = _wait_for_status(dynamodb, finance_table, posting)
    assert status == "POSTED"

    transaction = dynamodb.get_item(
        TableName=ledger_table,
        Key={
            "PK": {"S": f"EVENT#{posting.posting_id}"},
            "SK": {"S": f"EVENT#{posting.posting_id}"},
        },
        ConsistentRead=True,
    )["Item"]
    # DynamoDB normalises numbers ("1.00" is stored as "1"): compare the value exactly.
    assert Decimal(transaction["amount"]["N"]) == Decimal("1.00")
    assert transaction["source_account"] == {"S": external(SCHOOL)}
    assert transaction["destination_account"] == {"S": cash(SCHOOL)}
    assert transaction["currency"] == {"S": "ZAR"}
    assert transaction["status"] == {"S": "COMPLETED"}


def _wait_for_status(dynamodb: Any, table: str, posting: Posting) -> str:
    key = {"PK": {"S": f"SCHOOL#{SCHOOL}#POSTING#{posting.posting_id}"}, "SK": {"S": "META"}}
    deadline = time.monotonic() + TIMEOUT_SECONDS
    status = "PENDING"
    while time.monotonic() < deadline:
        status = dynamodb.get_item(TableName=table, Key=key, ConsistentRead=True)["Item"]["status"][
            "S"
        ]
        if status != "PENDING":
            return str(status)
        time.sleep(3)
    pytest.fail(f"posting still {status} after {TIMEOUT_SECONDS}s: check the posting worker's logs")
