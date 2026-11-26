"""Postings, the posting worker, and onboarding."""

from __future__ import annotations

import json
from typing import Any

import pytest

from finance.ledger.accounts import bank_operating, cash, chart_of_accounts, external, provider
from finance.ledger.memory import InMemoryLedger
from finance.ledger.port import LedgerUnavailableError, Transfer, TransferOutcome
from finance.onboarding import default_config, onboard
from finance.posting_worker import PostingMissingError, PostingWorker
from finance.postings import (
    Posting,
    PostingError,
    PostingStatus,
    cash_banked_posting_id,
    new_posting_put,
    other_income_posting_id,
    payment_posting_id,
    payslip_net_posting_id,
    remittance_posting_id,
    settlement_fee_posting_id,
    settlement_net_posting_id,
)
from tests.unit.finance.finance_harness import (
    AUDIT_TABLE,
    FINANCE_TABLE,
    LEDGER_TABLE,
    NOW,
    FinanceHarness,
)

SALARY = 1873429  # a distinctive net salary, in cents: must never appear in logs


# --- posting IDs and items ----------------------------------------------------------------


def test_posting_ids_are_deterministic() -> None:
    assert payment_posting_id("SCH-001", "PAY-01JD") == "SCH-001:payment:PAY-01JD"
    assert settlement_net_posting_id("SCH-001", "SET-1") == "SCH-001:settlement:SET-1:net"
    assert settlement_fee_posting_id("SCH-001", "SET-1") == "SCH-001:settlement:SET-1:fee"
    assert cash_banked_posting_id("SCH-001", "BNK-1") == "SCH-001:cash-banked:BNK-1"
    assert other_income_posting_id("SCH-001", "INC-1") == "SCH-001:other-income:INC-1"
    assert (
        payslip_net_posting_id("SCH-001", "PRUN-1", "STF-9") == "SCH-001:payslip:PRUN-1:STF-9:net"
    )
    assert remittance_posting_id("SCH-001", "PRUN-1", "PAYE") == "SCH-001:remittance:PRUN-1:PAYE"
    assert payment_posting_id("SCH-001", "PAY-01JD") == payment_posting_id("SCH-001", "PAY-01JD")


@pytest.mark.parametrize("bad", ["PAY:1", "PAY 1", "", "x" * 81])
def test_posting_id_parts_cannot_inject_separators(bad: str) -> None:
    with pytest.raises(PostingError):
        payment_posting_id("SCH-001", bad)


def _posting(
    amount: int = 25000,
    posting_id: str = "SCH-001:payment:PAY-1",
    src: str = "SCH-001:EXTERNAL",
    dst: str = "SCH-001:CASH",
) -> Posting:
    return Posting(
        school_id="SCH-001",
        posting_id=posting_id,
        amount_minor=amount,
        currency="ZAR",
        source=src,
        destination=dst,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"src": "SCH-002:EXTERNAL"},  # another school's money
        {"dst": "SCH-002:CASH"},
        {"posting_id": "SCH-002:payment:PAY-1"},
        {"dst": "SCH-001:EXTERNAL"},  # same account twice
        {"amount": 0},
        {"amount": -5},
    ],
)
def test_postings_stay_inside_their_school(kwargs: dict[str, Any]) -> None:
    with pytest.raises(PostingError):
        _posting(**kwargs)


def test_new_posting_item() -> None:
    put = new_posting_put(FINANCE_TABLE, _posting(), NOW)["Put"]
    item = put["Item"]
    assert put["ConditionExpression"] == "attribute_not_exists(PK)"
    assert (item["PK"]["S"], item["SK"]["S"]) == (
        "SCHOOL#SCH-001#POSTING#SCH-001:payment:PAY-1",
        "META",
    )
    assert item["itemType"] == {"S": "POSTING"}
    assert item["status"] == {"S": "PENDING"}
    assert item["amountMinor"] == {"N": "25000"}
    assert item["GSI4PK"] == {"S": "SCHOOL#SCH-001#POSTINGS#PENDING"}


# --- the posting worker -------------------------------------------------------------------


def _ledger() -> InMemoryLedger:
    ledger = InMemoryLedger()
    for account in chart_of_accounts("SCH-001", ("PAYSTACK",)):
        ledger.open_account(account.account_id, account.opening_balance_major)
    return ledger


def _worker(fh: FinanceHarness, ledger: Any) -> PostingWorker:
    return PostingWorker(
        dynamodb=fh.dynamodb,
        table_name=FINANCE_TABLE,
        ledger=ledger,
        metrics=fh.metrics,
        logger=fh.logger,
        now=lambda: NOW,
    )


