"""FinanceStack: school fees and payroll, wrapping FinancialLedgerAPI as the cash book.

Phase (b): tables and key, the transactional-outbox pipe, the inbound event queue and
their alarms. Lambdas, the API and ledger access arrive with the later phases. The
ledger's coordinates (config) are validated now and used from phase (c).
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_events as events
from aws_cdk import aws_sns as sns
from constructs import Construct

from infra.constructs.finance.alarms import FinanceAlarms
from infra.constructs.finance.inbox import FinanceInbox
from infra.constructs.finance.outbox_pipe import OutboxPipe
from infra.constructs.finance.tables import FinanceTables
from infra.constructs.ingestion_bucket import bucket_arn_for_name, ingestion_bucket_name
from infra.finance_config import FinanceConfig


class FinanceStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: FinanceConfig,
        event_bus: events.IEventBus,
        ops_alerts_topic: sns.ITopic,
        user_pool: cognito.IUserPool,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config
        self.user_pool = user_pool  # the API's JWT authorizer (fees phase)
        # Read-only, processed/fee_schedule/* only (granted to the event consumer later).
        self.fee_schedule_objects_arn = (
            f"{bucket_arn_for_name(self, ingestion_bucket_name(self))}/processed/fee_schedule/*"
        )

        self.tables = FinanceTables(self, "Tables")
        self.outbox = OutboxPipe(
            self,
            "Outbox",
            table=self.tables.finance,
            key=self.tables.key,
            audit_table=self.tables.audit,
            event_bus=event_bus,
        )
        self.inbox = FinanceInbox(self, "Inbox", event_bus=event_bus)
        self.alarms = FinanceAlarms(
            self,
            "Alarms",
            topic=ops_alerts_topic,
            dead_letter_queues={
                "finance-outbox-dlq-not-empty": (
                    self.outbox.dead_letter_queue,
                    "Outbox records failed to publish after retries: events for committed "
                    "changes are missing from the bus. Inspect the DLQ, fix the cause, and "
                    "republish from the outbox items (kept 7 days).",
                ),
                "finance-inbound-dlq-not-empty": (
                    self.inbox.dead_letter_queue,
                    "Inbound student, guardian or fee-schedule events failed 3 times. "
                    "Inspect and redrive the DLQ once the cause is fixed.",
                ),
            },
        )

        CfnOutput(self, "FinanceTableName", value=self.tables.finance.table_name)
        CfnOutput(self, "OutboxDeadLetterQueueUrl", value=self.outbox.dead_letter_queue.queue_url)
        CfnOutput(self, "InboundQueueUrl", value=self.inbox.queue.queue_url)
        CfnOutput(self, "InboundDeadLetterQueueUrl", value=self.inbox.dead_letter_queue.queue_url)
