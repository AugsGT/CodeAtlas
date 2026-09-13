"""In-memory representations of graph node kinds.

These are the canonical objects the rest of the pipeline (static
analyzer, telemetry ingestion, identity resolution) builds and passes
to the repository, rather than raw dicts or positional kwargs.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Module:
    path: str
    name: str
    language: str
    # Set when the file exists but couldn't be parsed (e.g. a real syntax
    # error) - the module is still recorded (with no entities/calls/
    # depends_on, since none could be extracted) rather than silently
    # disappearing from the graph, so a question about that specific file
    # can surface "it doesn't even parse" instead of getting no evidence
    # or, worse, evidence about some other file entirely.
    parse_error: str = ""
    # Canonical absolute path (see graph/paths.py::canonical_path_key),
    # populated even for a module that failed to parse (the file itself
    # still exists on disk). Lets the dashboard's file viewer read a
    # module's real source without the caller needing to know or resupply
    # whatever "repo root" was used at ingest time - the same
    # root-independence reasoning as CodeEntity.abs_path.
    abs_path: str = ""


@dataclass
class CodeEntity:
    id: str
    qualified_name: str
    name: str
    kind: str
    module_path: str
    start_line: int
    end_line: int
    # Runtime-identity matching fields, independent of whatever directory
    # was treated as the analysis "repo root" (see graph/identity.py):
    # abs_path is the canonical_path_key() of this entity's source file's
    # real absolute path, and local_qualname is the qualified name *without*
    # the module-dotted prefix (e.g. "Calculator.compute", matching
    # OpenTelemetry's code.function attribute directly).
    abs_path: str = ""
    local_qualname: str = ""


@dataclass
class RuntimeSpan:
    id: str
    trace_id: str
    name: str
    start_time: datetime
    end_time: datetime
    duration_ms: float
    status: str
    # Populated from the span's status description when status is ERROR
    # (OpenTelemetry sets this automatically when an exception propagates
    # out of a traced call), e.g. "NameError: name 'c' is not defined".
    error_message: str = ""
    # repr() of what the call returned, captured only on success (a
    # bug that produces a wrong-but-not-crashing result raises no
    # exception, so status/error_message alone can't surface it - this
    # can). Empty if the call raised, or if it wasn't captured.
    return_value: str = ""
    # Identity attributes used to resolve this span back to a CodeEntity.
    # Follow OpenTelemetry's "code.*" semantic conventions.
    code_filepath: str = ""
    code_namespace: str = ""
    code_function: str = ""
    code_lineno: int = 0


@dataclass
class Metric:
    id: str
    name: str
    value: float
    unit: str
    timestamp: datetime
    code_filepath: str = ""
    code_namespace: str = ""
    code_function: str = ""
    code_lineno: int = 0


@dataclass
class Issue:
    """A single, first-class representation of "something is wrong",
    normalized from whichever raw signal found it (a Module's
    parse_error, a failed/slow RuntimeSpan, an ERROR/WARN LogEntry, a
    non-zero exit code, a timeout) so alerts, retrieval, and reasoning
    all read one consistent shape instead of each having to know the
    raw signals' different properties separately. See graph/issues.py
    for how these get created.
    """
    id: str
    type: str  # e.g. "SyntaxError", "UncaughtException", "SlowCall", "Timeout", "NonZeroExit"
    severity: str  # "critical" | "high" | "medium" | "low" | "info"
    # "static" (found by AST analysis, before anything ran) or "runtime"
    # (found from RuntimeSpan/LogEntry/execution-outcome evidence).
    # "telemetry" and "inferred" are reserved for future use (externally
    # supplied OTLP telemetry currently isn't distinguished from
    # CodeAtlas's own sandboxed execution at the RuntimeSpan level, and
    # nothing yet produces LLM-inferred issues).
    detection_method: str
    message: str
    file: str = ""
    line: int = 0
    status: str = "open"


@dataclass
class LogEntry:
    id: str
    message: str
    level: str
    timestamp: datetime
    trace_id: str = ""
    span_id: str = ""
    code_filepath: str = ""
    code_namespace: str = ""
    code_function: str = ""
    code_lineno: int = 0
