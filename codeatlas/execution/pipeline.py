"""Orchestrates automatic execution of an arbitrary ingested repository:
detect an entry point, run it inside the Docker sandbox, decode whatever
telemetry it produced, and ingest that telemetry into the graph via the
existing (unmodified) identity-resolution pipeline.

This is the piece `/api/execute` calls. It deliberately does no static
analysis itself - callers are expected to have already run `/api/ingest`
for the same repo_root, exactly like the manual-instrumentation OTLP path.
"""

import shutil
from dataclasses import dataclass, field

from ..graph.identity import resolve_and_link_span
from ..graph.issues import write_execution_issues
from ..graph.models import LogEntry
from ..telemetry.otlp_receiver import parse_trace_request
from .entrypoint import detect_entrypoint
from .sandbox import DockerExecutor, docker_availability
from .streaming import read_records
from .subprocess_sandbox import SubprocessExecutor


@dataclass
class ExecutionReport:
    success: bool
    reason: str = ""
    isolation: str = ""
    isolation_warning: str = ""
    entrypoint_source: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    dependency_warning: str = ""
    spans_captured: int = 0
    spans_resolved: int = 0
    truncated: bool = False
    otel_instrumentors_enabled: list = field(default_factory=list)
    crash: dict | None = None


def select_executor():
    """Prefer the Docker sandbox (stronger isolation); fall back to the
    subprocess executor - which still enforces a timeout and captures
    everything, just with weaker guarantees - when Docker isn't running,
    rather than making automatic execution entirely unavailable."""
    available, _ = docker_availability()
    return DockerExecutor() if available else SubprocessExecutor()


def _decode_spans_file(path):
    spans = []
    with open(path, "rb") as f:
        for record in read_records(f):
            spans.extend(parse_trace_request(record))
    return spans


def run_repository(repo, repo_root: str, executor=None) -> ExecutionReport:
    """Detect an entry point, execute `repo_root` in the sandbox, and
    ingest the resulting telemetry into `repo` (a GraphRepository already
    populated by static analysis for the same repo_root).

    Clears prior runtime telemetry first, same reasoning as the
    hand-instrumented sample workload: a RuntimeSpan's id is fresh every
    execution, so without clearing, evidence would be an ever-growing mix
    of every past run instead of reflecting "what does the code do right
    now."""
    executor = executor or select_executor()
    result = executor.run(repo_root)

    if not result.success:
        return ExecutionReport(success=False, reason=result.reason)

    repo.clear_runtime_telemetry()

    spans_captured = 0
    spans_resolved = 0

    span_files = ([result.spans_path] if result.spans_path else []) + list(result.child_spans_paths)
    for path in span_files:
        for span in _decode_spans_file(path):
            spans_captured += 1
            if resolve_and_link_span(repo, span) is not None:
                spans_resolved += 1

    if result.crash:
        _ingest_crash_log(repo, result.crash)

    write_execution_issues(
        repo, timed_out=result.timed_out, exit_code=result.exit_code, has_crash=bool(result.crash),
    )

    return ExecutionReport(
        success=True,
        isolation=result.isolation,
        isolation_warning=result.isolation_warning,
        entrypoint_source=result.entrypoint_source,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        stdout=result.stdout,
        stderr=result.stderr,
        dependency_warning=result.dependency_warning,
        spans_captured=spans_captured,
        spans_resolved=spans_resolved,
        truncated=result.truncated,
        otel_instrumentors_enabled=result.otel_instrumentors_enabled,
        crash=result.crash,
    )


def _ingest_crash_log(repo, crash: dict):
    """An uncaught exception that escaped the entry point entirely (as
    opposed to one that failed inside a traced function, which already
    shows up as an ERROR-status RuntimeSpan) - stored as a process-level
    LogEntry with no code.* attributes, since it has no single owning
    CodeEntity. It's still visible evidence, just not linked to a
    specific function the way a span-level error is."""
    import datetime
    import uuid

    log = LogEntry(
        id=f"crash-{uuid.uuid4().hex}",
        message=f"{crash['type']}: {crash['message']}\n{crash.get('traceback', '')}",
        level="ERROR",
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    repo.upsert_log_entry(log)


def can_execute_automatically(repo_root: str) -> bool:
    """Cheap check the dashboard/API can use before offering the
    "execute" action at all - does not require Docker to be running."""
    return detect_entrypoint(repo_root).found


def cleanup_workspace(path):
    shutil.rmtree(path, ignore_errors=True)
