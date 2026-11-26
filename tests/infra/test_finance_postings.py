"""Phase (c) infrastructure: the postings pipe, ledger-postings queue and posting worker."""

from __future__ import annotations

import json
from typing import Any

import pytest

from infra.constructs.finance.postings import POSTINGS_FILTER
from tests.infra.conftest import Synthesized, as_list, resources_of_type

LEDGER_SECRET = "ledger/webhook-signing-secret"


def _function(template: dict[str, Any], handler: str) -> tuple[str, dict[str, Any]]:
    (match,) = [
        (k, v["Properties"])
        for k, v in resources_of_type(template, "AWS::Lambda::Function").items()
        if v["Properties"]["Handler"] == handler
    ]
    return match


def _statements(template: dict[str, Any], role_id: str) -> list[dict[str, Any]]:
    return [
        s
        for p in resources_of_type(template, "AWS::IAM::Policy").values()
        if {"Ref": role_id} in p["Properties"]["Roles"]
        for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]


def _pipe(template: dict[str, Any], name: str) -> dict[str, Any]:
    (pipe,) = [
        p
        for p in resources_of_type(template, "AWS::Pipes::Pipe").values()
        if p["Properties"]["Name"] == name
    ]
    return dict(pipe)


def _templates(synthesized: Synthesized) -> dict[str, dict[str, Any]]:
    return {
        "shared": synthesized.shared_json,
        "ingestion": synthesized.ingestion_json,
        "knowledge": synthesized.knowledge_json,
        "finance": synthesized.finance_json,
    }


# --- who can reach the ledger --------------------------------------------------------------


def test_only_the_posting_worker_can_read_the_ledger_secret(synthesized: Synthesized) -> None:
    readers = []
    for stack, template in _templates(synthesized).items():
        for policy_id, policy in resources_of_type(template, "AWS::IAM::Policy").items():
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                if LEDGER_SECRET in json.dumps(statement.get("Resource", "")):
                    readers.append((stack, policy_id, statement))
    ((stack, policy_id, statement),) = readers
    assert stack == "finance"
    assert "PostingsWorkerRole" in policy_id
    assert statement["Effect"] == "Allow"
    assert as_list(statement["Action"]) == ["secretsmanager:GetSecretValue"]
    assert f":secret:{LEDGER_SECRET}-??????" in json.dumps(statement["Resource"])


