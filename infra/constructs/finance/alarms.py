"""Finance alarms -> the shared ops-alerts topic. More arrive with later phases
(PostingRejected, reconciliation mismatches, webhook signature failures, payouts)."""

from __future__ import annotations

from aws_cdk import Duration
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as actions
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct


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
