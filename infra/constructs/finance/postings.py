"""Ledger postings: Finance stream -> postings pipe -> ledger-postings queue -> worker.

A posting is committed with the business change that causes it. This pipe forwards
every new POSTING item (the whole stream record, no transformation) to the queue, so a
committed posting can't be left unsent. posting_worker is the only code that calls the
ledger, and the only function that can read its signing key.
"""

from __future__ import annotations

import json

from aws_cdk import Acknowledgment, ArnFormat, Duration, RemovalPolicy, Stack, Validations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_pipes as pipes
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.constructs.dependencies_layer import LAMBDA_ARCHITECTURE, LAMBDA_RUNTIME
from infra.constructs.finance.roles import finance_role
from infra.constructs.worker_function import RUNTIME_REASON, lambda_code, log_permissions
from infra.finance_config import FinanceConfig

POSTINGS_FILTER = {
    "eventName": ["INSERT"],
    "dynamodb": {"NewImage": {"itemType": {"S": ["POSTING"]}}},
}
WORKER_TIMEOUT = Duration.seconds(60)  # a 422 check can wait 9 s; the ledger times out at 10
MAX_RECEIVE_COUNT = 5
SERVICE_NAME = "finance-posting-worker"


def secret_arn(scope: Construct, secret_name: str) -> str:
    """Secrets Manager adds a random 6-character suffix to every secret's ARN."""
    return Stack.of(scope).format_arn(
        service="secretsmanager",
        resource="secret",
        resource_name=f"{secret_name}-??????",
        arn_format=ArnFormat.COLON_RESOURCE_NAME,
    )