def test_worker_permissions_are_exactly_these(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    _, props = _function(template, "finance.handlers.posting_worker.handler")
    role_id = props["Role"]["Fn::GetAtt"][0]
    allows = [s for s in _statements(template, role_id) if s["Effect"] == "Allow"]
    by_sid = {s["Sid"]: s for s in allows if "Sid" in s}
    assert set(by_sid) == {"WriteOwnLogs", "LedgerSigningKey", "Postings", "FinanceKey"}
    assert set(as_list(by_sid["Postings"]["Action"])) == {
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:PutItem",
    }
    finance_id = next(
        k
        for k, t in resources_of_type(template, "AWS::DynamoDB::Table").items()
        if t["Properties"]["TableName"] == "Finance"
    )
    assert by_sid["Postings"]["Resource"] == {"Fn::GetAtt": [finance_id, "Arn"]}
    unnamed = {a for s in allows if "Sid" not in s for a in as_list(s["Action"])}
    # The rest: consuming its own queue (added by the event source) and X-Ray.
    assert unnamed <= {
        "sqs:ReceiveMessage",
        "sqs:ChangeMessageVisibility",
        "sqs:GetQueueUrl",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
    }


def test_worker_settings(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    _, props = _function(template, "finance.handlers.posting_worker.handler")
    assert props["Timeout"] == 60
    assert props["Architectures"] == ["arm64"]
    assert len(props["Layers"]) == 1
    env = props["Environment"]["Variables"]
    assert env["LEDGER_API_URL"] == "https://abc123defg.execute-api.us-east-1.amazonaws.com"
    assert env["LEDGER_SIGNING_SECRET_NAME"] == LEDGER_SECRET  # a name; the key is read at runtime
    assert env["POWERTOOLS_SERVICE_NAME"] == "finance-posting-worker"
    (mapping,) = resources_of_type(template, "AWS::Lambda::EventSourceMapping").values()
    assert mapping["Properties"]["BatchSize"] == 1
    assert mapping["Properties"]["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert mapping["Properties"]["ScalingConfig"] == {"MaximumConcurrency": 2}


def test_ledger_postings_queue(synthesized: Synthesized) -> None:
    queues = {
        q["Properties"]["QueueName"]: q["Properties"]
        for q in resources_of_type(synthesized.finance_json, "AWS::SQS::Queue").values()
    }
    postings = queues["ledger-postings"]
    assert postings["VisibilityTimeout"] == 360  # 6x the worker's timeout
    assert postings["RedrivePolicy"]["maxReceiveCount"] == 5
    assert "ledger-postings-dlq" in queues
    assert "finance-postings-pipe-dlq" in queues


# --- the postings pipe ----------------------------------------------------------------------


def test_postings_pipe_forwards_new_postings_unchanged(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    props = _pipe(template, "finance-postings")["Properties"]
    finance_id = next(
        k
        for k, t in resources_of_type(template, "AWS::DynamoDB::Table").items()
        if t["Properties"]["TableName"] == "Finance"
    )
    assert props["Source"] == {"Fn::GetAtt": [finance_id, "StreamArn"]}
    (filter_,) = props["SourceParameters"]["FilterCriteria"]["Filters"]
    assert json.loads(filter_["Pattern"]) == POSTINGS_FILTER
    assert POSTINGS_FILTER == {
        "eventName": ["INSERT"],
        "dynamodb": {"NewImage": {"itemType": {"S": ["POSTING"]}}},
    }
    queue_id = next(
        k
        for k, q in resources_of_type(template, "AWS::SQS::Queue").items()
        if q["Properties"]["QueueName"] == "ledger-postings"
    )
    assert props["Target"] == {"Fn::GetAtt": [queue_id, "Arn"]}
    assert "TargetParameters" not in props  # the whole stream record is the message
    assert "Enrichment" not in props
    stream = props["SourceParameters"]["DynamoDBStreamParameters"]
    assert stream["StartingPosition"] == "TRIM_HORIZON"
    assert "DeadLetterConfig" in stream
    assert props["LogConfiguration"]["Level"] == "ERROR"
    assert "IncludeExecutionData" not in props["LogConfiguration"]


def test_postings_pipe_role(synthesized: Synthesized) -> None:
    template = synthesized.finance_json
    role_id = _pipe(template, "finance-postings")["Properties"]["RoleArn"]["Fn::GetAtt"][0]
    allows = {s["Sid"]: s for s in _statements(template, role_id) if s["Effect"] == "Allow"}
    assert set(allows) == {
        "ReadFinanceStream",
        "ListStreams",
        "DecryptStreamRecords",
        "EnqueuePostings",
        "PipeLogs",
    }
    assert as_list(allows["EnqueuePostings"]["Action"]) == ["sqs:SendMessage"]
    text = json.dumps(allows)
    assert "events:PutEvents" not in text
    assert "lambda:InvokeFunction" not in text
    assert "secretsmanager" not in text


# --- alarms and the metric contract -----------------------------------------------------------


def test_any_rejected_posting_alarms(synthesized: Synthesized) -> None:
    (alarm,) = [
        a["Properties"]
        for a in resources_of_type(synthesized.finance_json, "AWS::CloudWatch::Alarm").values()
        if a["Properties"]["AlarmName"] == "finance-posting-rejected"
    ]
    assert alarm["Threshold"] == 0
    assert alarm["ComparisonOperator"] == "GreaterThanThreshold"
    dimension_sets = [
        {d["Name"]: d["Value"] for d in q["MetricStat"]["Metric"]["Dimensions"]}
        for q in alarm["Metrics"]
        if "MetricStat" in q
    ]
    assert {d["Reason"] for d in dimension_sets} == {
        "INSUFFICIENT_FUNDS",
        "VALIDATION_FAILED",
        "SIGNATURE_REJECTED",
    }
    assert {d["service"] for d in dimension_sets} == {"finance-posting-worker"}
    assert all(
        q["MetricStat"]["Metric"]["Namespace"] == "SchoolSystem/Finance"
        for q in alarm["Metrics"]
        if "MetricStat" in q
    )


def test_worker_emits_exactly_what_the_alarm_watches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from finance.metrics import NAMESPACE, PowertoolsMetrics

    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "finance-posting-worker")
    PowertoolsMetrics().count("PostingRejected", {"Reason": "INSUFFICIENT_FUNDS"})
    (document,) = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    metrics = document["_aws"]["CloudWatchMetrics"][0]
    assert metrics["Namespace"] == NAMESPACE == "SchoolSystem/Finance"
    assert [set(d) for d in metrics["Dimensions"]] == [{"service", "Reason"}]
    assert (document["service"], document["Reason"], document["PostingRejected"]) == (
        "finance-posting-worker",
        "INSUFFICIENT_FUNDS",
        [1.0],
    )


def test_new_outputs(synthesized: Synthesized) -> None:
    assert {"FinanceAuditTableName", "LedgerPostingsQueueUrl", "PostingsDeadLetterQueueUrl"} <= set(
        synthesized.finance_json["Outputs"]
    )