def _write(fh: FinanceHarness, posting: Posting) -> None:
    put = new_posting_put(FINANCE_TABLE, posting, NOW)["Put"]
    fh.dynamodb.put_item(
        TableName=FINANCE_TABLE, Item=put["Item"], ConditionExpression=put["ConditionExpression"]
    )


def _status(fh: FinanceHarness, posting_id: str) -> dict[str, Any]:
    item = fh.item(FINANCE_TABLE, f"SCHOOL#SCH-001#POSTING#{posting_id}", "META")
    assert item is not None
    return item


def test_posting_is_sent_and_marked_posted(fh: FinanceHarness) -> None:
    ledger = _ledger()
    posting = _posting(
        amount=SALARY,
        posting_id="SCH-001:payslip:PRUN-1:STF-9:net",
        src=external("SCH-001"),
        dst=cash("SCH-001"),
    )
    _write(fh, posting)
    assert _worker(fh, ledger).process("SCH-001", posting.posting_id) is PostingStatus.POSTED
    item = _status(fh, posting.posting_id)
    assert item["status"]["S"] == "POSTED"
    assert item["version"]["N"] == "2"
    assert item["GSI4PK"]["S"] == "SCHOOL#SCH-001#POSTINGS#POSTED"
    assert ledger.balance_minor(cash("SCH-001"), "ZAR") == SALARY
    assert fh.metrics.counts == [("PostingPosted", {})]
    assert fh.outbox() == []  # success publishes nothing in this phase
    assert str(SALARY) not in fh.logs()
    assert "18734.29" not in fh.logs()


def test_retries_never_double_post(fh: FinanceHarness) -> None:
    ledger = _ledger()
    _write(fh, _posting())
    worker = _worker(fh, ledger)
    worker.process("SCH-001", "SCH-001:payment:PAY-1")
    assert worker.process("SCH-001", "SCH-001:payment:PAY-1") is PostingStatus.POSTED  # redelivery
    assert len(ledger.transactions) == 1
    assert ledger.balance_minor(cash("SCH-001"), "ZAR") == 25000


def test_ledger_already_has_it(fh: FinanceHarness) -> None:
    """A previous attempt reached the ledger but crashed before recording the outcome."""
    ledger = _ledger()
    posting = _posting()
    ledger.transfer(posting.transfer())
    _write(fh, posting)
    assert _worker(fh, ledger).process("SCH-001", posting.posting_id) is PostingStatus.POSTED
    assert len(ledger.transactions) == 1


def test_insufficient_funds_is_permanent_alarmed_and_published(fh: FinanceHarness) -> None:
    ledger = _ledger()
    posting = _posting(
        amount=SALARY,
        posting_id="SCH-001:payslip:PRUN-1:STF-9:net",
        src=bank_operating("SCH-001"),
        dst=external("SCH-001"),
    )
    _write(fh, posting)
    assert (
        _worker(fh, ledger).process("SCH-001", posting.posting_id)
        is PostingStatus.REJECTED_INSUFFICIENT_FUNDS
    )
    item = _status(fh, posting.posting_id)
    assert (item["status"]["S"], item["reasonCode"]["S"]) == (
        "REJECTED_INSUFFICIENT_FUNDS",
        "INSUFFICIENT_FUNDS",
    )
    assert fh.metrics.counts == [("PostingRejected", {"Reason": "INSUFFICIENT_FUNDS"})]
    (outbox,) = fh.outbox()
    assert outbox["detailType"]["S"] == "LedgerPostingRejected"
    detail = json.loads(outbox["detail"]["S"])
    assert detail["data"] == {"postingId": posting.posting_id, "reasonCode": "INSUFFICIENT_FUNDS"}
    assert str(SALARY) not in outbox["detail"]["S"]  # no amounts in the event
    assert str(SALARY) not in fh.logs()


class _Answering:
    def __init__(self, outcome: TransferOutcome) -> None:
        self.outcome = outcome

    def transfer(self, transfer: Transfer) -> TransferOutcome:
        return self.outcome


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (TransferOutcome.VALIDATION_FAILED, "VALIDATION_FAILED"),
        (TransferOutcome.SIGNATURE_REJECTED, "SIGNATURE_REJECTED"),
    ],
)
def test_validation_and_signature_rejections(
    fh: FinanceHarness, outcome: TransferOutcome, reason: str
) -> None:
    _write(fh, _posting())
    assert (
        _worker(fh, _Answering(outcome)).process("SCH-001", "SCH-001:payment:PAY-1")
        is PostingStatus.REJECTED
    )
    assert _status(fh, "SCH-001:payment:PAY-1")["reasonCode"]["S"] == reason
    assert fh.metrics.counts == [("PostingRejected", {"Reason": reason})]


