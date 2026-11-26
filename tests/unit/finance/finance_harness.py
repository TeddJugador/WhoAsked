"""moto-backed Finance, FinanceAudit and ledger tables for finance unit tests."""

from __future__ import annotations

import io
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from aws_lambda_powertools import Logger
from moto import mock_aws

from tests.unit.aws import REGION, aws_environment

FINANCE_TABLE = "Finance"
AUDIT_TABLE = "FinanceAudit"
LEDGER_TABLE = "FinancialLedger"
NOW = datetime(2027, 2, 1, 8, 30, tzinfo=UTC)


@dataclass
class RecordedMetrics:
    counts: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def count(self, name: str, dimensions: dict[str, str] | None = None) -> None:
        self.counts.append((name, dict(dimensions or {})))


@dataclass
class FinanceHarness:
    dynamodb: Any
    metrics: RecordedMetrics
    logger: Logger
    log_stream: io.StringIO

    def item(self, table: str, pk: str, sk: str) -> dict[str, Any] | None:
        return self.dynamodb.get_item(TableName=table, Key={"PK": {"S": pk}, "SK": {"S": sk}}).get(
            "Item"
        )

    def scan(self, table: str) -> list[dict[str, Any]]:
        return list(self.dynamodb.scan(TableName=table)["Items"])

    def outbox(self) -> list[dict[str, Any]]:
        return [i for i in self.scan(FINANCE_TABLE) if i["PK"]["S"].startswith("OUTBOX#")]

    def logs(self) -> str:
        return self.log_stream.getvalue()


def _table(client: Any, name: str) -> None:
    client.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
    )


@pytest.fixture
def fh(monkeypatch: pytest.MonkeyPatch) -> Iterator[FinanceHarness]:
    aws_environment(monkeypatch)
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        for name in (FINANCE_TABLE, AUDIT_TABLE, LEDGER_TABLE):
            _table(client, name)
        stream = io.StringIO()
        yield FinanceHarness(
            dynamodb=client,
            metrics=RecordedMetrics(),
            logger=Logger(
                service=f"finance-test-{uuid.uuid4().hex[:8]}",
                logger_handler=logging.StreamHandler(stream),
            ),
            log_stream=stream,
        )
