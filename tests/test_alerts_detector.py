"""Proactive alert detection must surface the same problems the
recent_errors/slow_calls retrieval intents already find on-demand -
without a developer having to ask a question first - and must correlate
each alert back to the CodeEntity that produced it whenever the graph
has enough evidence to do so.
"""

import datetime
from pathlib import Path

import pytest

from codeatlas.alerts.detector import detect_alerts
from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.issues import write_static_issues
from codeatlas.graph.models import LogEntry, RuntimeSpan
from codeatlas.graph.repository import GraphRepository

SAMPLE_REPO = Path(__file__).parent.parent / "sample_repo"


@pytest.fixture
def repo_with_problems(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    result = PythonAstAnalyzer().analyze(str(SAMPLE_REPO))
    write_analysis_result(repo, result)

    now = datetime.datetime.now(datetime.timezone.utc)

    failed_span = RuntimeSpan(
        id="span-error", trace_id="t1", name="add",
        start_time=now, end_time=now, duration_ms=1.0, status="ERROR",
        error_message="NameError: name 'c' is not defined",
        code_filepath=str(SAMPLE_REPO / "pkg" / "math_utils.py"), code_function="add",
    )
    repo.upsert_runtime_span(failed_span)
    repo.add_produces("pkg/math_utils.py::add", "span-error")

    slow_span = RuntimeSpan(
        id="span-slow", trace_id="t2", name="add",
        start_time=now, end_time=now, duration_ms=900.0, status="OK",
        code_filepath=str(SAMPLE_REPO / "pkg" / "math_utils.py"), code_function="add",
    )
    repo.upsert_runtime_span(slow_span)
    repo.add_produces("pkg/math_utils.py::add", "span-slow")

    log = LogEntry(id="log-error", message="add failed badly", level="ERROR", timestamp=now, span_id="span-error")
    repo.upsert_log_entry(log)
    repo.add_emits("span-error", "log-error")

    yield repo
    repo.close()


def test_detects_error_span_and_slow_span(repo_with_problems):
    alerts = detect_alerts(repo_with_problems, slow_threshold_ms=500.0)

    severities_and_ids = {(a.severity, a.evidence_id) for a in alerts}
    assert ("error", "span-error") in severities_and_ids
    assert ("error", "log-error") in severities_and_ids
    assert ("warning", "span-slow") in severities_and_ids


def test_alerts_correlate_back_to_producing_entity(repo_with_problems):
    alerts = detect_alerts(repo_with_problems, slow_threshold_ms=500.0)

    by_evidence_id = {a.evidence_id: a for a in alerts}
    assert by_evidence_id["span-error"].entity_id == "pkg/math_utils.py::add"
    assert by_evidence_id["span-slow"].entity_id == "pkg/math_utils.py::add"
    assert by_evidence_id["log-error"].entity_id == "pkg/math_utils.py::add"


def test_no_alerts_when_graph_has_no_runtime_problems(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    result = PythonAstAnalyzer().analyze(str(SAMPLE_REPO))
    write_analysis_result(repo, result)

    assert detect_alerts(repo) == []
    repo.close()


def test_alerts_surfaces_a_static_syntax_error_with_no_execution_at_all(tmp_path):
    """Real bug, found via live use: a repository whose only problem is a
    syntax error (execution never even gets to run) produced ZERO alerts,
    because the alert system only ever queried RuntimeSpan/LogEntry.
    Reading from the unified Issue graph instead fixes this structurally
    - a static issue is stored the same way a runtime one is."""
    (tmp_path / "tt.py").write_text("def broken(:\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(tmp_path))
        write_analysis_result(repo, result)
        write_static_issues(repo, result)

        alerts = detect_alerts(repo)

        assert len(alerts) == 1
        assert alerts[0].severity == "error"
        assert alerts[0].type == "SyntaxError"
        assert alerts[0].detection_method == "static"
        assert alerts[0].file == "tt.py"
        assert "SyntaxError" in alerts[0].message
    finally:
        repo.close()


def test_alerts_are_capped_at_the_limit_even_with_many_low_severity_issues(tmp_path):
    """Quality checks (see analysis/quality.py) can produce many
    low-severity findings on a large repo - the proactive alerts panel
    must stay a short, actionable list (highest severity first), not
    grow unbounded."""
    lines = "".join(f"import os{i}\n" for i in range(10))  # 10 distinct unused imports
    (tmp_path / "many_imports.py").write_text(lines + "\ndef helper():\n    return 1\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(tmp_path))
        write_analysis_result(repo, result)
        write_static_issues(repo, result)

        alerts = detect_alerts(repo, limit=3)

        assert len(alerts) == 3
    finally:
        repo.close()
