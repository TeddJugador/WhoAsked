"""Onboard a school: create its ledger accounts and its first finance config.

    python scripts/onboard_school.py --school SCH-001 --provider PAYSTACK
    python scripts/onboard_school.py --school SCH-001 --provider PAYSTACK --dry-run

Uses your AWS credentials. Reads the ledger table name from cdk.json and the finance
tables from the deployed SchoolFinanceStack. Safe to re-run: only missing accounts and
config are created, and each run is recorded in FinanceAudit with your identity.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import boto3  # noqa: E402

from finance.ledger.accounts import chart_of_accounts  # noqa: E402
from finance.onboarding import onboard  # noqa: E402


def stack_output(key: str) -> str:
    stack = boto3.client("cloudformation").describe_stacks(StackName="SchoolFinanceStack")[
        "Stacks"
    ][0]
    return next(o["OutputValue"] for o in stack["Outputs"] if o["OutputKey"] == key)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--school", required=True, help="e.g. SCH-001")
    parser.add_argument(
        "--provider",
        action="append",
        default=[],
        help="payment provider, e.g. PAYSTACK (repeatable)",
    )
    parser.add_argument("--currency", default="ZAR")
    parser.add_argument("--dry-run", action="store_true", help="show the accounts, change nothing")
    args = parser.parse_args()

    providers = tuple(args.provider)
    if args.dry_run:
        print(f"Ledger accounts for {args.school}:")
        for account in chart_of_accounts(args.school, providers):
            print(f"  {account.account_id:40} opening balance {account.opening_balance_major}")
        return

    context = json.loads((ROOT / "cdk.json").read_text(encoding="utf-8"))["context"]
    actor = boto3.client("sts").get_caller_identity()["Arn"]
    result = onboard(
        dynamodb=boto3.client("dynamodb"),
        school_id=args.school,
        providers=providers,
        currency=args.currency,
        ledger_table=context["financeLedgerTableName"],
        finance_table=stack_output("FinanceTableName"),
        audit_table=stack_output("FinanceAuditTableName"),
        actor=actor,
        now=datetime.now(UTC),
    )
    print(f"Onboarded {result.school_id} (as {actor})")
    for account in result.created_accounts:
        print(f"  created  {account}")
    for account in result.existing_accounts:
        print(f"  existing {account}")
    print(f"  finance config: {'created (v1)' if result.config_created else 'already present'}")
    print(f"  audit record: {result.audit_key}")


if __name__ == "__main__":
    main()
