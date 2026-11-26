"""HttpLedger: the real adapter for FinancialLedgerAPI (docs/ledger-contract.md).

- The body is serialised once; those exact bytes are signed (HMAC-SHA256, hex, in
  X-Signature-256) and sent.
- Amounts go as JSON strings with exactly two decimals (contract section 4): the ledger
  parses JSON numbers through a float, strings never.
- event_id is the posting ID, the ledger's deduplication key.
- 422 is ambiguous (contract sections 5 and 7): insufficient funds, or a duplicate racing
  an earlier attempt that did commit. The same request is retried after short waits; a
  duplicate then comes back as 200 "already_processed". Still 422 -> insufficient funds.
- Throttling, 5xx, timeouts and network errors raise LedgerUnavailableError, so the SQS
  message is retried and finally dead-lettered.

Nothing here logs a request body: payroll postings carry salaries.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from finance.ledger.money import to_major
from finance.ledger.port import LedgerUnavailableError, Transfer, TransferOutcome

ROUTE = "/v1/webhooks/payment"
TIMEOUT_SECONDS = 10.0  # the ledger's own Lambda times out at 10 s
DUPLICATE_CHECK_DELAYS = (3.0, 6.0)  # seconds before each repeat of a 422'd request


def request_body(transfer: Transfer) -> bytes:
    return json.dumps(
        {
            "event_id": transfer.posting_id,
            "amount": to_major(transfer.amount_minor, transfer.currency),
            "currency": transfer.currency,
            "source_account": transfer.source,
            "destination_account": transfer.destination,
        },
        separators=(",", ":"),
    ).encode()


def sign(body: bytes, key: str) -> str:
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


class HttpLedger:
    def __init__(
        self,
        base_url: str,
        *,
        key_loader: Callable[[], str],
        client: httpx.Client | None = None,
        duplicate_check_delays: Sequence[float] = DUPLICATE_CHECK_DELAYS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = base_url.rstrip("/") + ROUTE
        self._key_loader = key_loader
        self._client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
        self._delays = tuple(duplicate_check_delays)
        self._sleep = sleep

    def transfer(self, transfer: Transfer) -> TransferOutcome:
        body = request_body(transfer)
        headers = {
            "Content-Type": "application/json",
            "X-Signature-256": sign(body, self._key_loader()),
        }
        for delay in (0.0, *self._delays):
            if delay:
                self._sleep(delay)
            status, payload = self._post(body, headers)
            if status != 422:
                return self._outcome(status, payload)
        return TransferOutcome.INSUFFICIENT_FUNDS

    def _post(self, body: bytes, headers: dict[str, str]) -> tuple[int, Any]:
        try:
            response = self._client.post(self._url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LedgerUnavailableError(f"ledger request failed ({type(exc).__name__})") from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return response.status_code, payload

    @staticmethod
    def _outcome(status: int, payload: Any) -> TransferOutcome:  # noqa: ANN401 - JSON
        if status == 200 and isinstance(payload, dict):
            if payload.get("status") == "completed":
                return TransferOutcome.APPLIED
            if payload.get("status") == "already_processed":
                return TransferOutcome.ALREADY_APPLIED
        if status == 400:
            return TransferOutcome.VALIDATION_FAILED
        if status == 401:
            return TransferOutcome.SIGNATURE_REJECTED
        # 200 with an unexpected body, 403/404 (misconfiguration), 429, 5xx: retry, and
        # let the DLQ alarm if it never succeeds.
        raise LedgerUnavailableError(f"unexpected ledger response (HTTP {status})")
