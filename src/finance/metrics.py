"""Finance metrics (EMF via Powertools). Counts and statuses only: never amounts."""

from __future__ import annotations

from typing import Protocol

from aws_lambda_powertools.metrics import MetricUnit, single_metric

NAMESPACE = "SchoolSystem/Finance"


class MetricsSink(Protocol):
    def count(self, name: str, dimensions: dict[str, str] | None = None) -> None: ...


class PowertoolsMetrics:
    def count(self, name: str, dimensions: dict[str, str] | None = None) -> None:
        with single_metric(
            name=name, unit=MetricUnit.Count, value=1, namespace=NAMESPACE
        ) as metric:
            for key, value in (dimensions or {}).items():
                metric.add_dimension(name=key, value=value)
