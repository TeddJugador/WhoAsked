"""Turning a DynamoDB stream record of an outbox item back into {detailType, detail}.

Used by the outbox publisher (outbox_publish.py) for every record the pipe delivers:
the stored detail (a JSON string, because the stream delivers DynamoDB-typed JSON) is
parsed and re-checked against the catalogue, so an item that is malformed, or that
bypassed new_event, is refused rather than published. A refusal raises, so the pipe
retries the batch and finally dead-letters it, which alarms.

Standard library only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from finance.events import PRODUCER, EventSchemaError, check_data


class OutboxRecordError(ValueError):
    """A stream record isn't a well-formed outbox item."""


def unwrap(record: Mapping[str, Any]) -> dict[str, Any]:
    try:
        image = record["dynamodb"]["NewImage"]
        detail_type = image["detailType"]["S"]
        detail = json.loads(image["detail"]["S"])
        metadata, data = detail["metadata"], detail["data"]
    except (KeyError, TypeError, ValueError) as exc:
        raise OutboxRecordError(f"malformed outbox record ({type(exc).__name__})") from None
    if not isinstance(metadata, dict) or not isinstance(data, dict):
        raise OutboxRecordError("outbox detail must hold metadata and data objects")
    if metadata.get("producer") != PRODUCER:
        raise OutboxRecordError("outbox detail has the wrong producer")
    try:
        check_data(detail_type, data)
    except EventSchemaError as exc:
        raise OutboxRecordError(str(exc)) from None
    return {"detailType": detail_type, "detail": detail}


def unwrap_batch(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [unwrap(record) for record in records]