def test_ledger_unavailable_leaves_it_pending_for_a_retry(fh: FinanceHarness) -> None:
    ledger = _ledger()
    ledger.fail_next = 1
    _write(fh, _posting())
    worker = _worker(fh, ledger)
    with pytest.raises(LedgerUnavailableError):
        worker.process("SCH-001", "SCH-001:payment:PAY-1")
    assert _status(fh, "SCH-001:payment:PAY-1")["status"]["S"] == "PENDING"
    assert worker.process("SCH-001", "SCH-001:payment:PAY-1") is PostingStatus.POSTED  # the retry


def test_concurrent_deliveries_record_one_outcome(fh: FinanceHarness) -> None:
    """A second delivery finishes while the first is still talking to the ledger."""
    ledger = _ledger()
    _write(fh, _posting())

    class Racing:
        def __init__(self) -> None:
            self.raced = False

        def transfer(self, transfer: Transfer) -> TransferOutcome:
            if not self.raced:
                self.raced = True
                _worker(fh, ledger).process("SCH-001", transfer.posting_id)  # the other delivery
            return ledger.transfer(transfer)

    assert _worker(fh, Racing()).process("SCH-001", "SCH-001:payment:PAY-1") is PostingStatus.POSTED
    assert len(ledger.transactions) == 1
    assert _status(fh, "SCH-001:payment:PAY-1")["version"]["N"] == "2"  # one transition


def test_missing_posting(fh: FinanceHarness) -> None:
    with pytest.raises(PostingMissingError):
        _worker(fh, _ledger()).process("SCH-001", "SCH-001:payment:NOPE")


def test_handler_reads_the_stream_record_the_pipe_forwards() -> None:
    from finance.handlers.posting_worker import InvalidPostingMessageError, posting_reference

    record = {
        "eventName": "INSERT",
        "dynamodb": {"NewImage": new_posting_put(FINANCE_TABLE, _posting(), NOW)["Put"]["Item"]},
    }
    assert posting_reference(json.dumps(record)) == ("SCH-001", "SCH-001:payment:PAY-1")
    record["dynamodb"]["NewImage"]["itemType"] = {"S": "INVOICE"}
    with pytest.raises(InvalidPostingMessageError):
        posting_reference(json.dumps(record))
    with pytest.raises(InvalidPostingMessageError):
        posting_reference("not json")


# --- onboarding -----------------------------------------------------------------------------


def _onboard(fh: FinanceHarness, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "dynamodb": fh.dynamodb,
        "school_id": "SCH-001",
        "providers": ("PAYSTACK",),
        "currency": "ZAR",
        "ledger_table": LEDGER_TABLE,
        "finance_table": FINANCE_TABLE,
        "audit_table": AUDIT_TABLE,
        "actor": "arn:aws:iam::123456789012:user/admin",
        "now": NOW,
    }
    return onboard(**{**kwargs, **overrides})


def test_onboarding_creates_the_chart_of_accounts(fh: FinanceHarness) -> None:
    result = _onboard(fh)
    assert sorted(result.created_accounts) == sorted(
        ["SCH-001:EXTERNAL", "SCH-001:BANK:OPERATING", "SCH-001:CASH", "SCH-001:PROVIDER:PAYSTACK"]
    )
    external_item = fh.item(LEDGER_TABLE, "ACCOUNT#SCH-001:EXTERNAL", "METADATA")
    assert external_item == {
        "PK": {"S": "ACCOUNT#SCH-001:EXTERNAL"},
        "SK": {"S": "METADATA"},
        "balance": {"N": "10000000000000"},
    }
    assert fh.item(LEDGER_TABLE, "ACCOUNT#SCH-001:PROVIDER:PAYSTACK", "METADATA")["balance"] == {
        "N": "0"
    }  # type: ignore[index]


def test_onboarding_writes_the_default_config(fh: FinanceHarness) -> None:
    _onboard(fh)
    config = fh.item(FINANCE_TABLE, "SCHOOL#SCH-001", "CONFIG#v1")
    assert config is not None
    assert config["currency"] == {"S": "ZAR"}
    assert config["discountStacking"] == {"S": "BEST_SINGLE"}
    assert config["payoutMode"] == {"S": "RECORD_ONLY"}
    assert config["payoutProvider"] == {"NULL": True}
    assert config["paymentProviders"] == {"L": [{"S": "PAYSTACK"}]}
    assert config["changedBy"]["S"].endswith(":user/admin")
    assert set(default_config("ZAR", ())) >= {
        "currency",
        "roundingIncrementMinor",
        "invoiceDueDays",
        "payrollProration",
    }


