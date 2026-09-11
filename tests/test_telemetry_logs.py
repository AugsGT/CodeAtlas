from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import set_span_in_context

from codeatlas.graph.models import CodeEntity
from codeatlas.graph.paths import canonical_path_key
from codeatlas.graph.repository import GraphRepository
from codeatlas.telemetry.logs import GraphLogExporter
from codeatlas.telemetry.tracing import GraphSpanExporter


def test_log_emitted_inside_span_correlates_via_emits(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    repo.upsert_code_entity(CodeEntity(
        id="pkg/service.py::run",
        qualified_name="pkg.service.run",
        name="run",
        kind="function",
        module_path="pkg/service.py",
        start_line=1,
        end_line=2,
    ))

    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(GraphSpanExporter(repo)))
    tracer = tracer_provider.get_tracer("test")

    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(GraphLogExporter(repo)))
    otel_logger = logger_provider.get_logger("test")

    with tracer.start_as_current_span("run") as span:
        span.set_attribute("code.filepath", "/repo/pkg/service.py")
        span.set_attribute("code.function", "run")
        span_id = format(span.get_span_context().span_id, "016x")

    # The span is only exported to the graph once it ends (on `with` exit,
    # via SimpleSpanProcessor). Emit the log afterward, explicitly tied to
    # that now-ended span's context, so EMITS correlation has a RuntimeSpan
    # to match against.
    otel_logger.emit(
        context=set_span_in_context(span),
        severity_number=SeverityNumber.ERROR,
        severity_text="ERROR",
        body="something failed",
        attributes={},
    )

    logs = repo.logs_for_span(span_id)
    assert len(logs) == 1
    assert logs[0]["message"] == "something failed"
    assert logs[0]["level"] == "ERROR"

    # No fallback CodeEntity LOGS edge should be created when EMITS succeeded.
    assert repo.logs_for_entity("pkg/service.py::run") == []
    repo.close()


def test_log_without_active_span_falls_back_to_code_identity(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    repo.upsert_code_entity(CodeEntity(
        id="pkg/service.py::run",
        qualified_name="pkg.service.run",
        name="run",
        kind="function",
        module_path="pkg/service.py",
        start_line=1,
        end_line=2,
        abs_path=canonical_path_key("/repo/pkg/service.py"),
        local_qualname="run",
    ))

    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(GraphLogExporter(repo)))
    otel_logger = logger_provider.get_logger("test")

    otel_logger.emit(
        severity_number=SeverityNumber.WARN,
        severity_text="WARN",
        body="no active span here",
        attributes={"code.filepath": "/repo/pkg/service.py", "code.function": "run"},
    )

    logs = repo.logs_for_entity("pkg/service.py::run")
    assert len(logs) == 1
    assert logs[0]["message"] == "no active span here"
    repo.close()


def test_log_with_no_correlation_is_still_stored(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(GraphLogExporter(repo)))
    otel_logger = logger_provider.get_logger("test")

    otel_logger.emit(
        severity_number=SeverityNumber.INFO,
        severity_text="INFO",
        body="just a log",
        attributes={},
    )

    rows = repo.query("MATCH (l:LogEntry {message: 'just a log'}) RETURN l.id")
    assert len(rows) == 1
    repo.close()
