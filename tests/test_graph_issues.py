"""The unified Issue node: a single representation of "something is
wrong" that static analysis, execution telemetry, and process-level
execution outcomes all write into, so alerts/retrieval/reasoning read
one consistent shape instead of three different raw ones
(Module.parse_error, RuntimeSpan.status, LogEntry.level).

Real bug this fixes, reproduced end-to-end here: a repository with
nothing but a syntax error (no execution possible at all) used to
produce zero alerts, because the alert system only ever queried runtime
evidence and never looked at Module.parse_error.
"""

import datetime

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.issues import (
    NONZERO_EXIT_ISSUE_ID,
    TIMEOUT_ISSUE_ID,
    sync_runtime_issues,
    write_execution_issues,
    write_static_issues,
)
from codeatlas.graph.models import LogEntry, RuntimeSpan
from codeatlas.graph.repository import GraphRepository

NOW = datetime.datetime.now(datetime.timezone.utc)


def test_write_static_issues_creates_issue_for_parse_error(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(tmp_path))
        write_analysis_result(repo, result)

        issues = write_static_issues(repo, result)

        assert len(issues) == 1
        assert issues[0].type == "SyntaxError"
        assert issues[0].severity == "critical"
        assert issues[0].detection_method == "static"
        assert issues[0].file == "broken.py"
        assert issues[0].line == 1

        stored = repo.list_issues()
        assert len(stored) == 1
        assert stored[0]["module_path"] == "broken.py"
    finally:
        repo.close()


