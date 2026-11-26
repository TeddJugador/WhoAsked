"""Inbound events: one queue, fed by two rules on SchoolEventBus.

- school.records: StudentEnrolled, StudentWithdrawn, GuardianLinked,
  ClassAssignmentChanged (fee accounts and family projections).
- school.ingestion: ImportCompleted with datasetType FEE_SCHEDULE (draft schedules).

The consumer Lambda arrives with the fees phase; until then events wait in the queue
(14 days). Messages that fail 3 times go to the DLQ, which alarms; events EventBridge
can't deliver go there too.
"""

from __future__ import annotations

from aws_cdk import Duration
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_sqs as sqs
from constructs import Construct

RECORDS_EVENTS = ("StudentEnrolled", "StudentWithdrawn", "GuardianLinked", "ClassAssignmentChanged")
CONSUMER_TIMEOUT = Duration.seconds(30)
MAX_RECEIVE_COUNT = 3


class FinanceInbox(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, event_bus: events.IEventBus) -> None:
        super().__init__(scope, construct_id)

        self.dead_letter_queue = sqs.Queue(
            self,
            "DeadLetterQueue",
            queue_name="finance-inbound-dlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )
        self.queue = sqs.Queue(
            self,
            "Queue",
            queue_name="finance-inbound-queue",
            retention_period=Duration.days(14),
            # 6x the consumer's timeout, as AWS recommends for Lambda event sources.
            visibility_timeout=Duration.seconds(CONSUMER_TIMEOUT.to_seconds() * 6),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(
                queue=self.dead_letter_queue, max_receive_count=MAX_RECEIVE_COUNT
            ),
        )
        target = targets.SqsQueue(
            self.queue,
            dead_letter_queue=self.dead_letter_queue,
            retry_attempts=185,
            max_event_age=Duration.hours(24),
        )
        self.records_rule = events.Rule(
            self,
            "RecordsRule",
            rule_name="finance-records-events",
            description="Student and guardian changes for fee accounts and families",
            event_bus=event_bus,
            event_pattern=events.EventPattern(
                source=["school.records"], detail_type=list(RECORDS_EVENTS)
            ),
            targets=[target],
        )
        self.fee_schedule_rule = events.Rule(
            self,
            "FeeScheduleRule",
            rule_name="finance-fee-schedule-imports",
            description="Imported fee schedules, which become drafts for a bursar to review",
            event_bus=event_bus,
            event_pattern=events.EventPattern(
                source=["school.ingestion"],
                detail_type=["ImportCompleted"],
                detail={"data": {"datasetType": ["FEE_SCHEDULE"]}},
            ),
            targets=[target],
        )
