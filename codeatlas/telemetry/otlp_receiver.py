# OTLP (OpenTelemetry Protocol) receiver: decodes standard OTLP/HTTP protobuf payloads into this project's RuntimeSpan/Metric/LogEntry.

import datetime

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from ..graph.models import LogEntry, Metric, RuntimeSpan

_STATUS_CODE_NAMES = {0: "UNSET", 1: "OK", 2: "ERROR"}


def _attr_value(value_msg):
    # Extracts the value from a protobuf attribute message
    kind = value_msg.WhichOneof("value")
    if kind == "string_value":
        return value_msg.string_value
    if kind == "int_value":
        return value_msg.int_value
    if kind == "double_value":
        return value_msg.double_value
    if kind == "bool_value":
        return value_msg.bool_value
    return None  # array/kvlist/bytes not needed for the code.* attributes we key on


def _attrs_dict(attribute_list):
    # Converts a list of attribute messages to a dictionary
    return {a.key: _attr_value(a.value) for a in attribute_list}


def _to_datetime(epoch_nanos):
    # Converts an epoch timestamp in nanoseconds to a datetime object
    return datetime.datetime.fromtimestamp(epoch_nanos / 1e9, tz=datetime.timezone.utc)


def parse_trace_request(body: bytes) -> list[RuntimeSpan]:
    """Decode an OTLP ExportTraceServiceRequest into RuntimeSpans."""
    request = ExportTraceServiceRequest()
    request.ParseFromString(body)

    spans = []
    for resource_spans in request.resource_spans:
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                attrs = _attrs_dict(span.attributes)
                duration_ms = (span.end_time_unix_nano - span.start_time_unix_nano) / 1e6
                spans.append(RuntimeSpan(
                    id=span.span_id.hex(),
                    trace_id=span.trace_id.hex(),
                    name=span.name,
                    start_time=_to_datetime(span.start_time_unix_nano),
                    end_time=_to_datetime(span.end_time_unix_nano),
                    duration_ms=duration_ms,
                    status=_STATUS_CODE_NAMES.get(span.status.code, "UNSET"),
                    error_message=span.status.message or "",
                    return_value=str(attrs.get("codeatlas.return_value", "") or ""),
                    code_filepath=str(attrs.get("code.filepath", "") or ""),
                    code_namespace=str(attrs.get("code.namespace", "") or ""),
                    code_function=str(attrs.get("code.function", "") or ""),
                    code_lineno=int(attrs.get("code.lineno", 0) or 0),
                ))
    return spans


def parse_metrics_request(body: bytes) -> list[Metric]:
    """Decode an OTLP ExportMetricsServiceRequest into Metrics. Sum and Gauge data points use their as_int/as_double value directly; Histogram data points use their sum (see telemetry/metrics.py's to_metrics() for the same documented limitation on the in-process path — the full bucket distribution isn't modeled)."""
    request = ExportMetricsServiceRequest()
    request.ParseFromString(body)

    metrics = []
    for resource_metrics in request.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                data_kind = metric.WhichOneof("data")
                if data_kind in ("sum", "gauge"):
                    data_points = getattr(metric, data_kind).data_points
                    for point in data_points:
                        value = point.as_double if point.WhichOneof("value") == "as_double" else point.as_int
                        metrics.append(_build_metric(metric.name, metric.unit, value, point))
                elif data_kind == "histogram":
                    for point in metric.histogram.data_points:
                        metrics.append(_build_metric(metric.name, metric.unit, point.sum, point))
    return metrics


def _build_metric(name, unit, value, point) -> Metric:
    attrs = _attrs_dict(point.attributes)
    return Metric(
        id=f"{point.time_unix_nano}:{name}:{hash(tuple(sorted(attrs.items())))}",
        name=name,
        value=float(value),
        unit=unit or "",
        timestamp=_to_datetime(point.time_unix_nano),
        code_filepath=str(attrs.get("code.filepath", "") or ""),
        code_namespace=str(attrs.get("code.namespace", "") or ""),
        code_function=str(attrs.get("code.function", "") or ""),
        code_lineno=int(attrs.get("code.lineno", 0) or 0),
    )


def parse_logs_request(body: bytes) -> list[LogEntry]:
    """Decode an OTLP ExportLogsServiceRequest into LogEntrys."""
    request = ExportLogsServiceRequest()
    request.ParseFromString(body)

    logs = []
    for resource_logs in request.resource_logs:
        for scope_logs in resource_logs.scope_logs:
            for record in scope_logs.log_records:
                attrs = _attrs_dict(record.attributes)
                timestamp_nanos = record.time_unix_nano or record.observed_time_unix_nano
                logs.append(LogEntry(
                    id=f"{record.trace_id.hex()}-{record.span_id.hex()}-{timestamp_nanos}",
                    message=record.body.string_value or "",
                    level=record.severity_text or "",
                    timestamp=_to_datetime(timestamp_nanos) if timestamp_nanos else datetime.datetime.now(datetime.timezone.utc),
                    trace_id=record.trace_id.hex() if record.trace_id else "",
                    span_id=record.span_id.hex() if record.span_id else "",
                    code_filepath=str(attrs.get("code.filepath", "") or ""),
                    code_namespace=str(attrs.get("code.namespace", "") or ""),
                    code_function=str(attrs.get("code.function", "") or ""),
                    code_lineno=int(attrs.get("code.lineno", 0) or 0),
                ))
    return logs