class LedgerPostings(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: FinanceConfig,
        table: dynamodb.ITable,
        key: kms.IKey,
        audit_table: dynamodb.ITable,
        event_bus: events.IEventBus,
        layer: lambda_.ILayerVersion,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = Stack.of(self)
        if table.table_stream_arn is None:
            raise ValueError("postings need the Finance table's stream")

        def queue(construct_id: str, name: str, **kwargs: object) -> sqs.Queue:
            return sqs.Queue(
                self,
                construct_id,
                queue_name=name,
                retention_period=Duration.days(14),
                encryption=sqs.QueueEncryption.SQS_MANAGED,
                enforce_ssl=True,
                **kwargs,  # type: ignore[arg-type]
            )

        self.dead_letter_queue = queue("DeadLetterQueue", "ledger-postings-dlq")
        self.queue = queue(
            "Queue",
            "ledger-postings",
            visibility_timeout=Duration.seconds(WORKER_TIMEOUT.to_seconds() * 6),
            dead_letter_queue=sqs.DeadLetterQueue(
                queue=self.dead_letter_queue, max_receive_count=MAX_RECEIVE_COUNT
            ),
        )
        self.pipe_dead_letter_queue = queue("PipeDeadLetterQueue", "finance-postings-pipe-dlq")
        Validations.of(self.pipe_dead_letter_queue.node.default_child).acknowledge(  # type: ignore[arg-type]
            Acknowledgment(
                id="AwsSolutions-SQS3",
                reason="This is the postings pipe's dead-letter queue (its DeadLetterConfig), "
                "which cdk-nag can't see; it is not a source queue.",
            )
        )

        # --- the pipe: new POSTING items -> the queue -------------------------------
        self.pipe_log_group = logs.LogGroup(
            self,
            "PipeLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.pipe_role = finance_role(
            self,
            "PipeRole",
            assumed_by=iam.ServicePrincipal(
                "pipes.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": stack.account}},
            ),
            description="finance postings pipe: read the Finance stream, enqueue postings",
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
                sid="ListStreams", actions=["dynamodb:ListStreams"], resources=["*"]
            ),
            iam.PolicyStatement(
                sid="DecryptStreamRecords", actions=["kms:Decrypt"], resources=[key.key_arn]
            ),
            iam.PolicyStatement(
                sid="EnqueuePostings",
                actions=["sqs:SendMessage"],
                resources=[self.queue.queue_arn, self.pipe_dead_letter_queue.queue_arn],
            ),
            iam.PolicyStatement(
                sid="PipeLogs",
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{self.pipe_log_group.log_group_arn}:*"],
            ),
        ):
            self.pipe_role.add_to_policy(statement)
        log_group_id = stack.get_logical_id(self.pipe_log_group.node.default_child)  # type: ignore[arg-type]
        Validations.of(self.pipe_role.node.find_child("DefaultPolicy")).acknowledge(
            Acknowledgment(
                id="AwsSolutions-IAM5[Resource::*]",
                reason="dynamodb:ListStreams has no resource-level permissions; reads of the "
                "stream itself are limited to the Finance table's stream ARN.",
            ),
            Acknowledgment(
                id=f"AwsSolutions-IAM5[Resource::<{log_group_id}.Arn>:*]",
                reason="Log streams inside the pipe's own log group.",
            ),
        )
        self.pipe = pipes.CfnPipe(
            self,
            "Pipe",
            name="finance-postings",
            description="Finance postings: POSTING inserts -> ledger-postings queue",
            role_arn=self.pipe_role.role_arn,
            source=table.table_stream_arn,
            source_parameters=pipes.CfnPipe.PipeSourceParametersProperty(
                dynamo_db_stream_parameters=pipes.CfnPipe.PipeSourceDynamoDBStreamParametersProperty(
                    starting_position="TRIM_HORIZON",
                    batch_size=10,
                    maximum_retry_attempts=10,
                    maximum_record_age_in_seconds=24 * 60 * 60,
                    on_partial_batch_item_failure="AUTOMATIC_BISECT",
                    dead_letter_config=pipes.CfnPipe.DeadLetterConfigProperty(
                        arn=self.pipe_dead_letter_queue.queue_arn
                    ),
                ),
                filter_criteria=pipes.CfnPipe.FilterCriteriaProperty(
                    filters=[pipes.CfnPipe.FilterProperty(pattern=json.dumps(POSTINGS_FILTER))]
                ),
            ),
            target=self.queue.queue_arn,  # the whole stream record becomes the message
            log_configuration=pipes.CfnPipe.PipeLogConfigurationProperty(
                level="ERROR",
                cloudwatch_logs_log_destination=pipes.CfnPipe.CloudwatchLogsLogDestinationProperty(
                    log_group_arn=self.pipe_log_group.log_group_arn
                ),
            ),
        )
        self.pipe.node.add_dependency(self.pipe_role)

        # --- the worker: the only code that calls the ledger ----------------------------
        self.log_group = logs.LogGroup(
            self,
            "WorkerLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.role = finance_role(
            self,
            "WorkerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="finance posting worker: the only caller of the ledger",
            audit_table=audit_table,
        )
        log_permissions(self.role, self.log_group)
        self.ledger_secret_arn = secret_arn(self, config.ledger_signing_secret_name)
        for statement in (
            iam.PolicyStatement(
                sid="LedgerSigningKey",
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.ledger_secret_arn],
            ),
            iam.PolicyStatement(
                sid="Postings",
                # Read the posting; record the outcome (and an outbox item) in one transaction.
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem"],
                resources=[table.table_arn],
            ),
            iam.PolicyStatement(
                sid="FinanceKey",
                actions=["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                resources=[key.key_arn],
            ),
        ):
            self.role.add_to_policy(statement)

        self.function = lambda_.Function(
            self,
            "Worker",
            description="Sends ledger postings to FinancialLedgerAPI and records the outcome",
            runtime=LAMBDA_RUNTIME,
            architecture=LAMBDA_ARCHITECTURE,
            handler="finance.handlers.posting_worker.handler",
            code=lambda_code(),
            layers=[layer],
            memory_size=512,
            timeout=WORKER_TIMEOUT,
            tracing=lambda_.Tracing.ACTIVE,
            role=self.role,
            log_group=self.log_group,
            environment={
                "FINANCE_TABLE_NAME": table.table_name,
                "LEDGER_API_URL": config.ledger_api_url,
                "LEDGER_SIGNING_SECRET_NAME": config.ledger_signing_secret_name,
                "POWERTOOLS_SERVICE_NAME": SERVICE_NAME,
            },
        )
        self.function.add_event_source(
            sources.SqsEventSource(
                self.queue,
                batch_size=1,  # a 422 check waits up to 9 s: one posting per invocation
                report_batch_item_failures=True,
                max_concurrency=2,  # gentle on the ledger
            )
        )
        Validations.of(self.function.node.default_child).acknowledge(  # type: ignore[arg-type]
            Acknowledgment(id="AwsSolutions-L1", reason=RUNTIME_REASON)
        )
        Validations.of(self.role.node.find_child("DefaultPolicy")).acknowledge(
            Acknowledgment(
                id="AwsSolutions-IAM5[Resource::*]",
                reason="X-Ray PutTraceSegments/PutTelemetryRecords only support Resource '*'.",
            ),
        )
