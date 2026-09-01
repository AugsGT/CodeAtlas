# This file handles the ingestion of OpenTelemetry metrics into a graph.
# It supports Sum and Histogram instrument data. Metrics are matched to CodeEntities using specific attributes.

import datetime
import hashlib

from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult

from ..graph.identity import resolve_and_link_metric
from ..graph.models import Metric


def to_metrics(metrics_data) -> list[Metric]:
    """Converts an OTel MetricsData tree into our Metric dataclasses."""
    metrics = []
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                for point in metric.data.data_points:
                    attrs = dict(point.attributes or {})
                    metrics.append(Metric(
                        id=_make_metric_id(metric.name, attrs, point.time_unix_nano),
                        name=metric.name,
                        value=_data_point_value(point),
                        unit=metric.unit or "",
                        timestamp=_to_datetime(point.time_unix_nano),
                        code_filepath=attrs.get("code.filepath", ""),
                        code_namespace=attrs.get("code.namespace", ""),
                        code_function=attrs.get("code.function", ""),
                        code_lineno=attrs.get("code.lineno", 0),
                    ))
    return metrics


def _data_point_value(point):
    """Extracts the value from a data point, handling both Sum and Histogram types."""
    if hasattr(point, "value"):
        return point.value
    return point.sum  # Histogram


def _make_metric_id(name, attributes, time_unix_nano):
    """Generates a unique ID for a metric using its name, attributes, and timestamp."""
    key = f"{name}|{time_unix_nano}|{sorted(attributes.items())}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _to_datetime(epoch_nanos):
    """Converts an epoch time in nanoseconds to a datetime object."""
    return datetime.datetime.fromtimestamp(epoch_nanos / 1e9, tz=datetime.timezone.utc)


class GraphMetricExporter(MetricExporter):
    """A MetricExporter that writes exported metric data points directly into the graph,
    resolving each to its recording CodeEntity.
    """

    def __init__(self, repo):
        super().__init__()
        self.repo = repo

    def export(self, metrics_data, timeout_millis=10_000, **kwargs):
        """Exports metrics by converting them and linking to their corresponding CodeEntities."""
        for metric in to_metrics(metrics_data):
            resolve_and_link_metric(self.repo, metric)
        return MetricExportResult.SUCCESS

    def shutdown(self, timeout_millis=30_000, **kwargs):
        """Shuts down the exporter gracefully."""
        pass

    def force_flush(self, timeout_millis=10_000):
        """Forces a flush of any buffered data."""
        return True