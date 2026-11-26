"""Ledger postings: one transfer each, recorded in the Finance table before it is sent.

A posting is written in the same TransactWriteItems as the business change that causes
it (a payment, a payslip...). A second pipe on the table's stream puts each new posting
on the ledger-postings queue (infra/constructs/finance/postings.py), so a committed
change can't lose its posting; posting_worker.py sends it to the ledger.

Posting IDs are deterministic, built from the business item's ID, so retrying anything
can never post twice: the ledger deduplicates on the ID (its event_id).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from finance.events import iso_ms
from finance.ledger.port import Transfer

POSTING_ITEM_TYPE = "POSTING"
_PART = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class PostingStatus(StrEnum):
    PENDING = "PENDING"
    POSTED = "POSTED"
    REJECTED_INSUFFICIENT_FUNDS = "REJECTED_INSUFFICIENT_FUNDS"  # needs money in first
    REJECTED = "REJECTED"  # validation or signature: a bug or misconfiguration


class PostingError(ValueError):
    pass


def _id(school_id: str, *parts: str) -> str:
    for part in parts:
        if not _PART.fullmatch(part):
            raise PostingError("posting ID parts are short IDs: letters, digits, - and _")
    return ":".join((school_id, *parts))


# Spec 7.3: one function per business event that moves money.
def payment_posting_id(school_id: str, payment_id: str) -> str:
    return _id(school_id, "payment", payment_id)


def settlement_net_posting_id(school_id: str, settlement_id: str) -> str:
    return _id(school_id, "settlement", settlement_id, "net")


def settlement_fee_posting_id(school_id: str, settlement_id: str) -> str:
    return _id(school_id, "settlement", settlement_id, "fee")


def cash_banked_posting_id(school_id: str, banking_id: str) -> str:
    return _id(school_id, "cash-banked", banking_id)


def other_income_posting_id(school_id: str, income_id: str) -> str:
    return _id(school_id, "other-income", income_id)


def payslip_net_posting_id(school_id: str, run_id: str, staff_id: str) -> str:
    return _id(school_id, "payslip", run_id, staff_id, "net")


def remittance_posting_id(school_id: str, run_id: str, rule_id: str) -> str:
    return _id(school_id, "remittance", run_id, rule_id)


@dataclass(frozen=True)
class Posting:
    school_id: str
    posting_id: str
    amount_minor: int
    currency: str
    source: str
    destination: str
    status: PostingStatus = PostingStatus.PENDING
    reason_code: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        prefix = f"{self.school_id}:"
        if not self.posting_id.startswith(prefix):
            raise PostingError("a posting ID starts with its school ID")
        # The ledger has no tenants: this is what keeps schools' money apart.
        if not (self.source.startswith(prefix) and self.destination.startswith(prefix)):
            raise PostingError("both ledger accounts must belong to the posting's school")
        if self.source == self.destination:
            raise PostingError("a transfer needs two different accounts")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise PostingError("amounts are integer minor units")
        if self.amount_minor <= 0:
            raise PostingError("a posting moves a positive amount")

    def transfer(self) -> Transfer:
        return Transfer(
            posting_id=self.posting_id,
            amount_minor=self.amount_minor,
            currency=self.currency,
            source=self.source,
            destination=self.destination,
        )


def posting_key(school_id: str, posting_id: str) -> dict[str, dict[str, str]]:
    return {"PK": {"S": f"SCHOOL#{school_id}#POSTING#{posting_id}"}, "SK": {"S": "META"}}


def status_index(
    school_id: str, status: PostingStatus, stamp: str, posting_id: str
) -> dict[str, Any]:
    """PostingsByStatus: GSI4PK = SCHOOL#<sid>#POSTINGS#<status>, GSI4SK = <time>#<id>."""
    return {
        "GSI4PK": {"S": f"SCHOOL#{school_id}#POSTINGS#{status}"},
        "GSI4SK": {"S": f"{stamp}#{posting_id}"},
    }


def new_posting_put(table_name: str, posting: Posting, now: datetime) -> dict[str, Any]:
    """The Put to include in the business change's TransactWriteItems."""
    stamp = iso_ms(now)
    return {
        "Put": {
            "TableName": table_name,
            "Item": {
                **posting_key(posting.school_id, posting.posting_id),
                "itemType": {"S": POSTING_ITEM_TYPE},  # what the postings pipe filters on
                "schoolId": {"S": posting.school_id},
                "postingId": {"S": posting.posting_id},
                "amountMinor": {"N": str(posting.amount_minor)},
                "currency": {"S": posting.currency},
                "source": {"S": posting.source},
                "destination": {"S": posting.destination},
                "status": {"S": PostingStatus.PENDING.value},
                "version": {"N": "1"},
                "createdAt": {"S": stamp},
                "updatedAt": {"S": stamp},
                **status_index(posting.school_id, PostingStatus.PENDING, stamp, posting.posting_id),
            },
            # Deterministic IDs: a second posting for the same business change is refused.
            "ConditionExpression": "attribute_not_exists(PK)",
        }
    }


def parse_posting(item: dict[str, Any]) -> Posting:
    return Posting(
        school_id=item["schoolId"]["S"],
        posting_id=item["postingId"]["S"],
        amount_minor=int(item["amountMinor"]["N"]),
        currency=item["currency"]["S"],
        source=item["source"]["S"],
        destination=item["destination"]["S"],
        status=PostingStatus(item["status"]["S"]),
        reason_code=item.get("reasonCode", {}).get("S"),
        version=int(item["version"]["N"]),
    )
