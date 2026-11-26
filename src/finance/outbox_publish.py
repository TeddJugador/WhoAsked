"""The outbox pipe's target: stream records -> PutEvents on SchoolEventBus.

The pipe reads the Finance table's stream, keeps only new OUTBOX# items, and invokes
this synchronously with each batch. Each record is parsed and re-checked against the
catalogue (outbox_enrich.unwrap), then published with its detail-type set explicitly.

Why a Lambda target rather than the bus directly: Pipes' EventBridge target can't be
relied on to take each record's detail-type from enrichment output (on real AWS every
publish failed with no error detail). Publishing here makes the step ordinary,
testable code, and an error is logged with its code.

Any failure raises, so the pipe retries the batch and finally dead-letters it (which
alarms). Entries that already succeeded may then be published again: delivery is at
least once, and consumers deduplicate on eventId.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from finance.events import SOURCE
from finance.outbox_enrich import unwrap

MAX_ENTRIES_PER_CALL = 10  # PutEvents limit


class PublishError(RuntimeError):
    """EventBridge rejected some entries. The message names error codes, never data."""


def entries(records: Sequence[Mapping[str, Any]], bus_name: str) -> list[dict[str, Any]]:
    result = []
    for record in records:
        event = unwrap(record)
        result.append(
            {
                "Source": SOURCE,
                "DetailType": event["detailType"],
                "Detail": json.dumps(event["detail"], separators=(",", ":")),
                "EventBusName": bus_name,
            }
        )
    return result


def publish(client: Any, records: Sequence[Mapping[str, Any]], bus_name: str) -> list[str]:  # noqa: ANN401 - boto3 events client
    """Publish every record; returns the detail-types published, in order."""
    all_entries = entries(records, bus_name)
    for start in range(0, len(all_entries), MAX_ENTRIES_PER_CALL):
        batch = all_entries[start : start + MAX_ENTRIES_PER_CALL]
        response = client.put_events(Entries=batch)
        if response.get("FailedEntryCount", 0):
            codes = sorted(
                {e.get("ErrorCode", "unknown") for e in response["Entries"] if "ErrorCode" in e}
            )
            failed = response["FailedEntryCount"]
            raise PublishError(f"PutEvents rejected {failed} of {len(batch)} entries: {codes}")
    return [e["DetailType"] for e in all_entries]
