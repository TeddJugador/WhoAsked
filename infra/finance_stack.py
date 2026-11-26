"""FinanceStack: school fees and payroll, wrapping FinancialLedgerAPI as the cash book.

So far: tables and key, the transactional-outbox pipe, the inbound event queue, ledger
postings (a second pipe, the ledger-postings queue and the posting worker) and their
alarms. The API, fees and payroll arrive with the later phases.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_events as events
from aws_cdk import aws_sns as sns
from constructs import Construct

from infra.constructs.dependencies_layer import DependenciesLayer
from infra.constructs.finance.alarms import FinanceAlarms
from infra.constructs.finance.inbox import FinanceInbox
from infra.constructs.finance.outbox_pipe import OutboxPipe
from infra.constructs.finance.postings import LedgerPostings
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
        layer_asset_path: str | Path,
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
        self.dependencies_layer = DependenciesLayer(
            self, "Dependencies", asset_path=layer_asset_path
        ).layer
        self.postings = LedgerPostings(
            self,
            "Postings",
            config=config,
            table=self.tables.finance,
            key=self.tables.key,
            audit_table=self.tables.audit,
            event_bus=event_bus,
            layer=self.dependencies_layer,
        )
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
                "finance-postings-dlq-not-empty": (
                    self.postings.dead_letter_queue,
                    "Ledger postings failed 5 times (the ledger unavailable or misconfigured). "
                    "Check the posting worker's logs; redrive once the ledger is reachable.",
                ),
                "finance-postings-pipe-dlq-not-empty": (
                    self.postings.pipe_dead_letter_queue,
                    "New postings couldn't be queued for the ledger. Check the postings "
                    "pipe's log group, then requeue the PENDING postings.",
                ),
                "finance-inbound-dlq-not-empty": (
                    self.inbox.dead_letter_queue,
                    "Inbound student, guardian or fee-schedule events failed 3 times. "
                    "Inspect and redrive the DLQ once the cause is fixed.",
                ),
            },
        )

        self.alarms.add_posting_rejected_alarm(topic=ops_alerts_topic)

        CfnOutput(self, "FinanceTableName", value=self.tables.finance.table_name)
        CfnOutput(self, "FinanceAuditTableName", value=self.tables.audit.table_name)
        CfnOutput(self, "LedgerPostingsQueueUrl", value=self.postings.queue.queue_url)
        CfnOutput(
            self, "PostingsDeadLetterQueueUrl", value=self.postings.dead_letter_queue.queue_url
        )
        CfnOutput(self, "OutboxDeadLetterQueueUrl", value=self.outbox.dead_letter_queue.queue_url)
        CfnOutput(self, "InboundQueueUrl", value=self.inbox.queue.queue_url)
        CfnOutput(self, "InboundDeadLetterQueueUrl", value=self.inbox.dead_letter_queue.queue_url)
