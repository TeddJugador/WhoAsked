"""The chart of ledger accounts for one school (spec 7.1).

The ledger has no tenant concept: a school's accounts are ledger accounts whose IDs
start with the school ID. Provider accounts are named by the provider configured for
the school; no provider name is built into this code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SCHOOL_ID = re.compile(r"^SCH-\d{3}$")
PROVIDER = re.compile(r"^[A-Z][A-Z0-9_]{1,30}$")

# The outside world. The ledger forbids negative balances and has no notion of money
# arriving from outside, so EXTERNAL starts with a very large balance and acts as a
# contra account: only its movements mean anything, never its absolute balance.
EXTERNAL_OPENING_BALANCE_MAJOR = "10000000000000"  # 10^13 major units


def _check(school_id: str) -> None:
    if not SCHOOL_ID.fullmatch(school_id):
        raise ValueError("school IDs look like SCH-001")


def external(school_id: str) -> str:
    _check(school_id)
    return f"{school_id}:EXTERNAL"


def provider(school_id: str, name: str) -> str:
    _check(school_id)
    if not PROVIDER.fullmatch(name):
        raise ValueError("provider account names are upper-case, e.g. PAYSTACK")
    return f"{school_id}:PROVIDER:{name}"


def bank_operating(school_id: str) -> str:
    _check(school_id)
    return f"{school_id}:BANK:OPERATING"


def cash(school_id: str) -> str:
    _check(school_id)
    return f"{school_id}:CASH"


@dataclass(frozen=True)
class LedgerAccount:
    account_id: str
    opening_balance_major: str  # what onboarding seeds, as the ledger stores it


def chart_of_accounts(school_id: str, providers: tuple[str, ...]) -> list[LedgerAccount]:
    """Every ledger account a school needs, with its opening balance."""
    accounts = [
        LedgerAccount(external(school_id), EXTERNAL_OPENING_BALANCE_MAJOR),
        LedgerAccount(bank_operating(school_id), "0"),
        LedgerAccount(cash(school_id), "0"),
    ]
    accounts.extend(LedgerAccount(provider(school_id, p), "0") for p in providers)
    return accounts
