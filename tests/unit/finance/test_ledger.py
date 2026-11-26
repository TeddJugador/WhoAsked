"""The ledger port: money conversion, the chart of accounts, InMemoryLedger, HttpLedger."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from decimal import Decimal
from typing import Any

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from finance.ledger.accounts import (
    EXTERNAL_OPENING_BALANCE_MAJOR,
    bank_operating,
    cash,
    chart_of_accounts,
    external,
    provider,
)
from finance.ledger.http import HttpLedger
from finance.ledger.memory import InMemoryLedger
from finance.ledger.money import MoneyError, from_major, to_major
from finance.ledger.port import LedgerUnavailableError, Transfer, TransferOutcome

KEY = "test-signing-key-0123456789"


# --- money ------------------------------------------------------------------------------


@given(st.integers(min_value=1, max_value=10**18))
def test_minor_to_major_round_trips_exactly(minor: int) -> None:
    major = to_major(minor, "ZAR")
    assert re.fullmatch(r"\d+\.\d{2}", major)  # always exactly two decimals
    assert from_major(major, "ZAR") == minor
    assert Decimal(major) * 100 == minor  # no float anywhere on the way


@pytest.mark.parametrize(
    ("minor", "major"),
    [
        (1, "0.01"),
        (10, "0.10"),
        (100, "1.00"),
        (25000, "250.00"),
        (123456789012345678, "1234567890123456.78"),
    ],
)
def test_known_conversions(minor: int, major: str) -> None:
    assert to_major(minor, "ZAR") == major


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "100"])
def test_ledger_amounts_are_positive_integers(bad: Any) -> None:
    with pytest.raises(MoneyError):
        to_major(bad, "ZAR")


@pytest.mark.parametrize("bad", ["250.005", "NaN", "Infinity", "abc"])
def test_from_major_refuses_inexact_values(bad: str) -> None:
    with pytest.raises(MoneyError):
        from_major(bad, "ZAR")


def test_unknown_currency() -> None:
    with pytest.raises(MoneyError, match="unsupported currency"):
        to_major(100, "USD")


# --- chart of accounts ---------------------------------------------------------------------


def test_chart_of_accounts() -> None:
    accounts = {
        a.account_id: a.opening_balance_major for a in chart_of_accounts("SCH-001", ("PAYSTACK",))
    }
    assert accounts == {
        "SCH-001:EXTERNAL": "10000000000000",
        "SCH-001:BANK:OPERATING": "0",
        "SCH-001:CASH": "0",
        "SCH-001:PROVIDER:PAYSTACK": "0",
    }
    assert str(10**13) == EXTERNAL_OPENING_BALANCE_MAJOR


@pytest.mark.parametrize("bad", ["SCH-1", "sch-001", "SCH-001:CASH"])
def test_account_ids_need_a_valid_school(bad: str) -> None:
    with pytest.raises(ValueError, match="SCH-001"):
        external(bad)


def test_provider_names_are_not_built_in() -> None:
    assert provider("SCH-001", "MOBILEPAY") == "SCH-001:PROVIDER:MOBILEPAY"
    with pytest.raises(ValueError, match="upper-case"):
        provider("SCH-001", "paystack")


# --- InMemoryLedger ----------------------------------------------------------------------


def _transfer(
    amount: int = 25000,
    posting_id: str = "SCH-001:payment:PAY-1",
    src: str = "SCH-001:EXTERNAL",
    dst: str = "SCH-001:CASH",
) -> Transfer:
    return Transfer(
        posting_id=posting_id, amount_minor=amount, currency="ZAR", source=src, destination=dst
    )


@pytest.fixture
def ledger() -> InMemoryLedger:
    memory = InMemoryLedger()
    for account in chart_of_accounts("SCH-001", ("PAYSTACK",)):
        memory.open_account(account.account_id, account.opening_balance_major)
    return memory


def test_transfer_moves_money(ledger: InMemoryLedger) -> None:
    assert ledger.transfer(_transfer()) is TransferOutcome.APPLIED
    assert ledger.balance_minor(cash("SCH-001"), "ZAR") == 25000
    assert ledger.transactions["SCH-001:payment:PAY-1"].amount == "250.00"


def test_duplicates_are_not_applied_twice(ledger: InMemoryLedger) -> None:
    ledger.transfer(_transfer())
    # Like the real ledger, a known event_id is acknowledged without comparing anything.
    assert ledger.transfer(_transfer(amount=99999)) is TransferOutcome.ALREADY_APPLIED
    assert ledger.balance_minor(cash("SCH-001"), "ZAR") == 25000


def test_balances_never_go_negative(ledger: InMemoryLedger) -> None:
    outcome = ledger.transfer(_transfer(src=cash("SCH-001"), dst=bank_operating("SCH-001")))
    assert outcome is TransferOutcome.INSUFFICIENT_FUNDS
    assert ledger.balance_minor(cash("SCH-001"), "ZAR") == 0
    assert ledger.transactions == {}


@given(st.lists(st.integers(min_value=1, max_value=10**9), max_size=30))
def test_money_is_conserved(amounts: list[int]) -> None:
    memory = InMemoryLedger()
    for account in chart_of_accounts("SCH-001", ()):
        memory.open_account(account.account_id, account.opening_balance_major)
    total_before = sum(memory.balances.values())
    for i, amount in enumerate(amounts):
        src, dst = (
            (external("SCH-001"), cash("SCH-001"))
            if i % 2 == 0
            else (cash("SCH-001"), bank_operating("SCH-001"))
        )
        memory.transfer(
            _transfer(amount=amount, posting_id=f"SCH-001:other-income:I{i}", src=src, dst=dst)
        )
    assert sum(memory.balances.values()) == total_before
    assert all(balance >= 0 for balance in memory.balances.values())


def test_missing_source_cannot_pay_missing_destination_fails(ledger: InMemoryLedger) -> None:
    assert ledger.transfer(_transfer(src="SCH-001:NOPE")) is TransferOutcome.INSUFFICIENT_FUNDS
    with pytest.raises(LedgerUnavailableError):
        ledger.transfer(_transfer(dst="SCH-001:NOPE"))


# --- HttpLedger against a fake that checks requests the way the real ledger does -----------


class FakeLedgerServer:
    """Verifies each request like FinancialLedgerAPI: HMAC over the raw bytes, plain
    json.loads, and (our contract) the amount is a string. Answers from a script."""

    def __init__(self, *responses: tuple[int, Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        expected = hmac.new(KEY.encode(), body, hashlib.sha256).hexdigest()
        assert request.headers["X-Signature-256"] == expected
        assert request.url.path == "/v1/webhooks/payment"
        payload = json.loads(body)  # exactly what the ledger does
        assert isinstance(payload["amount"], str)  # never a JSON number (float path)
        self.requests.append(payload)
        status, response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return httpx.Response(status, json=response)


def _http(server: FakeLedgerServer, sleeps: list[float] | None = None) -> HttpLedger:
    return HttpLedger(
        "https://abc123defg.execute-api.us-east-1.amazonaws.com/",
        key_loader=lambda: KEY,
        client=httpx.Client(transport=httpx.MockTransport(server)),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def test_http_request_matches_the_contract() -> None:
    server = FakeLedgerServer((200, {"status": "completed", "event_id": "SCH-001:payment:PAY-1"}))
    assert _http(server).transfer(_transfer(amount=123456789012345678)) is TransferOutcome.APPLIED
    assert server.requests == [
        {
            "event_id": "SCH-001:payment:PAY-1",
            "amount": "1234567890123456.78",  # exact: a float would have lost the cents
            "currency": "ZAR",
            "source_account": "SCH-001:EXTERNAL",
            "destination_account": "SCH-001:CASH",
        }
    ]


def test_already_processed_is_success() -> None:
    server = FakeLedgerServer((200, {"status": "already_processed", "event_id": "x"}))
    assert _http(server).transfer(_transfer()) is TransferOutcome.ALREADY_APPLIED


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(400, TransferOutcome.VALIDATION_FAILED), (401, TransferOutcome.SIGNATURE_REJECTED)],
)
def test_permanent_rejections(status: int, outcome: TransferOutcome) -> None:
    assert _http(FakeLedgerServer((status, {"error": "x"}))).transfer(_transfer()) is outcome


def test_422_that_was_a_racing_duplicate_resolves_to_already_applied() -> None:
    sleeps: list[float] = []
    server = FakeLedgerServer(
        (422, {"error": "x"}), (200, {"status": "already_processed", "event_id": "x"})
    )
    assert _http(server, sleeps).transfer(_transfer()) is TransferOutcome.ALREADY_APPLIED
    assert sleeps == [3.0]
    assert server.requests[0] == server.requests[1]  # the very same request again


def test_repeated_422_means_insufficient_funds() -> None:
    sleeps: list[float] = []
    server = FakeLedgerServer(*([(422, {"error": "x"})] * 3))
    assert _http(server, sleeps).transfer(_transfer()) is TransferOutcome.INSUFFICIENT_FUNDS
    assert sleeps == [3.0, 6.0]
    assert len(server.requests) == 3


@pytest.mark.parametrize(
    "response",
    [
        (500, {"error": "Internal Server Error"}),
        (503, {"message": "Service Unavailable"}),
        (429, {"message": "Too Many Requests"}),
        (404, {"message": "Not Found"}),  # a wrong URL: retry, then the DLQ alarms
        (200, {"status": "something-new"}),
        (0, httpx.ConnectTimeout("timed out")),
    ],
)
def test_everything_else_is_retried_via_sqs(response: tuple[int, Any]) -> None:
    with pytest.raises(LedgerUnavailableError) as excinfo:
        _http(FakeLedgerServer(response)).transfer(_transfer())
    assert "250.00" not in str(excinfo.value)  # no amounts in errors


def test_signing_key_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    from finance.ledger import secrets

    monkeypatch.setattr(
        secrets.parameters,
        "get_secret",
        lambda name, max_age: json.dumps({"key": "", "signing_key": "abc"}),
    )
    assert secrets.load_signing_key("ledger/webhook-signing-secret") == "abc"
    monkeypatch.setattr(
        secrets.parameters, "get_secret", lambda name, max_age: json.dumps({"key": ""})
    )
    with pytest.raises(secrets.SigningKeyError):
        secrets.load_signing_key("ledger/webhook-signing-secret")
