"""Transactional outbox: an event is written in the same TransactWriteItems as the change
that caused it, so no event is published for a change that didn't commit and no
committed change loses its event. An EventBridge Pipe publishes new outbox items from
the table's stream (see outbox_enrich.py and infra/constructs/finance/outbox_pipe.py).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ulid import ULID

from finance.events import build_detail

OUTBOX_PK_PREFIX = "OUTBOX#"
OUTBOX_SK = "EVENT"
OUTBOX_TTL = timedelta(days=7)  # published within seconds; kept a week for inspection


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    detail_type: str
    detail: dict[str, Any]


def new_event(
    detail_type: str,
    *,
    school_id: str,
    correlation_id: str,
    data: Mapping[str, Any],
    now: datetime,
    new_id: Callable[[], str] = lambda: str(ULID()),
) -> OutboxEvent:
    event_id = new_id()
    detail = build_detail(
        detail_type,
        event_id=event_id,
        school_id=school_id,
        correlation_id=correlation_id,
        occurred_at=now,
        data=data,
    )
    return OutboxEvent(event_id=event_id, detail_type=detail_type, detail=detail)


def outbox_put(table_name: str, event: OutboxEvent, now: datetime) -> dict[str, Any]:
    """The ``Put`` to include in the business change's TransactWriteItems."""
    return {
        "Put": {
            "TableName": table_name,
            "Item": {
                "PK": {"S": f"{OUTBOX_PK_PREFIX}{event.event_id}"},
                "SK": {"S": OUTBOX_SK},
                "detailType": {"S": event.detail_type},
                # A string, because the stream delivers DynamoDB-typed JSON; the pipe's
                # enrichment step parses it back into an object (see outbox_enrich.py).
                "detail": {"S": json.dumps(event.detail, separators=(",", ":"), sort_keys=True)},
                "expiresAt": {"N": str(int((now + OUTBOX_TTL).timestamp()))},
            },
            "ConditionExpression": "attribute_not_exists(PK)",
        }
    }
