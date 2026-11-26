"""Every finance role is created here, so every one carries the audit-table deny.

FinanceAudit is append-only: roles get PutItem where they need it, and every role in the
finance stack also has an explicit Deny on anything that could change or remove an
audit record. An explicit Deny can't be overridden by any Allow added later.
tests/infra/test_finance_stack.py checks that no role in the stack lacks it.
"""

from __future__ import annotations

from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from constructs import Construct

AUDIT_TAMPERING_ACTIONS = (
    "dynamodb:DeleteItem",
    "dynamodb:UpdateItem",
    "dynamodb:BatchWriteItem",  # can delete
    "dynamodb:PartiQLDelete",
    "dynamodb:PartiQLUpdate",
)


def finance_role(
    scope: Construct,
    construct_id: str,
    *,
    assumed_by: iam.IPrincipal,
    description: str,
    audit_table: dynamodb.ITable,
) -> iam.Role:
    role = iam.Role(scope, construct_id, assumed_by=assumed_by, description=description)
    role.add_to_policy(
        iam.PolicyStatement(
            sid="AuditIsAppendOnly",
            effect=iam.Effect.DENY,
            actions=list(AUDIT_TAMPERING_ACTIONS),
            resources=[audit_table.table_arn],
        )
    )
    return role
