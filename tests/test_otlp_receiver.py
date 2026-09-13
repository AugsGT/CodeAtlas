"""Tests for the OTLP receiver — the mechanism that lets telemetry from
an ARBITRARY, separately-run repository reach CodeAtlas safely (the
repository runs entirely in its own process and exports over the
network in the standard OTLP wire format; CodeAtlas never executes
that code itself).

These use the REAL standard OpenTelemetry OTLP exporters — the exact
ones any arbitrary instrumented repository would use — captured via a
monkeypatch on their low-level HTTP send, so the test proves genuine
wire-format interop rather than testing against hand-rolled protobuf
bytes that might not match what a real SDK actually produces.
"""

from pathlib import Path

from fastapi.testclient import TestClient
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from codeatlas.api.app import create_app
from codeatlas.graph.models import CodeEntity
from codeatlas.graph.paths import canonical_path_key

SAMPLE_ABS_FILEPATH = str(Path("C:/repo/pkg/math_utils.py"))


class _FakeHttpResponse:
    ok = True
    status_code = 200
    reason = "OK"
    text = ""


def _capture_export_body(exporter_cls):
    """Monkeypatch `_export` (the low-level HTTP send) on an OTLP
    exporter class to capture the serialized protobuf body instead of
    sending it over the network. Returns a dict that gets a "body" key
    once something is exported."""
    captured = {}

    def fake_export(self, serialized_data, deadline_sec=None):
        captured["body"] = serialized_data
        return _FakeHttpResponse()

    exporter_cls._export = fake_export
    return captured


def make_entity(**overrides):
    defaults = dict(
        id="pkg/math_utils.py::add",
        qualified_name="pkg.math_utils.add",
        name="add",
        kind="function",
        module_path="pkg/math_utils.py",
        start_line=1,
        end_line=2,
        abs_path=canonical_path_key(SAMPLE_ABS_FILEPATH),
        local_qualname="add",
    )
    defaults.update(overrides)
    return CodeEntity(**defaults)


def test_receive_traces_links_real_otlp_span_to_known_entity(tmp_path):
    """Sets attributes the same way traced() does (including the
    explicit OK status traced() sets on success — see tracing.py's
    docstring on why OTel doesn't default to OK on its own) but with
    fixed identity values matching make_entity(), rather than using
    traced() directly, which would capture *this test function's own*
    file/module instead of a controlled value to assert against."""
    from opentelemetry.trace import Status, StatusCode

    captured = _capture_export_body(OTLPSpanExporter)

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:9/v1/traces")))
    tracer = provider.get_tracer("external-app")

    with tracer.start_as_current_span("add") as span:
        span.set_attribute("code.filepath", SAMPLE_ABS_FILEPATH)
        span.set_attribute("code.namespace", "pkg.math_utils")
        span.set_attribute("code.function", "add")
        span.set_attribute("code.lineno", 5)
        span.set_status(Status(StatusCode.OK))
        span.set_attribute("codeatlas.return_value", "5")

    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        app.state.repo.upsert_code_entity(make_entity())

        response = client.post(
            "/v1/traces", content=captured["body"], headers={"Content-Type": "application/x-protobuf"}
        )

        assert response.status_code == 200
        spans = app.state.repo.spans_for_entity("pkg/math_utils.py::add")
        assert len(spans) == 1
        assert spans[0]["name"] == "add"

        span_obj = app.state.repo.get_runtime_span(spans[0]["id"])
        assert span_obj.status == "OK"
        assert span_obj.return_value == "5"


def test_receive_traces_captures_real_error_span(tmp_path):
    captured = _capture_export_body(OTLPSpanExporter)

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:9/v1/traces")))
    tracer = provider.get_tracer("external-app")

    try:
        with tracer.start_as_current_span("compute") as span:
            span.set_attribute("code.filepath", SAMPLE_ABS_FILEPATH)
            span.set_attribute("code.function", "compute")
            raise ValueError("bad input")
    except ValueError:
        pass

    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        app.state.repo.upsert_code_entity(make_entity(
            id="pkg/math_utils.py::compute", local_qualname="compute",
            qualified_name="pkg.math_utils.compute", name="compute",
        ))

        response = client.post(
            "/v1/traces", content=captured["body"], headers={"Content-Type": "application/x-protobuf"}
        )
        assert response.status_code == 200

        spans = app.state.repo.spans_for_entity("pkg/math_utils.py::compute")
        assert len(spans) == 1
        span_obj = app.state.repo.get_runtime_span(spans[0]["id"])
        assert span_obj.status == "ERROR"
        assert "ValueError" in span_obj.error_message


def test_receive_metrics_links_real_otlp_metric_to_known_entity(tmp_path):
    captured = _capture_export_body(OTLPMetricExporter)

    exporter = OTLPMetricExporter(endpoint="http://localhost:9/v1/metrics")
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=60_000)
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("external-app")
    counter = meter.create_counter("calls_total")
    counter.add(1, {"code.filepath": SAMPLE_ABS_FILEPATH, "code.function": "add"})
    provider.force_flush()
    provider.shutdown()

    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        app.state.repo.upsert_code_entity(make_entity())

        response = client.post(
            "/v1/metrics", content=captured["body"], headers={"Content-Type": "application/x-protobuf"}
        )

        assert response.status_code == 200
        metrics = app.state.repo.metrics_for_entity("pkg/math_utils.py::add")
        assert len(metrics) == 1
        assert metrics[0]["name"] == "calls_total"
        assert metrics[0]["value"] == 1


def test_receive_logs_links_real_otlp_log_to_known_entity(tmp_path):
    captured = _capture_export_body(OTLPLogExporter)

    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(OTLPLogExporter(endpoint="http://localhost:9/v1/logs")))
    otel_logger = logger_provider.get_logger("external-app")
    otel_logger.emit(
        severity_number=SeverityNumber.WARN,
        severity_text="WARN",
        body="something looked off",
        attributes={"code.filepath": SAMPLE_ABS_FILEPATH, "code.function": "add"},
    )

    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        app.state.repo.upsert_code_entity(make_entity())

        response = client.post(
            "/v1/logs", content=captured["body"], headers={"Content-Type": "application/x-protobuf"}
        )

        assert response.status_code == 200
        logs = app.state.repo.logs_for_entity("pkg/math_utils.py::add")
        assert len(logs) == 1
        assert logs[0]["message"] == "something looked off"


def test_receive_traces_rejects_malformed_body_without_crashing(tmp_path):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        response = client.post(
            "/v1/traces", content=b"not a valid protobuf message at all \xff\xfe",
            headers={"Content-Type": "application/x-protobuf"},
        )
        assert response.status_code == 400
