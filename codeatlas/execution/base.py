# Shared result type and output-directory decoding for every execution backend (Docker sandbox, subprocess fallback, and any future one). All backends ultimately invoke the same `codeatlas.execution.bootstrap` entry point and write the same `spans.otlp`, `crash.json`, and `summary.json` files into an output directory. Decoding that output is common code, not duplicated per backend.

import json
import os
from dataclasses import dataclass, field

SPANS_FILENAME = "spans.otlp"
CRASH_FILENAME = "crash.json"
SUMMARY_FILENAME = "summary.json"

@dataclass
class ExecutionResult:
    # Represents the result of an execution attempt.
    success: bool  # True if the execution was successful; False otherwise.
    reason: str = ""  # Populated when `success` is False and no execution attempt ran at all.
    isolation: str = ""  # "docker" or "subprocess" - which backend actually ran this.
    isolation_warning: str = ""  # Populated when the backend offers weaker guarantees than the strongest available.
    entrypoint_source: str = ""
    exit_code: int | None = None  # Exit code of the execution, if applicable.
    timed_out: bool = False  # True if the execution timed out; False otherwise.
    stdout: str = ""  # Standard output from the execution.
    stderr: str = ""  # Standard error output from the execution.
    dependency_install_log: str = ""
    dependency_warning: str = ""
    spans_path: str | None = None  # Host path to the primary `spans.otlp` file, if produced.
    child_spans_paths: list = field(default_factory=list)  # Paths to `spans.otlp.<pid>` files from traced child processes.
    crash: dict | None = None  # {"type", "message", "traceback"} if the entry point raised an exception.
    span_count: int = 0  # Number of spans recorded during the execution.
    truncated: bool = False  # True if the output was truncated; False otherwise.
    otel_instrumentors_enabled: list = field(default_factory=list)  # List of OTel instrumentors enabled during the execution.

def read_bootstrap_output(output_dir: str) -> dict:
    """Decode whatever `codeatlas.execution.bootstrap` left in `output_dir`, regardless of which backend ran it. Tolerant of a partially-written or entirely-missing output directory (e.g., the process was killed before writing anything)."""
    
    spans_path = os.path.join(output_dir, SPANS_FILENAME)
    if not os.path.isfile(spans_path):
        spans_path = None

    child_spans_paths = []
    if os.path.isdir(output_dir):
        child_spans_paths = [
            os.path.join(output_dir, name)
            for name in os.listdir(output_dir)
            if name.startswith(SPANS_FILENAME + ".")
        ]

    crash = None
    crash_path = os.path.join(output_dir, CRASH_FILENAME)
    if os.path.isfile(crash_path):
        with open(crash_path, "r", encoding="utf-8") as f:
            crash = json.load(f)

    span_count, truncated, otel_instrumentors_enabled = 0, False, []
    summary_path = os.path.join(output_dir, SUMMARY_FILENAME)
    if os.path.isfile(summary_path):
        with open(summary_path, "r", encoding="utf-8") as f:
            summary = json.load(f)
        span_count = summary.get("span_count", 0)
        truncated = summary.get("truncated", False)
        otel_instrumentors_enabled = summary.get("otel_instrumentors_enabled", [])

    return {
        "spans_path": spans_path,
        "child_spans_paths": child_spans_paths,
        "crash": crash,
        "span_count": span_count,
        "truncated": truncated,
        "otel_instrumentors_enabled": otel_instrumentors_enabled,
    }