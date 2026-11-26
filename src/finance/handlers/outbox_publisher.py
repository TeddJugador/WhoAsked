"""EventBridge Pipes target for the finance outbox (synchronous; batch in)."""

from __future__ import annotations

import logging
import os
from functools import cache
from typing import Any

import boto3

from finance.outbox_publish import publish

logger = logging.getLogger()
logger.setLevel(logging.INFO)


@cache
def _events() -> Any:  # noqa: ANN401 - boto3 client
    return boto3.client("events")


def handler(event: list[dict[str, Any]], context: Any) -> dict[str, int]:  # noqa: ANN401
    published = publish(_events(), event, os.environ["EVENT_BUS_NAME"])
    # Detail-types and counts only: event data stays out of logs.
    logger.info("published %d events: %s", len(published), sorted(set(published)))
    return {"published": len(published)}
