"""Transactional outbox: Finance table stream -> EventBridge Pipe -> publisher Lambda
-> SchoolEventBus.

Only INSERTs of OUTBOX# items pass the pipe's filter. The pipe invokes the publisher
Lambda synchronously with each batch; it re-checks every item against the catalogue
and calls PutEvents with the detail-type set explicitly (src/finance/outbox_publish.py
explains why the pipe doesn't target the bus directly).

Delivery is at least once: the pipe starts from the oldest record in the stream
(TRIM_HORIZON), so a replaced pipe republishes up to a day of events rather than losing
any; consumers are idempotent on eventId. Records that keep failing go to the DLQ, which
alarms. The pipe logs its own failures (ERROR level, never event contents).
"""

from __future__ import annotations

import json

from aws_cdk import Acknowledgment, Duration, RemovalPolicy, Stack, Validations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_pipes as pipes
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.constructs.dependencies_layer import LAMBDA_ARCHITECTURE, LAMBDA_RUNTIME
from infra.constructs.finance.roles import finance_role
from infra.constructs.worker_function import RUNTIME_REASON, lambda_code, log_permissions

OUTBOX_FILTER = {
    "eventName": ["INSERT"],
    "dynamodb": {"Keys": {"PK": {"S": [{"prefix": "OUTBOX#"}]}}},
}
MAX_RETRY_ATTEMPTS = 10


class OutboxPipe(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table: dynamodb.ITable,
        key: kms.IKey,
        audit_table: dynamodb.ITable,
        event_bus: events.IEventBus,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = Stack.of(self)
        if table.table_stream_arn is None:
            raise ValueError("the outbox needs the Finance table's stream")

        self.dead_letter_queue = sqs.Queue(
            self,
            "DeadLetterQueue",
            queue_name="finance-outbox-dlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )
        Validations.of(self.dead_letter_queue.node.default_child).acknowledge(  # type: ignore[arg-type]
            Acknowledgment(
                id="AwsSolutions-SQS3",
                reason="This is the outbox pipe's dead-letter queue (set in the pipe's "
                "DeadLetterConfig), which cdk-nag can't see; it is not a source queue.",
            )
        )

        # The publisher: boto3 comes with the Lambda runtime, so no dependencies layer.
        self.log_group = logs.LogGroup(
            self,
            "PublisherLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        publisher_role = finance_role(
            self,
            "PublisherRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="finance outbox publisher: PutEvents on SchoolEventBus, no data access",
            audit_table=audit_table,
        )
        log_permissions(publisher_role, self.log_group)
        publisher_role.add_to_policy(
            iam.PolicyStatement(
                sid="Publish", actions=["events:PutEvents"], resources=[event_bus.event_bus_arn]
            )
        )
        self.publisher = lambda_.Function(
            self,
            "Publisher",
            description="Finance outbox: stream records -> PutEvents (source school.finance)",
            runtime=LAMBDA_RUNTIME,
            architecture=LAMBDA_ARCHITECTURE,
            handler="finance.handlers.outbox_publisher.handler",
            code=lambda_code(),
            memory_size=256,
            timeout=Duration.seconds(30),
            role=publisher_role,
            log_group=self.log_group,
            environment={"EVENT_BUS_NAME": event_bus.event_bus_name},
        )
        Validations.of(self.publisher.node.default_child).acknowledge(  # type: ignore[arg-type]
            Acknowledgment(id="AwsSolutions-L1", reason=RUNTIME_REASON)
        )

        # The pipe's own failures (ERROR only; execution data excluded: no event contents).
        self.pipe_log_group = logs.LogGroup(
            self,
            "PipeLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.role = finance_role(
            self,
            "PipeRole",
            assumed_by=iam.ServicePrincipal(
                "pipes.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": stack.account}},
            ),
            description="finance outbox pipe: read the Finance stream, invoke the publisher",
            audit_table=audit_table,
        )
        for statement in (
            iam.PolicyStatement(
                sid="ReadFinanceStream",
                actions=[
                    "dynamodb:DescribeStream",
                    "dynamodb:GetRecords",
                    "dynamodb:GetShardIterator",
                ],
                resources=[table.table_stream_arn],
            ),
            iam.PolicyStatement(
                sid="ListStreams",
                actions=["dynamodb:ListStreams"],  # no resource-level permissions
                resources=["*"],
            ),
            iam.PolicyStatement(
                sid="DecryptStreamRecords", actions=["kms:Decrypt"], resources=[key.key_arn]
            ),
            iam.PolicyStatement(
                sid="InvokePublisher",
                actions=["lambda:InvokeFunction"],
                resources=[self.publisher.function_arn],
            ),
            iam.PolicyStatement(
                sid="DeadLetters",
                actions=["sqs:SendMessage"],
                resources=[self.dead_letter_queue.queue_arn],
            ),
            iam.PolicyStatement(
                sid="PipeLogs",
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{self.pipe_log_group.log_group_arn}:*"],
            ),
        ):
            self.role.add_to_policy(statement)
        Validations.of(self.role.node.find_child("DefaultPolicy")).acknowledge(
            Acknowledgment(
                id="AwsSolutions-IAM5[Resource::*]",
                reason="dynamodb:ListStreams has no resource-level permissions; reads of the "
                "stream itself are limited to the Finance table's stream ARN.",
            ),
            Acknowledgment(
                id=f"AwsSolutions-IAM5[Resource::<{stack.get_logical_id(self.pipe_log_group.node.default_child)}.Arn>:*]",  # type: ignore[arg-type]
                reason="Log streams inside the pipe's own log group.",
            ),
        )

        self.pipe = pipes.CfnPipe(
            self,
            "Pipe",
            name="finance-outbox",
            description="Finance outbox: OUTBOX# inserts -> publisher -> SchoolEventBus",
            role_arn=self.role.role_arn,
            source=table.table_stream_arn,
            source_parameters=pipes.CfnPipe.PipeSourceParametersProperty(
                dynamo_db_stream_parameters=pipes.CfnPipe.PipeSourceDynamoDBStreamParametersProperty(
                    starting_position="TRIM_HORIZON",
                    batch_size=10,
                    maximum_retry_attempts=MAX_RETRY_ATTEMPTS,
                    maximum_record_age_in_seconds=24 * 60 * 60,
                    on_partial_batch_item_failure="AUTOMATIC_BISECT",
                    dead_letter_config=pipes.CfnPipe.DeadLetterConfigProperty(
                        arn=self.dead_letter_queue.queue_arn
                    ),
                ),
                filter_criteria=pipes.CfnPipe.FilterCriteriaProperty(
                    filters=[pipes.CfnPipe.FilterProperty(pattern=json.dumps(OUTBOX_FILTER))]
                ),
            ),
            target=self.publisher.function_arn,
            target_parameters=pipes.CfnPipe.PipeTargetParametersProperty(
                # Synchronous, so a publish failure fails the batch: retried, then DLQ.
                lambda_function_parameters=pipes.CfnPipe.PipeTargetLambdaFunctionParametersProperty(
                    invocation_type="REQUEST_RESPONSE"
                ),
            ),
            log_configuration=pipes.CfnPipe.PipeLogConfigurationProperty(
                level="ERROR",
                cloudwatch_logs_log_destination=pipes.CfnPipe.CloudwatchLogsLogDestinationProperty(
                    log_group_arn=self.pipe_log_group.log_group_arn
                ),
            ),
        )
        # Pipes validates the role's permissions on creation: attach them first.
        self.pipe.node.add_dependency(self.role)
