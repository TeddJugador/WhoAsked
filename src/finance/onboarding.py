"""Onboarding a school (spec 7.1 and 3): its ledger accounts and its first finance config.

The only write to the ledger's table outside its API, using the ledger's documented
method (a PutItem per account, docs/ledger-contract.md section 8). A manual, audited
setup step run by an administrator with scripts/onboard_school.py, never runtime code.

Safe to run again: each account and the config are created only if absent, and every
run is recorded in FinanceAudit, with hashes of the state before and after.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from ulid import ULID

from finance.events import iso_ms
from finance.ledger.accounts import PROVIDER, SCHOOL_ID, chart_of_accounts

SUPPORTED_CURRENCIES = frozenset({"ZAR"})  # v1 (spec 3); a setting, not a constant elsewhere
_serializer = TypeSerializer()


def default_config(currency: str, providers: tuple[str, ...]) -> dict[str, Any]:
    """Spec section 3's defaults, plus the payment providers the school collects with
    (needed to know which provider ledger accounts exist)."""
    return {
        "currency": currency,
        "roundingIncrementMinor": 1,
        "invoiceDueDays": 30,
        "overdueReminderDays": [7, 14, 30],
        "discountStacking": "BEST_SINGLE",
        "maxTotalDiscountPercent": 100,
        "manualAdjustmentApprovalThresholdMinor": 0,
        "payoutMode": "RECORD_ONLY",
        "payoutProvider": None,
        "payoutLimits": None,
        "payrollProration": "CALENDAR_DAYS",
        "paymentProviders": list(providers),
    }


@dataclass
class OnboardingResult:
    school_id: str
    created_accounts: list[str] = field(default_factory=list)
    existing_accounts: list[str] = field(default_factory=list)
    config_created: bool = False
    audit_key: str = ""


def _digest(state: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def _put_if_absent(dynamodb: Any, table: str, item: dict[str, Any]) -> bool:  # noqa: ANN401
    try:
        dynamodb.put_item(
            TableName=table, Item=item, ConditionExpression="attribute_not_exists(PK)"
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


def onboard(
    *,
    dynamodb: Any,  # noqa: ANN401 - boto3 DynamoDB client
    school_id: str,
    providers: tuple[str, ...],
    currency: str,
    ledger_table: str,
    finance_table: str,
    audit_table: str,
    actor: str,
    now: datetime,
    new_id: Callable[[], str] = lambda: str(ULID()),
) -> OnboardingResult:
    if not SCHOOL_ID.fullmatch(school_id):
        raise ValueError("school IDs look like SCH-001")
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError(f"currency must be one of {sorted(SUPPORTED_CURRENCIES)} in v1")
    for provider in providers:
        if not PROVIDER.fullmatch(provider):
            raise ValueError("provider names are upper-case, e.g. PAYSTACK")

    result = OnboardingResult(school_id=school_id)
    for account in chart_of_accounts(school_id, providers):
        created = _put_if_absent(
            dynamodb,
            ledger_table,
            {
                "PK": {"S": f"ACCOUNT#{account.account_id}"},
                "SK": {"S": "METADATA"},
                "balance": {"N": account.opening_balance_major},
            },
        )
        (result.created_accounts if created else result.existing_accounts).append(
            account.account_id
        )

    stamp = iso_ms(now)
    config = default_config(currency, providers)
    result.config_created = _put_if_absent(
        dynamodb,
        finance_table,
        {
            "PK": {"S": f"SCHOOL#{school_id}"},
            "SK": {"S": "CONFIG#v1"},
            "version": {"N": "1"},
            "changedBy": {"S": actor},
            "changedAt": {"S": stamp},
            **{k: _serializer.serialize(v) for k, v in config.items()},
        },
    )

    before = {"accounts": sorted(result.existing_accounts), "config": not result.config_created}
    after = {"accounts": sorted(result.existing_accounts + result.created_accounts), "config": True}
    result.audit_key = f"{stamp}#{new_id()}"
    dynamodb.put_item(
        TableName=audit_table,
        Item={
            "PK": {"S": f"SCHOOL#{school_id}"},
            "SK": {"S": result.audit_key},
            "actor": {"S": actor},
            "action": {"S": "SCHOOL_ONBOARDED"},
            "target": {"S": f"school:{school_id}"},
            "beforeHash": {"S": _digest(before)},
            "afterHash": {"S": _digest(after)},
            "createdAccounts": {"L": [{"S": a} for a in result.created_accounts]},
            "configCreated": {"BOOL": result.config_created},
        },
        ConditionExpression="attribute_not_exists(PK)",
    )
    return result
