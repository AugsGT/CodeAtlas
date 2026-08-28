# This file contains a Python function-level tracer that uses `sys.settrace` to automatically instrument an arbitrary repository with no source modification. It captures every call/return/exception in the target repository's own code.

import os
import sys
import threading
import time

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode, set_span_in_context

from ..graph.paths import canonical_path_key
from .streaming import append_record

DEFAULT_MAX_SPANS = 20_000


def _safe_repr(value, max_len=500):
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - repr() can raise arbitrary user code
        return f"<unrepresentable {type(value).__name__}>"
    if len(text) > max_len:
        return text[:max_len] + "...(truncated)"
    return text


class StreamingSpanFileExporter(SpanExporter):
    """Appends each finished span to `filepath` immediately, encoded as a
    standard OTLP `ExportTraceServiceRequest` (the exact wire format the
    existing `telemetry/otlp_receiver.py` already decodes) - so a crash or
    a hard timeout-kill loses at most the one call still in flight, never
    everything captured so far. See streaming.py for why append-per-span,
    not buffer-and-write-once."""

    def __init__(self, filepath):
        self._filepath = filepath
        self._fileobj = open(filepath, "ab")

    def export(self, spans):
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

        if spans:
            request = encode_spans(spans)
            append_record(self._fileobj, request.SerializeToString())
        return SpanExportResult.SUCCESS

    def shutdown(self):
        try:
            self._fileobj.close()
        except OSError:
            pass


def _is_repo_file(filename, repo_root_key):
    try:
        key = canonical_path_key(filename)
    except (OSError, ValueError):
        return False
    return key == repo_root_key or key.startswith(repo_root_key + "/")


class RepoTracer:
    """Installs a `sys.settrace`/`threading.settrace` hook that emits one
    OpenTelemetry span per call into a file whose source is under
    `repo_root`, with correct parent/child nesting derived from the
    Python call stack itself (no cooperation from the traced code)."""

    def __init__(self, tracer, repo_root, max_spans=DEFAULT_MAX_SPANS):
        self._tracer = tracer
        self._repo_root_key = canonical_path_key(os.path.abspath(repo_root))
        self._max_spans = max_spans
        self._span_count = 0
        self._truncated = False
        # frame id -> (span, exception_recorded_or_None)
        self._active = {}
        self._lock = threading.Lock()

    @property
    def truncated(self):
        return self._truncated

    @property
    def span_count(self):
        return self._span_count

    def install(self):
        sys.settrace(self._on_event)
        threading.settrace(self._on_event)

    def uninstall(self):
        sys.settrace(None)
        threading.settrace(None)

    def _on_event(self, frame, event, arg):
        if event == "call":
            return self._on_call(frame)
        entry = self._active.get(id(frame))
        if entry is None:
            return None
        if event == "return":
            self._on_return(frame, arg)
        elif event == "exception":
            self._on_exception(frame, arg)
        return self._on_event

    def _on_call(self, frame):
        code = frame.f_code
        if not _is_repo_file(code.co_filename, self._repo_root_key):
            return None  # not repository code: don't trace this frame's body
        if code.co_name == "<module>":
            # Module-level top-level code (mostly import-time execution) -
            # never corresponds to a CodeEntity (static analysis only
            # extracts functions/classes/methods), so recording it would
            # just be unresolvable evidence noise. Still descend into
            # anything it calls (returning None here only stops tracing
            # this one frame, not its callees - see the module docstring).
            return None

        with self._lock:
            if self._span_count >= self._max_spans:
                self._truncated = True
                return None
            self._span_count += 1

        parent_frame = frame.f_back
        parent_entry = self._active.get(id(parent_frame)) if parent_frame is not None else None
        parent_context = set_span_in_context(parent_entry[0]) if parent_entry else None

        qualname = getattr(code, "co_qualname", code.co_name)  # co_qualname: Python 3.11+
        span = self._tracer.start_span(qualname, context=parent_context, start_time=time.time_ns())
        span.set_attribute("code.filepath", code.co_filename)
        span.set_attribute("code.namespace", frame.f_globals.get("__name__", ""))
        span.set_attribute("code.function", qualname)
        span.set_attribute("code.lineno", code.co_firstlineno)

        self._active[id(frame)] = (span, None)
        return self._on_event

    def _on_exception(self, frame, arg):
        span, _ = self._active[id(frame)]
        exc_type, exc_value, _ = arg
        self._active[id(frame)] = (span, exc_value)

    def _on_return(self, frame, arg):
        span, exc_value = self._active.pop(id(frame))
        # `sys.settrace` delivers a `return` event with arg=None both when
        # a function genuinely returns None AND when its frame is popped
        # because an exception propagated through it - the two are
        # indistinguishable from the `return` event alone. Treating "an
        # `exception` event fired for this frame, and the frame's `return`
        # event carries no value" as an error is correct for the common
        # and important case (an uncaught, propagating exception) but can
        # false-positive on a function that catches an exception
        # internally and then legitimately returns None itself - a real,
        # documented limitation of settrace-based tracing (see module
        # docstring), not something worth a more invasive workaround for.
        still_propagating = exc_value is not None and arg is None
        if still_propagating:
            span.set_status(Status(StatusCode.ERROR, description=f"{type(exc_value).__name__}: {exc_value}"))
        else:
            span.set_status(Status(StatusCode.OK))
            span.set_attribute("codeatlas.return_value", _safe_repr(arg))
        span.end(end_time=time.time_ns())


