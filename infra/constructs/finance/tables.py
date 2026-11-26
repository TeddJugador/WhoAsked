"""Finance tables, encrypted with the finance service's own KMS key.

Finance: the single table (fees, payments, payroll, postings, outbox). Its stream
(NEW_IMAGE) feeds the outbox pipe. Global secondary indexes, each for one query:

- OpenInvoicesByDueDate: GSI1PK = SCHOOL#<sid>#OPEN_INVOICES, GSI1SK =
  <dueDate>#<invoiceId>. A school's unpaid invoices due on or before a date (the overdue
  job). Sparse: set while an invoice has an outstanding balance, removed when settled.
- PaymentsByProviderReference: GSI2PK = PROVIDER#<provider>#<reference>. The payment a
  provider webhook refers to (the webhook request carries no school).
- PayslipsByStaff: GSI3PK = SCHOOL#<sid>#STAFF#<staffId>, GSI3SK = <period>#<runId>.
  One staff member's payslips, newest first (self-service).
- PostingsByStatus: GSI4PK = SCHOOL#<sid>#POSTING#<status>, GSI4SK =
  <updatedAt>#<postingId>. A school's ledger postings in a status, by time
  (reconciliation, stuck postings).

FinanceAudit: append-only audit trail (see roles.py). FinanceIdempotency: Powertools.

Finance and FinanceAudit, and the key that encrypts them, are retained when the stack is
deleted: they hold financial records. The idempotency table holds nothing durable.
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from constructs import Construct

OPEN_INVOICES_INDEX = "OpenInvoicesByDueDate"
PAYMENTS_BY_REFERENCE_INDEX = "PaymentsByProviderReference"
PAYSLIPS_BY_STAFF_INDEX = "PayslipsByStaff"
POSTINGS_BY_STATUS_INDEX = "PostingsByStatus"
GSIS = (
    (OPEN_INVOICES_INDEX, "GSI1PK", "GSI1SK"),
    (PAYMENTS_BY_REFERENCE_INDEX, "GSI2PK", None),
    (PAYSLIPS_BY_STAFF_INDEX, "GSI3PK", "GSI3SK"),
    (POSTINGS_BY_STATUS_INDEX, "GSI4PK", "GSI4SK"),
)
OUTBOX_TTL_ATTRIBUTE = "expiresAt"


def _key(name: str) -> dynamodb.Attribute:
    return dynamodb.Attribute(name=name, type=dynamodb.AttributeType.STRING)


class FinanceTables(Construct):
    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)

        self.key = kms.Key(
            self,
            "Key",
            alias="alias/school-finance",
            description="Encrypts the finance service's tables",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,  # the retained tables need it
            pending_window=Duration.days(30),
        )
        self.finance = self._table(
            "Finance",
            table_name="Finance",
            sort_key="SK",
            stream=dynamodb.StreamViewType.NEW_IMAGE,
            time_to_live_attribute=OUTBOX_TTL_ATTRIBUTE,
            removal_policy=RemovalPolicy.RETAIN,
        )
        for name, partition, sort in GSIS:
            self.finance.add_global_secondary_index(
                index_name=name,
                partition_key=_key(partition),
                sort_key=_key(sort) if sort else None,
                projection_type=dynamodb.ProjectionType.ALL,
            )

        self.audit = self._table(
            "Audit",
            table_name="FinanceAudit",
            sort_key="SK",  # PK = SCHOOL#<sid>, SK = <ISO timestamp>#<ulid>
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.idempotency = self._table(
            "Idempotency",
            table_name="FinanceIdempotency",
            partition_key="id",
            time_to_live_attribute="expiration",
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _table(
        self,
        construct_id: str,
        *,
        table_name: str,
        removal_policy: RemovalPolicy,
        partition_key: str = "PK",
        sort_key: str | None = None,
        stream: dynamodb.StreamViewType | None = None,
        time_to_live_attribute: str | None = None,
    ) -> dynamodb.Table:
        """On-demand, point-in-time recovery, encrypted with the finance key."""
        return dynamodb.Table(
            self,
            construct_id,
            table_name=table_name,
            partition_key=_key(partition_key),
            sort_key=_key(sort_key) if sort_key else None,
            stream=stream,
            time_to_live_attribute=time_to_live_attribute,
            removal_policy=removal_policy,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.key,
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
        )
