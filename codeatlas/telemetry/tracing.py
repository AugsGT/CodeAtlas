# OpenTelemetry tracing: instrumentation plus a graph-backed exporter.

import functools
import inspect

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from ..graph.identity import resolve_and_link_span
from ..graph.models import RuntimeSpan


def traced(tracer):
    """Decorator factory: wraps a function so each call emits a span
    carrying its code identity attributes.
    """

    def decorator(func):
        filepath = inspect.getsourcefile(func) or ""
        try:
            _, lineno = inspect.getsourcelines(func)
        except (OSError, TypeError):
            lineno = getattr(func.__code__, "co_firstlineno", 0)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with tracer.start_as_current_span(func.__qualname__) as span:
                span.set_attribute("code.filepath", filepath)
                span.set_attribute("code.namespace", func.__module__)
                span.set_attribute("code.function", func.__qualname__)
                span.set_attribute("code.lineno", lineno)
                result = func(*args, **kwargs)
                # Only reached on success — if func raises, this is
                # skipped and the span's ERROR status/error_message
                # (set automatically by the `with` block above) is the
                # only evidence, same as before. OTel does NOT default a
                # successful span's status to OK on its own (it stays
                # UNSET unless something sets it), and "UNSET" reads as
                # ambiguous evidence to a model deciding whether a call
                # succeeded — so this sets it explicitly.
                span.set_status(Status(StatusCode.OK))
                span.set_attribute("codeatlas.return_value", _safe_repr(result))
                return result

        return wrapper

    return decorator


def _safe_repr(value, max_len=500):
    """repr() a return value for evidence display, tolerating both
    objects whose __repr__ raises and ones large enough to bloat every
    answer's evidence with (e.g. accidentally returning a big list)."""
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - repr() can raise arbitrary user code
        return f"<unrepresentable {type(value).__name__}>"
    if len(text) > max_len:
        return text[:max_len] + "...(truncated)"
    return text


def make_in_memory_tracer():
    """Build a tracer backed by an in-memory exporter — no collector
    needed. Used for tests and for Phase 2 validation.
    """
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("codeatlas")
    return tracer, exporter


def make_otlp_tracer(service_name="codeatlas-instrumented-app", endpoint="http://localhost:8000/v1/traces"):
    """Build a tracer for instrumenting an ARBITRARY repository's own
    code, in that repository's own process, exporting to a running
    CodeAtlas server's OTLP receiver over the network (see
    telemetry/otlp_receiver.py). This is the safe alternative to
    GraphSpanExporter for any code CodeAtlas doesn't execute itself:

        from codeatlas.telemetry.tracing import make_otlp_tracer, traced

        tracer = make_otlp_tracer(endpoint="http://localhost:8000/v1/traces")

        @traced(tracer)
        def my_function(...): ...

    Requires opentelemetry-exporter-otlp-proto-http (a CodeAtlas
    dependency already, so nothing extra to install if you're importing
    this from a project that has CodeAtlas as a dependency).

    Uses SimpleSpanProcessor (synchronous, one HTTP POST per finished
    span) rather than BatchSpanProcessor: the target use case here is a
    script or short-lived workload where the point is "run it once, see
    the evidence in CodeAtlas" — with batching, a process that exits
    without calling provider.shutdown()/force_flush() can lose spans
    sitting in the batch buffer, which is exactly the kind of surprise
    this helper exists to avoid. A long-running production service that
    actually needs batching for throughput can build its own
    TracerProvider with BatchSpanProcessor(OTLPSpanExporter(...)) instead
    of using this helper."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    return provider.get_tracer(service_name)


def to_runtime_span(readable_span) -> RuntimeSpan:
    """Convert a finished OpenTelemetry ReadableSpan into our RuntimeSpan."""
    ctx = readable_span.get_span_context()
    attrs = readable_span.attributes or {}
    duration_ms = (readable_span.end_time - readable_span.start_time) / 1e6
    return RuntimeSpan(
        id=format(ctx.span_id, "016x"),
        trace_id=format(ctx.trace_id, "032x"),
        name=readable_span.name,
        start_time=_to_datetime(readable_span.start_time),
        end_time=_to_datetime(readable_span.end_time),
        duration_ms=duration_ms,
        status=readable_span.status.status_code.name,
        error_message=readable_span.status.description or "",
        return_value=attrs.get("codeatlas.return_value", ""),
        code_filepath=attrs.get("code.filepath", ""),
        code_namespace=attrs.get("code.namespace", ""),
        code_function=attrs.get("code.function", ""),
        code_lineno=attrs.get("code.lineno", 0),
    )


def _to_datetime(epoch_nanos):
    import datetime

    return datetime.datetime.fromtimestamp(epoch_nanos / 1e9, tz=datetime.timezone.utc)


class GraphSpanExporter(SpanExporter):
    """SpanExporter that writes finished spans directly into the graph,
    resolving each one to its producing CodeEntity via identity.py (no
    "repo root" needed — resolution matches on the span's own qualified
    name / canonical file path against whatever's already ingested).

    Wire it up like any other OTel exporter:

        provider.add_span_processor(SimpleSpanProcessor(GraphSpanExporter(repo)))
    """

    def __init__(self, repo):
        self.repo = repo

    def export(self, spans):
        for readable_span in spans:
            runtime_span = to_runtime_span(readable_span)
            resolve_and_link_span(self.repo, runtime_span)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass