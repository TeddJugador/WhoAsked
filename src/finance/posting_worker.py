"""Sends one posting to the ledger and records the outcome (spec 7.3).

- POSTED: the ledger applied it, or had already (a retry): no double-post either way.
- REJECTED_INSUFFICIENT_FUNDS / REJECTED: permanent. The status, the reason code and a
  LedgerPostingRejected outbox event are written in one transaction, and the
  PostingRejected metric (which alarms) is emitted.
- The ledger unavailable: LedgerUnavailableError propagates, so the SQS message is
  retried and finally dead-lettered.

The status changes only from PENDING, guarded by the item's version, so a message
delivered twice, even concurrently, records one outcome. Logs carry posting IDs,
statuses and reason codes, never amounts (payroll postings are salaries).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from finance.events import iso_ms
from finance.ledger.port import LedgerPort, TransferOutcome
from finance.metrics import MetricsSink
from finance.outbox import new_event, outbox_put
from finance.postings import Posting, PostingStatus, parse_posting, posting_key, status_index

REJECTIONS = {
    TransferOutcome.INSUFFICIENT_FUNDS: (
        PostingStatus.REJECTED_INSUFFICIENT_FUNDS,
        "INSUFFICIENT_FUNDS",
    ),
    TransferOutcome.VALIDATION_FAILED: (PostingStatus.REJECTED, "VALIDATION_FAILED"),
    TransferOutcome.SIGNATURE_REJECTED: (PostingStatus.REJECTED, "SIGNATURE_REJECTED"),
}


class PostingMissingError(RuntimeError):
    """The stream announced a posting the table doesn't have."""


@dataclass
class PostingWorker:
    dynamodb: Any  # boto3 DynamoDB client
    table_name: str
    ledger: LedgerPort
    metrics: MetricsSink
    logger: Logger
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def process(self, school_id: str, posting_id: str) -> PostingStatus:
        posting = self._load(school_id, posting_id)
        if posting.status is not PostingStatus.PENDING:
            self.logger.info("posting already settled", status=str(posting.status))
            return posting.status

        outcome = self.ledger.transfer(posting.transfer())  # unavailable -> raises, SQS retries
        if outcome in (TransferOutcome.APPLIED, TransferOutcome.ALREADY_APPLIED):
            status, reason = PostingStatus.POSTED, None
        else:
            status, reason = REJECTIONS[outcome]

        now = self.now()
        stamp = iso_ms(now)
        names = {"#s": "status"}
        values: dict[str, Any] = {
            ":new": {"S": status.value},
            ":pending": {"S": PostingStatus.PENDING.value},
            ":v": {"N": str(posting.version)},
            ":next": {"N": str(posting.version + 1)},
            ":now": {"S": stamp},
            ":outcome": {"S": outcome.value},
            ":gpk": status_index(school_id, status, stamp, posting_id)["GSI4PK"],
            ":gsk": status_index(school_id, status, stamp, posting_id)["GSI4SK"],
        }
        update = (
            "SET #s = :new, version = :next, updatedAt = :now, ledgerOutcome = :outcome, "
            "GSI4PK = :gpk, GSI4SK = :gsk"
        )
        if reason:
            update += ", reasonCode = :reason"
            values[":reason"] = {"S": reason}
        items: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": self.table_name,
                    "Key": posting_key(school_id, posting_id),
                    "UpdateExpression": update,
                    "ConditionExpression": "#s = :pending AND version = :v",
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": values,
                }
            }
        ]
        if reason:
            event = new_event(
                "LedgerPostingRejected",
                school_id=school_id,
                correlation_id=posting_id,
                data={"postingId": posting_id, "reasonCode": reason},
                now=now,
            )
            items.append(outbox_put(self.table_name, event, now))
        try:
            self.dynamodb.transact_write_items(TransactItems=items)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            # Another delivery recorded the outcome first: report what it recorded.
            settled = self._load(school_id, posting_id).status
            self.logger.info("posting settled concurrently", status=str(settled))
            return settled

        if reason:
            self.metrics.count("PostingRejected", {"Reason": reason})
            self.logger.warning(
                "posting rejected by the ledger", status=str(status), reason_code=reason
            )
        else:
            self.metrics.count("PostingPosted")
            self.logger.info("posting posted", ledger_outcome=outcome.value)
        return status

    def _load(self, school_id: str, posting_id: str) -> Posting:
        item = self.dynamodb.get_item(
            TableName=self.table_name, Key=posting_key(school_id, posting_id), ConsistentRead=True
        ).get("Item")
        if item is None:
            raise PostingMissingError(posting_id)
        return parse_posting(item)
