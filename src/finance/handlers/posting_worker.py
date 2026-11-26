"""SQS-triggered posting worker (queue ledger-postings, batch size 1). Wiring only."""

from __future__ import annotations

import json
import os
from functools import cache
from typing import Any

import boto3
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
from aws_lambda_powertools.utilities.typing import LambdaContext

from finance.ledger.http import HttpLedger
from finance.ledger.secrets import load_signing_key
from finance.metrics import PowertoolsMetrics
from finance.posting_worker import PostingWorker
from finance.postings import POSTING_ITEM_TYPE

logger = Logger()
tracer = Tracer()
processor = BatchProcessor(event_type=EventType.SQS)


class InvalidPostingMessageError(ValueError):
    pass


def posting_reference(body: str) -> tuple[str, str]:
    """The postings pipe forwards the whole DynamoDB stream record: (schoolId, postingId)."""
    try:
        image = json.loads(body)["dynamodb"]["NewImage"]
        if image["itemType"]["S"] != POSTING_ITEM_TYPE:
            raise InvalidPostingMessageError("not a posting")
        return image["schoolId"]["S"], image["postingId"]["S"]
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidPostingMessageError(
            f"unrecognised posting message ({type(exc).__name__})"
        ) from None


@cache
def worker() -> PostingWorker:
    env = os.environ
    secret_name = env["LEDGER_SIGNING_SECRET_NAME"]
    return PostingWorker(
        dynamodb=boto3.client("dynamodb"),
        table_name=env["FINANCE_TABLE_NAME"],
        ledger=HttpLedger(env["LEDGER_API_URL"], key_loader=lambda: load_signing_key(secret_name)),
        metrics=PowertoolsMetrics(),
        logger=logger,
    )


def record_handler(record: SQSRecord) -> None:
    school_id, posting_id = posting_reference(record.body)
    logger.set_correlation_id(posting_id)
    logger.append_keys(school_id=school_id)
    worker().process(school_id, posting_id)


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return dict(
        process_partial_response(
            event=event, record_handler=record_handler, processor=processor, context=context
        )
    )
