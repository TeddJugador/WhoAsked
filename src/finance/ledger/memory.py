"""An in-memory ledger with the real ledger's rules (docs/ledger-contract.md):

- transfers are atomic and never leave a balance negative;
- a posting ID seen before is acknowledged without applying anything, and, like the real
  ledger, without comparing the amount or accounts;
- an unknown source account can't cover anything (insufficient funds); an unknown
  destination fails the request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from finance.ledger.money import exponent, to_major
from finance.ledger.port import LedgerUnavailableError, Transfer, TransferOutcome


@dataclass(frozen=True)
class LedgerTransaction:
    event_id: str
    amount: str  # decimal string, as the ledger stores it
    currency: str
    source_account: str
    destination_account: str


@dataclass
class InMemoryLedger:
    balances: dict[str, Decimal] = field(default_factory=dict)  # major units
    transactions: dict[str, LedgerTransaction] = field(default_factory=dict)
    fail_next: int = 0  # simulate that many transient failures (5xx/timeouts)

    def open_account(self, account_id: str, balance_major: str = "0") -> None:
        self.balances.setdefault(account_id, Decimal(balance_major))

    def transfer(self, transfer: Transfer) -> TransferOutcome:
        if self.fail_next:
            self.fail_next -= 1
            raise LedgerUnavailableError("simulated ledger outage")
        if transfer.posting_id in self.transactions:
            return TransferOutcome.ALREADY_APPLIED
        amount_text = to_major(transfer.amount_minor, transfer.currency)
        amount = Decimal(amount_text)
        if transfer.destination not in self.balances:
            raise LedgerUnavailableError("destination account does not exist")
        if self.balances.get(transfer.source, Decimal(-1)) < amount:
            return TransferOutcome.INSUFFICIENT_FUNDS
        self.balances[transfer.source] -= amount
        self.balances[transfer.destination] += amount
        self.transactions[transfer.posting_id] = LedgerTransaction(
            event_id=transfer.posting_id,
            amount=amount_text,
            currency=transfer.currency,
            source_account=transfer.source,
            destination_account=transfer.destination,
        )
        return TransferOutcome.APPLIED

    def balance_minor(self, account_id: str, currency: str) -> int:
        return int(self.balances[account_id].scaleb(exponent(currency)))