def test_onboarding_is_audited_and_safe_to_rerun(fh: FinanceHarness) -> None:
    first = _onboard(fh)
    fh.dynamodb.update_item(  # money moved since: a re-run must not reset it
        TableName=LEDGER_TABLE,
        Key={"PK": {"S": "ACCOUNT#SCH-001:CASH"}, "SK": {"S": "METADATA"}},
        UpdateExpression="SET balance = :b",
        ExpressionAttributeValues={":b": {"N": "500.00"}},
    )
    second = _onboard(fh, now=NOW.replace(minute=45))
    assert second.created_accounts == []
    assert len(second.existing_accounts) == 4
    assert second.config_created is False
    assert fh.item(LEDGER_TABLE, "ACCOUNT#SCH-001:CASH", "METADATA")["balance"] == {"N": "500.00"}  # type: ignore[index]
    audit = fh.scan(AUDIT_TABLE)
    assert [a["action"]["S"] for a in audit] == ["SCHOOL_ONBOARDED", "SCHOOL_ONBOARDED"]
    by_key = {a["SK"]["S"]: a for a in audit}
    assert by_key[first.audit_key]["beforeHash"] != by_key[first.audit_key]["afterHash"]
    assert (
        by_key[second.audit_key]["beforeHash"] == by_key[second.audit_key]["afterHash"]
    )  # nothing changed


def test_onboarded_accounts_work_with_the_in_memory_ledger_rules(fh: FinanceHarness) -> None:
    """The accounts onboarding creates are exactly the chart the postings use."""
    result = _onboard(fh, providers=("PAYSTACK",))
    assert provider("SCH-001", "PAYSTACK") in result.created_accounts
    assert {external("SCH-001"), bank_operating("SCH-001"), cash("SCH-001")} <= set(
        result.created_accounts
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"currency": "USD"}, "currency"),
        ({"school_id": "SCH-1"}, "SCH-001"),
        ({"providers": ("paystack",)}, "upper-case"),
    ],
)
def test_onboarding_validates_its_input(
    fh: FinanceHarness, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _onboard(fh, **overrides)
    assert fh.scan(LEDGER_TABLE) == []  # nothing written


class _Counting:
    """Wraps a ledger and counts calls."""

    def __init__(self, ledger: Any) -> None:
        self.ledger = ledger
        self.calls = 0

    def transfer(self, transfer: Transfer) -> TransferOutcome:
        self.calls += 1
        return self.ledger.transfer(transfer)  # type: ignore[no-any-return]


def test_settled_postings_never_reach_the_ledger_again(fh: FinanceHarness) -> None:
    """A rejected posting redelivered after the money arrives must not be applied: the
    ledger would move the money while the record still said REJECTED."""
    ledger = _ledger()
    posting = _posting(src=cash("SCH-001"), dst=bank_operating("SCH-001"))  # CASH starts empty
    _write(fh, posting)
    counting = _Counting(ledger)
    worker = _worker(fh, counting)
    assert (
        worker.process("SCH-001", posting.posting_id) is PostingStatus.REJECTED_INSUFFICIENT_FUNDS
    )
    ledger.balances[cash("SCH-001")] += 1000  # the money arrives later
    assert (
        worker.process("SCH-001", posting.posting_id) is PostingStatus.REJECTED_INSUFFICIENT_FUNDS
    )
    assert counting.calls == 1  # the redelivery never called the ledger
    assert ledger.transactions == {}


def test_a_slow_delivery_cannot_overwrite_a_recorded_outcome(fh: FinanceHarness) -> None:
    """Two deliveries disagree: the first to record wins; the other changes nothing."""
    ledger = _ledger()
    _write(fh, _posting())

    class SlowAndWrong:
        def transfer(self, transfer: Transfer) -> TransferOutcome:
            _worker(fh, ledger).process("SCH-001", transfer.posting_id)  # the other one: POSTED
            return TransferOutcome.VALIDATION_FAILED  # this one, late, says something else

    metrics_before = list(fh.metrics.counts)
    assert (
        _worker(fh, SlowAndWrong()).process("SCH-001", "SCH-001:payment:PAY-1")
        is PostingStatus.POSTED
    )
    item = _status(fh, "SCH-001:payment:PAY-1")
    assert item["status"]["S"] == "POSTED"
    assert "reasonCode" not in item
    assert fh.outbox() == []  # no LedgerPostingRejected for a posting that was posted
    assert ("PostingRejected", {"Reason": "VALIDATION_FAILED"}) not in fh.metrics.counts[
        len(metrics_before) :
    ]