def test_write_static_issues_creates_nothing_for_a_clean_module(tmp_path):
    (tmp_path / "clean.py").write_text("def helper():\n    return 1\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(tmp_path))
        write_analysis_result(repo, result)

        issues = write_static_issues(repo, result)

        assert issues == []
        assert repo.list_issues() == []
    finally:
        repo.close()


def test_sync_runtime_issues_covers_span_errors_slow_calls_and_logs(tmp_path):
    (tmp_path / "mod.py").write_text("def slow():\n    pass\n\n\ndef broken():\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        write_analysis_result(repo, PythonAstAnalyzer().analyze(str(tmp_path)))

        repo.upsert_runtime_span(RuntimeSpan(
            id="s-error", trace_id="t1", name="broken", start_time=NOW, end_time=NOW,
            duration_ms=1.0, status="ERROR", error_message="boom",
        ))
        repo.add_produces("mod.py::broken", "s-error")

        repo.upsert_runtime_span(RuntimeSpan(
            id="s-slow", trace_id="t1", name="slow", start_time=NOW, end_time=NOW,
            duration_ms=900.0, status="OK",
        ))
        repo.add_produces("mod.py::slow", "s-slow")

        repo.upsert_log_entry(LogEntry(
            id="l-error", message="broken failed badly", level="ERROR", timestamp=NOW, span_id="s-error",
        ))
        repo.add_emits("s-error", "l-error")

        issues = sync_runtime_issues(repo, slow_threshold_ms=500.0)

        by_type = {i.type for i in issues}
        assert by_type == {"UncaughtException", "SlowCall", "LoggedError"}

        stored = {i["id"]: i for i in repo.list_issues()}
        assert stored["issue:span-error:s-error"]["entity_id"] == "mod.py::broken"
        # A runtime issue's file/line (the affected entity's own file and
        # start line) must be populated - without it, the dashboard's code
        # viewer has no way to place a runtime error inline against any
        # file at all (a real gap found live: /api/issues?file=... could
        # never match a runtime issue, since neither field was ever set).
        assert stored["issue:span-error:s-error"]["file"] == "mod.py"
        assert stored["issue:span-error:s-error"]["line"] == 5
        assert stored["issue:slow-call:s-slow"]["file"] == "mod.py"
        # The log is resolved to its entity via the span that emitted it
        # (EMITS), not a direct owning span of its own.
        assert stored["issue:log:l-error"]["file"] == "mod.py"
        assert stored["issue:span-error:s-error"]["span_id"] == "s-error"
        assert stored["issue:slow-call:s-slow"]["entity_id"] == "mod.py::slow"
        assert stored["issue:log:l-error"]["log_id"] == "l-error"
    finally:
        repo.close()


def test_sync_runtime_issues_leaves_file_empty_for_a_log_with_no_owning_span(tmp_path):
    """A process-level crash log (no EMITS edge to any span at all, since
    the crash happened outside any traced function) has no entity to
    attribute file/line to - left empty rather than guessed, not defaulted
    to some other file."""
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        repo.upsert_log_entry(LogEntry(id="crash-1", message="boom", level="ERROR", timestamp=NOW))

        sync_runtime_issues(repo)

        issue = repo.list_issues()[0]
        assert issue["file"] == ""
        assert issue["line"] == 0
        assert issue["entity_id"] is None
    finally:
        repo.close()


def test_sync_runtime_issues_is_idempotent_not_cumulative(tmp_path):
    (tmp_path / "mod.py").write_text("def broken():\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        write_analysis_result(repo, PythonAstAnalyzer().analyze(str(tmp_path)))
        repo.upsert_runtime_span(RuntimeSpan(
            id="s-error", trace_id="t1", name="broken", start_time=NOW, end_time=NOW,
            duration_ms=1.0, status="ERROR", error_message="boom",
        ))
        repo.add_produces("mod.py::broken", "s-error")

        sync_runtime_issues(repo)
        sync_runtime_issues(repo)
        sync_runtime_issues(repo)

        assert len(repo.list_issues()) == 1
    finally:
        repo.close()


def test_sync_runtime_issues_clears_issues_for_spans_that_no_longer_exist(tmp_path):
    (tmp_path / "mod.py").write_text("def broken():\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        write_analysis_result(repo, PythonAstAnalyzer().analyze(str(tmp_path)))
        repo.upsert_runtime_span(RuntimeSpan(
            id="s-error", trace_id="t1", name="broken", start_time=NOW, end_time=NOW,
            duration_ms=1.0, status="ERROR", error_message="boom",
        ))
        repo.add_produces("mod.py::broken", "s-error")
        sync_runtime_issues(repo)
        assert len(repo.list_issues()) == 1

        repo.clear_runtime_telemetry()
        sync_runtime_issues(repo)

        assert repo.list_issues() == []
    finally:
        repo.close()


def test_write_execution_issues_reports_timeout(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        issues = write_execution_issues(repo, timed_out=True, exit_code=None, has_crash=False)
        assert len(issues) == 1
        assert issues[0].id == TIMEOUT_ISSUE_ID
        assert issues[0].type == "Timeout"
    finally:
        repo.close()


def test_write_execution_issues_reports_bare_nonzero_exit(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        issues = write_execution_issues(repo, timed_out=False, exit_code=1, has_crash=False)
        assert len(issues) == 1
        assert issues[0].id == NONZERO_EXIT_ISSUE_ID
        assert issues[0].type == "NonZeroExit"
    finally:
        repo.close()


def test_write_execution_issues_defers_to_crash_when_one_was_captured(tmp_path):
    """A non-zero exit caused by an uncaught exception already gets a
    more specific LoggedError issue from the crash log (via
    sync_runtime_issues) - reporting a second, vaguer NonZeroExit issue
    for the same underlying failure would be redundant noise."""
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        issues = write_execution_issues(repo, timed_out=False, exit_code=1, has_crash=True)
        assert issues == []
        assert repo.list_issues() == []
    finally:
        repo.close()


def test_write_execution_issues_clears_stale_issue_from_a_previous_run(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        write_execution_issues(repo, timed_out=True, exit_code=None, has_crash=False)
        assert len(repo.list_issues()) == 1

        write_execution_issues(repo, timed_out=False, exit_code=0, has_crash=False)

        assert repo.list_issues() == []
    finally:
        repo.close()