def make_streaming_tracer(repo_root, spans_output_path, max_spans=DEFAULT_MAX_SPANS):
    """Build a TracerProvider + RepoTracer pair wired to durably stream
    finished spans to `spans_output_path` as they complete. Returns
    (tracer_provider, repo_tracer) - call repo_tracer.install() to start
    capturing, and tracer_provider.shutdown() when execution finishes."""
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(StreamingSpanFileExporter(spans_output_path)))
    tracer = provider.get_tracer("codeatlas-auto-instrumentation")
    repo_tracer = RepoTracer(tracer, repo_root, max_spans=max_spans)
    return provider, repo_tracer


ENV_REPO_ROOT = "CODEATLAS_TRACE_REPO_ROOT"
ENV_SPANS_OUTPUT = "CODEATLAS_TRACE_SPANS_OUTPUT"

SITECUSTOMIZE_SOURCE = '''\
# Written by codeatlas.execution.tracer for child-process propagation: a
# Python child process that inherits PYTHONPATH (and so imports this file
# automatically at startup) and the CODEATLAS_TRACE_* environment
# variables gets the same repository tracing installed automatically.
# A non-Python child, or one launched with a cleared environment or
# `-S`/`-I`, is not affected - see tracer.py's module docstring.
import os as _os

if _os.environ.get("CODEATLAS_TRACE_REPO_ROOT"):
    try:
        from codeatlas.execution.tracer import auto_install_from_env
        auto_install_from_env()
    except Exception:
        pass
'''


def auto_install_from_env():
    """Install tracing using repo root / output path from environment
    variables (see SITECUSTOMIZE_SOURCE) - how a traced Python child
    process picks up the same tracer as its parent, with each process
    writing to its own PID-suffixed output file to avoid concurrent
    writers on one file."""
    repo_root = os.environ.get(ENV_REPO_ROOT)
    output_path = os.environ.get(ENV_SPANS_OUTPUT)
    if not repo_root or not output_path:
        return None

    child_output_path = f"{output_path}.{os.getpid()}"
    provider, repo_tracer = make_streaming_tracer(repo_root, child_output_path)
    repo_tracer.install()

    import atexit

    def _shutdown():
        repo_tracer.uninstall()
        provider.shutdown()

    atexit.register(_shutdown)
    return provider, repo_tracer