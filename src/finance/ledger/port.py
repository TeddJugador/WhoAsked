"""The ledger port: one operation, a transfer, with the outcomes the ledger can give.

Two implementations: HttpLedger (http.py), the real adapter, and InMemoryLedger
(memory.py), with the same rules, for tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class TransferOutcome(StrEnum):
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"  # the ledger had this posting ID: no double-post
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"  # permanent: the source can't cover it
    VALIDATION_FAILED = "VALIDATION_FAILED"  # permanent: the ledger refused the request
    SIGNATURE_REJECTED = "SIGNATURE_REJECTED"  # permanent: wrong key or signing bug


class LedgerUnavailableError(RuntimeError):
    """Throttling, a 5xx, a timeout, a network failure: retry later (SQS)."""


@dataclass(frozen=True)
class Transfer:
    posting_id: str  # the ledger's deduplication key (event_id)
    amount_minor: int
    currency: str
    source: str  # ledger account IDs
    destination: str


class LedgerPort(Protocol):
    def transfer(self, transfer: Transfer) -> TransferOutcome: ...
