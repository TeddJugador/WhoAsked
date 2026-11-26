"""Finance alarms -> the shared ops-alerts topic. More arrive with later phases
(PostingRejected, reconciliation mismatches, webhook signature failures, payouts)."""

from __future__ import annotations

from aws_cdk import Duration
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as actions
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.constructs.finance.postings import SERVICE_NAME as POSTING_WORKER_SERVICE

METRICS_NAMESPACE = "SchoolSystem/Finance"  # src/finance/metrics.py
POSTING_REJECTION_REASONS = ("INSUFFICIENT_FUNDS", "VALIDATION_FAILED", "SIGNATURE_REJECTED")


class FinanceAlarms(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        topic: sns.ITopic,
        dead_letter_queues: dict[str, tuple[sqs.IQueue, str]],
    ) -> None:
        """``dead_letter_queues``: alarm name -> (queue, what to do about it)."""
        super().__init__(scope, construct_id)
        action = actions.SnsAction(topic)
        self.alarms: dict[str, cloudwatch.Alarm] = {}
        for name, (queue, advice) in dead_letter_queues.items():
            alarm = cloudwatch.Alarm(
                self,
                name,
                alarm_name=name,
                alarm_description=advice,
                metric=queue.metric_approximate_number_of_messages_visible(
                    period=Duration.minutes(1), statistic=cloudwatch.Stats.MAXIMUM
                ),
                threshold=0,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            alarm.add_alarm_action(action)
            alarm.add_ok_action(action)
            self.alarms[name] = alarm

    def add_posting_rejected_alarm(self, *, topic: sns.ITopic) -> cloudwatch.Alarm:
        """Any rejected posting (spec 4): money the school thinks moved, but the ledger
        refused. Powertools adds a "service" dimension, so each Reason is a metric of its
        own; the alarm sums them."""
        metrics = {
            f"m{i}": cloudwatch.Metric(
                namespace=METRICS_NAMESPACE,
                metric_name="PostingRejected",
                dimensions_map={"service": POSTING_WORKER_SERVICE, "Reason": reason},
                statistic=cloudwatch.Stats.SUM,
                period=Duration.minutes(5),
            )
            for i, reason in enumerate(POSTING_REJECTION_REASONS)
        }
        alarm = cloudwatch.Alarm(
            self,
            "PostingRejected",
            alarm_name="finance-posting-rejected",
            alarm_description=(
                "The ledger refused a posting. INSUFFICIENT_FUNDS: record the money that "
                "should be there first (bank balance, other income), then resubmit. "
                "VALIDATION_FAILED / SIGNATURE_REJECTED: a bug or a wrong signing key."
            ),
            metric=cloudwatch.MathExpression(
                expression=" + ".join(f"FILL({name}, 0)" for name in metrics),
                using_metrics=metrics,
                label="Rejected postings",
                period=Duration.minutes(5),
            ),
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        action = actions.SnsAction(topic)
        alarm.add_alarm_action(action)
        alarm.add_ok_action(action)
        self.alarms["finance-posting-rejected"] = alarm
        return alarm
