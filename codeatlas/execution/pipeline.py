# Orchestrates automatic execution of an arbitrary ingested repository:
# detects an entry point, runs it inside the Docker sandbox, decodes whatever
# telemetry it produced, and ingests that telemetry into the graph via the
# existing (unmodified) identity-resolution pipeline.

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
    # Represents the result of an execution attempt.
    success: bool  # Whether the execution was successful.
    reason: str = ""  # Reason for failure if not successful.
    isolation: str = ""  # Type of isolation used.
    isolation_warning: str = ""  # Warning related to isolation.
    entrypoint_source: str = ""  # Source of the detected entry point.
    exit_code: int | None = None  # Exit code of the execution.
    timed_out: bool = False  # Whether the execution timed out.
    stdout: str = ""  # Standard output from the execution.
    stderr: str = ""  # Standard error from the execution.
    dependency_warning: str = ""  # Warning related to dependencies.
    spans_captured: int = 0  # Number of spans captured.
    spans_resolved: int = 0  # Number of spans resolved.
    truncated: bool = False  # Whether the output was truncated.
    otel_instrumentors_enabled: list = field(default_factory=list)  # List of enabled OTLP instrumentors.
    crash: dict | None = None  # Information about any crashes.


def select_executor():
    """Selects the appropriate executor for running a repository.
    Prefers DockerExecutor if available, otherwise uses SubprocessExecutor."""
    available, _ = docker_availability()
    return DockerExecutor() if available else SubprocessExecutor()


def _decode_spans_file(path):
    # Decodes spans from a file.
    spans = []
    with open(path, "rb") as f:
        for record in read_records(f):
            spans.extend(parse_trace_request(record))
    return spans


def run_repository(repo, repo_root: str, executor=None) -> ExecutionReport:
    """Detects an entry point, executes the repository in a sandbox,
    and ingests the resulting telemetry into the graph.
    
    Clears prior runtime telemetry to ensure fresh data."""
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
    """Ingests a crash log into the repository.
    Stores it as a process-level LogEntry."""
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
    """Checks if the repository can be executed automatically.
    Does not require Docker to be running."""
    return detect_entrypoint(repo_root).found


def cleanup_workspace(path):
    shutil.rmtree(path, ignore_errors=True)