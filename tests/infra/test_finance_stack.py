"""Phase (b) infrastructure: tables and key, the outbox pipe, the inbox, alarms."""

from __future__ import annotations

import json
from typing import Any

import pytest

from infra.constructs.finance.outbox_pipe import OUTBOX_FILTER
from infra.constructs.finance.roles import AUDIT_TAMPERING_ACTIONS
from tests.infra.conftest import Synthesized, SynthFn, as_list, resources_of_type


def _tables(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        t["Properties"]["TableName"]: t
        for t in resources_of_type(template, "AWS::DynamoDB::Table").values()
    }


def _logical_id(template: dict[str, Any], resource_type: str, contains: str) -> str:
    (logical_id,) = [k for k in resources_of_type(template, resource_type) if contains in k]
    return logical_id


# --- key and tables -----------------------------------------------------------------------


def test_customer_managed_key_with_rotation(synthesized: Synthesized) -> None:
    (key,) = resources_of_type(synthesized.finance_json, "AWS::KMS::Key").values()
    assert key["Properties"]["EnableKeyRotation"] is True
    assert key["DeletionPolicy"] == "Retain"  # the retained tables depend on it


def test_finance_tables_use_the_key_and_pitr(synthesized: Synthesized) -> None:
    key_id = _logical_id(synthesized.finance_json, "AWS::KMS::Key", "Key")
    tables = _tables(synthesized.finance_json)
    assert set(tables) == {"Finance", "FinanceAudit", "FinanceIdempotency"}
    for name, table in tables.items():
        props = table["Properties"]
        assert props["SSESpecification"]["SSEEnabled"] is True, name
        assert props["SSESpecification"]["SSEType"] == "KMS", name
        assert props["SSESpecification"]["KMSMasterKeyId"] == {"Fn::GetAtt": [key_id, "Arn"]}, name
        assert props["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True, name
        assert props["BillingMode"] == "PAY_PER_REQUEST", name
    assert tables["Finance"]["DeletionPolicy"] == "Retain"
    assert tables["FinanceAudit"]["DeletionPolicy"] == "Retain"
    assert tables["FinanceIdempotency"]["DeletionPolicy"] == "Delete"


def test_finance_table_stream_ttl_and_indexes(synthesized: Synthesized) -> None:
    props = _tables(synthesized.finance_json)["Finance"]["Properties"]
    assert props["StreamSpecification"] == {"StreamViewType": "NEW_IMAGE"}
    assert props["TimeToLiveSpecification"] == {"AttributeName": "expiresAt", "Enabled": True}
    indexes = {
        gsi["IndexName"]: [(k["AttributeName"], k["KeyType"]) for k in gsi["KeySchema"]]
        for gsi in props["GlobalSecondaryIndexes"]
    }
    assert indexes == {
        "OpenInvoicesByDueDate": [("GSI1PK", "HASH"), ("GSI1SK", "RANGE")],
        "PaymentsByProviderReference": [("GSI2PK", "HASH")],
        "PayslipsByStaff": [("GSI3PK", "HASH"), ("GSI3SK", "RANGE")],
        "PostingsByStatus": [("GSI4PK", "HASH"), ("GSI4SK", "RANGE")],
    }


# --- the audit table ----------------------------------------------------------------------


def _policies_of(template: dict[str, Any], role_id: str) -> list[dict[str, Any]]:
    return [
        s
        for p in resources_of_type(template, "AWS::IAM::Policy").values()
        if {"Ref": role_id} in p["Properties"]["Roles"]
        for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]


def test_every_finance_role_denies_audit_tampering(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    audit_id = _logical_id(template, "AWS::DynamoDB::Table", "Audit")
    roles = resources_of_type(template, "AWS::IAM::Role")
    assert roles  # the pipe and its enrichment Lambda so far
    for role_id in roles:
        denies = [s for s in _policies_of(template, role_id) if s["Effect"] == "Deny"]
        (deny,) = denies
        assert set(as_list(deny["Action"])) == set(AUDIT_TAMPERING_ACTIONS), role_id
        assert deny["Resource"] == {"Fn::GetAtt": [audit_id, "Arn"]}, role_id


def test_no_role_may_change_audit_records(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    audit_id = _logical_id(template, "AWS::DynamoDB::Table", "Audit")
    for role_id in resources_of_type(template, "AWS::IAM::Role"):
        for statement in _policies_of(template, role_id):
            if statement["Effect"] != "Allow" or audit_id not in json.dumps(statement["Resource"]):
                continue
            assert set(as_list(statement["Action"])) <= {"dynamodb:PutItem"}, role_id


# --- the outbox pipe ----------------------------------------------------------------------


def _pipe(template: dict[str, Any]) -> dict[str, Any]:
    """The outbox pipe (the postings pipe is tested in test_finance_postings.py)."""
    (pipe,) = [
        p
        for p in resources_of_type(template, "AWS::Pipes::Pipe").values()
        if p["Properties"]["Name"] == "finance-outbox"
    ]
    return dict(pipe)


def test_pipe_reads_the_finance_stream_through_the_outbox_filter(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    finance_id = next(
        k
        for k, t in resources_of_type(template, "AWS::DynamoDB::Table").items()
        if t["Properties"]["TableName"] == "Finance"
    )
    props = _pipe(template)["Properties"]
    assert props["Source"] == {"Fn::GetAtt": [finance_id, "StreamArn"]}
    (filter_,) = props["SourceParameters"]["FilterCriteria"]["Filters"]
    assert json.loads(filter_["Pattern"]) == OUTBOX_FILTER
    assert OUTBOX_FILTER == {
        "eventName": ["INSERT"],
        "dynamodb": {"Keys": {"PK": {"S": [{"prefix": "OUTBOX#"}]}}},
    }


def test_pipe_delivery_settings(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    stream = _pipe(template)["Properties"]["SourceParameters"]["DynamoDBStreamParameters"]
    dlq_id = _logical_id(template, "AWS::SQS::Queue", "OutboxDeadLetterQueue")
    assert stream["StartingPosition"] == "TRIM_HORIZON"  # republish rather than lose
    assert stream["MaximumRetryAttempts"] == 10
    assert stream["OnPartialBatchItemFailure"] == "AUTOMATIC_BISECT"
    assert stream["DeadLetterConfig"]["Arn"] == {"Fn::GetAtt": [dlq_id, "Arn"]}


def test_pipe_invokes_the_publisher_synchronously(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    props = _pipe(template)["Properties"]
    publisher_id = _logical_id(template, "AWS::Lambda::Function", "Publisher")
    assert props["Target"] == {"Fn::GetAtt": [publisher_id, "Arn"]}
    assert "Enrichment" not in props
    # REQUEST_RESPONSE: a publish failure fails the batch (retried, then dead-lettered).
    assert props["TargetParameters"] == {
        "LambdaFunctionParameters": {"InvocationType": "REQUEST_RESPONSE"}
    }


def test_pipe_logs_its_errors_without_event_contents(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    logging = _pipe(template)["Properties"]["LogConfiguration"]
    assert logging["Level"] == "ERROR"
    assert "IncludeExecutionData" not in logging  # never the event payloads
    log_group_id = _logical_id(template, "AWS::Logs::LogGroup", "OutboxPipeLogs")
    assert logging["CloudwatchLogsLogDestination"]["LogGroupArn"] == {
        "Fn::GetAtt": [log_group_id, "Arn"]
    }


def test_pipe_waits_for_its_permissions(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    policy_id = _logical_id(template, "AWS::IAM::Policy", "OutboxPipeRoleDefaultPolicy")
    assert policy_id in as_list(_pipe(template).get("DependsOn", []))


def test_pipe_role(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    role_id = _logical_id(template, "AWS::IAM::Role", "OutboxPipeRole")
    trust = resources_of_type(template, "AWS::IAM::Role")[role_id]["Properties"][
        "AssumeRolePolicyDocument"
    ]
    (statement,) = trust["Statement"]
    assert statement["Principal"] == {"Service": "pipes.amazonaws.com"}
    assert "aws:SourceAccount" in json.dumps(statement["Condition"])
    allows = {s["Sid"]: s for s in _policies_of(template, role_id) if s["Effect"] == "Allow"}
    assert set(allows) == {
        "ReadFinanceStream",
        "ListStreams",
        "DecryptStreamRecords",
        "InvokePublisher",
        "DeadLetters",
        "PipeLogs",
    }
    assert "events:PutEvents" not in json.dumps(allows)  # only the publisher publishes
    assert as_list(allows["DecryptStreamRecords"]["Action"]) == ["kms:Decrypt"]
    assert allows["ListStreams"]["Resource"] == "*"  # the only wildcard, acknowledged


def test_publisher_only_publishes_and_needs_no_layer(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    publisher_id = _logical_id(template, "AWS::Lambda::Function", "Publisher")
    props = resources_of_type(template, "AWS::Lambda::Function")[publisher_id]["Properties"]
    assert props["Handler"] == "finance.handlers.outbox_publisher.handler"
    assert props["Architectures"] == ["arm64"]
    assert "Layers" not in props  # boto3 comes with the runtime
    assert "EVENT_BUS_NAME" in props["Environment"]["Variables"]
    role_id = props["Role"]["Fn::GetAtt"][0]
    allows = {s["Sid"]: s for s in _policies_of(template, role_id) if s["Effect"] == "Allow"}
    assert set(allows) == {"WriteOwnLogs", "Publish"}
    assert as_list(allows["Publish"]["Action"]) == ["events:PutEvents"]
    assert "Fn::ImportValue" in json.dumps(allows["Publish"]["Resource"])  # the shared bus only


# --- inbound events -----------------------------------------------------------------------


def test_inbound_queue_and_dlq(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    queues = {
        q["Properties"]["QueueName"]: q["Properties"]
        for q in resources_of_type(template, "AWS::SQS::Queue").values()
    }
    inbound = queues["finance-inbound-queue"]
    assert inbound["RedrivePolicy"]["maxReceiveCount"] == 3
    assert inbound["VisibilityTimeout"] == 180
    assert "finance-inbound-dlq" in queues
    assert "finance-outbox-dlq" in queues


def test_inbound_rules(synthesized: Synthesized) -> None:
    rules = {
        r["Properties"]["Name"]: r["Properties"]
        for r in resources_of_type(synthesized.finance_json, "AWS::Events::Rule").values()
    }
    assert set(rules) == {"finance-records-events", "finance-fee-schedule-imports"}
    records = rules["finance-records-events"]["EventPattern"]
    assert records == {
        "source": ["school.records"],
        "detail-type": [
            "StudentEnrolled",
            "StudentWithdrawn",
            "GuardianLinked",
            "ClassAssignmentChanged",
        ],
    }
    imports = rules["finance-fee-schedule-imports"]["EventPattern"]
    assert imports == {
        "source": ["school.ingestion"],
        "detail-type": ["ImportCompleted"],
        "detail": {"data": {"datasetType": ["FEE_SCHEDULE"]}},
    }
    for name, rule in rules.items():
        (target,) = rule["Targets"]
        assert target["RetryPolicy"] == {
            "MaximumEventAgeInSeconds": 86400,
            "MaximumRetryAttempts": 185,
        }, name
        assert "DeadLetterConfig" in target, name


def test_queue_policies_only_admit_the_two_rules(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    rule_ids = set(resources_of_type(template, "AWS::Events::Rule"))
    for policy in resources_of_type(template, "AWS::SQS::QueuePolicy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if statement.get("Principal") == {"Service": "events.amazonaws.com"}:
                source = json.dumps(statement["Condition"]["ArnEquals"]["aws:SourceArn"])
                assert any(rule_id in source for rule_id in rule_ids)


# --- alarms, boundaries, config -------------------------------------------------------------


def test_dlq_alarms_notify_ops(synthesized: Synthesized) -> None:
    alarms = {
        a["Properties"]["AlarmName"]: a["Properties"]
        for a in resources_of_type(synthesized.finance_json, "AWS::CloudWatch::Alarm").values()
    }
    assert {
        "finance-outbox-dlq-not-empty",
        "finance-inbound-dlq-not-empty",
        "finance-postings-dlq-not-empty",
        "finance-postings-pipe-dlq-not-empty",
    } <= set(alarms)
    for name, props in alarms.items():
        assert props["Threshold"] == 0, name
        assert props["AlarmActions"] == props["OKActions"], name
        assert "ImportValue" in json.dumps(props["AlarmActions"]), name


def test_no_finance_role_can_touch_the_ledger_table_yet(synthesized: Synthesized) -> None:
    """Read access to the ledger's table arrives with reconciliation (phase g), only there."""
    for policy in resources_of_type(synthesized.finance_json, "AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            assert "table/FinancialLedger" not in json.dumps(statement.get("Resource", ""))


def test_payout_resources_absent_by_default(synthesized: Synthesized) -> None:
    text = json.dumps(synthesized.finance_json).lower()
    assert "payout" not in text


def test_outputs(synthesized: Synthesized) -> None:
    assert {
        "FinanceTableName",
        "OutboxDeadLetterQueueUrl",
        "InboundQueueUrl",
        "InboundDeadLetterQueueUrl",
    } <= set(synthesized.finance_json["Outputs"])


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"financeLedgerApiUrl": None}, "financeLedgerApiUrl"),
        ({"financeLedgerApiUrl": "http://insecure.example.com"}, "https base URL"),
        (
            {
                "financeLedgerApiUrl": "https://abc123defg.execute-api.us-east-1.amazonaws.com/v1/webhooks/payment"
            },
            "base URL",
        ),
        (
            {"financeLedgerApiUrl": "https://YOUR-API-ID.execute-api.us-east-1.amazonaws.com"},
            "placeholder",
        ),
        (
            {"financeLedgerApiUrl": "https://abc.execute-api.us-east-1.amazonaws.com"},
            "10-character API ID",
        ),
        ({"financeLedgerSigningSecretName": "ledger/*"}, "no wildcards"),
        ({"financeLedgerTableName": "x"}, "table name"),
        ({"financePayoutProvider": "Bad Provider!"}, "lowercase"),
    ],
)
def test_invalid_finance_config_is_rejected(
    synth: SynthFn, overrides: dict[str, Any], message: str
) -> None:
    from infra.finance_config import FinanceConfigError

    with pytest.raises(FinanceConfigError, match=message):
        synth(**overrides)
