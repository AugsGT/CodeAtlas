import datetime
from pathlib import Path

import pytest

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.models import LogEntry, Metric, RuntimeSpan
from codeatlas.graph.repository import GraphRepository
from codeatlas.retrieval.subgraph import SubgraphRetriever

SAMPLE_REPO = Path(__file__).parent.parent / "sample_repo"


@pytest.fixture
def populated_repo(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    result = PythonAstAnalyzer().analyze(str(SAMPLE_REPO))
    write_analysis_result(repo, result)

    now = datetime.datetime.now(datetime.timezone.utc)
    span = RuntimeSpan(
        id="span1", trace_id="trace1", name="add",
        start_time=now, end_time=now, duration_ms=1.5, status="OK",
        code_filepath=str(SAMPLE_REPO / "pkg" / "math_utils.py"),
        code_function="add",
    )
    repo.upsert_runtime_span(span)
    repo.add_produces("pkg/math_utils.py::add", "span1")

    log = LogEntry(
        id="log1", message="add failed", level="ERROR", timestamp=now,
        span_id="span1",
    )
    repo.upsert_log_entry(log)
    repo.add_emits("span1", "log1")

    metric = Metric(
        id="metric1", name="add_calls_total", value=5.0, unit="",
        timestamp=now, code_function="add",
    )
    repo.upsert_metric(metric)
    repo.add_records("pkg/math_utils.py::add", "metric1")

    yield repo
    repo.close()


def test_entity_overview_includes_module_calls_and_spans(populated_repo):
    retriever = SubgraphRetriever(populated_repo)

    evidence = retriever.retrieve("entity_overview", entity_id="pkg/math_utils.py::add")

    assert evidence.intent == "entity_overview"
    node_ids = {n.get("id") or n.get("path") for n in evidence.nodes}
    assert "pkg/math_utils.py::add" in node_ids
    assert "pkg/math_utils.py" in node_ids  # containing module
    assert "span1" in node_ids  # produced span
    edge_types = {e["type"] for e in evidence.edges}
    assert "CONTAINS" in edge_types
    assert "PRODUCES" in edge_types


def test_callers_of_add_includes_calculator_compute(populated_repo):
    retriever = SubgraphRetriever(populated_repo)

    evidence = retriever.retrieve("callers", entity_id="pkg/math_utils.py::add")

    caller_ids = {n["id"] for n in evidence.nodes if n["label"] == "CodeEntity" and n["id"] != "pkg/math_utils.py::add"}
    assert "pkg/service.py::Calculator.compute" in caller_ids


def test_callees_of_square_includes_a_times_a(populated_repo):
    retriever = SubgraphRetriever(populated_repo)

    evidence = retriever.retrieve("callees", entity_id="pkg/math_utils.py::square")

    callee_ids = {n["id"] for n in evidence.nodes if n["label"] == "CodeEntity" and n["id"] != "pkg/math_utils.py::square"}
    assert "pkg/math_utils.py::a_times_a" in callee_ids


def test_callees_respects_depth_bound(populated_repo):
    retriever = SubgraphRetriever(populated_repo)

    # run -> Calculator.compute is depth 1; compute -> add/square is depth 2.
    shallow = retriever.retrieve("callees", entity_id="pkg/service.py::run", max_depth=1)
    deep = retriever.retrieve("callees", entity_id="pkg/service.py::run", max_depth=2)

    shallow_ids = {n["id"] for n in shallow.nodes if n["label"] == "CodeEntity"}
    deep_ids = {n["id"] for n in deep.nodes if n["label"] == "CodeEntity"}
    assert "pkg/math_utils.py::add" not in shallow_ids
    assert "pkg/math_utils.py::add" in deep_ids


def test_runtime_behavior_includes_span_metric_and_log(populated_repo):
    retriever = SubgraphRetriever(populated_repo)

    evidence = retriever.retrieve("runtime_behavior", entity_id="pkg/math_utils.py::add")

    kinds = {n["label"] for n in evidence.nodes}
    assert kinds == {"CodeEntity", "RuntimeSpan", "Metric", "LogEntry"}
    edge_types = {e["type"] for e in evidence.edges}
    assert {"PRODUCES", "RECORDS", "EMITS"} <= edge_types


def test_module_dependencies_returns_depends_on_and_contains(populated_repo):
    retriever = SubgraphRetriever(populated_repo)

    evidence = retriever.retrieve("module_dependencies", module_path="pkg/service.py")

    node_paths = {n.get("path") for n in evidence.nodes if n["label"] == "Module"}
    assert "pkg/math_utils.py" in node_paths
    entity_ids = {n["id"] for n in evidence.nodes if n["label"] == "CodeEntity"}
    assert "pkg/service.py::run" in entity_ids


def test_recent_errors_finds_error_log_via_span(populated_repo):
    retriever = SubgraphRetriever(populated_repo)

    evidence = retriever.retrieve("recent_errors")

    log_messages = {n.get("message") for n in evidence.nodes if n["label"] == "LogEntry"}
    assert "add failed" in log_messages
    entity_ids = {n["id"] for n in evidence.nodes if n["label"] == "CodeEntity"}
    assert "pkg/math_utils.py::add" in entity_ids


def test_recent_errors_finds_error_status_span_with_no_log(populated_repo):
    """A span can fail (uncaught exception) with no LogEntry at all —
    recent_errors must still surface it via the span's own ERROR status."""
    now = datetime.datetime.now(datetime.timezone.utc)
    populated_repo.upsert_runtime_span(RuntimeSpan(
        id="span_err", trace_id="trace2", name="compute",
        start_time=now, end_time=now, duration_ms=0.5, status="ERROR",
        error_message="NameError: name 'c' is not defined",
        code_function="compute",
    ))
    populated_repo.add_produces("pkg/service.py::Calculator.compute", "span_err")
    retriever = SubgraphRetriever(populated_repo)

    evidence = retriever.retrieve("recent_errors")

    error_spans = [n for n in evidence.nodes if n["label"] == "RuntimeSpan" and n.get("id") == "span_err"]
    assert len(error_spans) == 1
    assert error_spans[0]["status"] == "ERROR"
    entity_ids = {n["id"] for n in evidence.nodes if n["label"] == "CodeEntity"}
    assert "pkg/service.py::Calculator.compute" in entity_ids


def test_slow_calls_finds_spans_above_threshold(populated_repo):
    now = datetime.datetime.now(datetime.timezone.utc)
    populated_repo.upsert_runtime_span(RuntimeSpan(
        id="span_slow", trace_id="trace3", name="run",
        start_time=now, end_time=now, duration_ms=500.0, status="OK",
        code_function="run",
    ))
    populated_repo.add_produces("pkg/service.py::run", "span_slow")
    retriever = SubgraphRetriever(populated_repo)

    evidence = retriever.retrieve("slow_calls", threshold_ms=100)

    slow_span_ids = {n["id"] for n in evidence.nodes if n["label"] == "RuntimeSpan"}
    assert "span_slow" in slow_span_ids
    assert "span1" not in slow_span_ids  # duration_ms=1.5, below threshold
    entity_ids = {n["id"] for n in evidence.nodes if n["label"] == "CodeEntity"}
    assert "pkg/service.py::run" in entity_ids


def test_recent_activity_includes_successful_spans_not_just_errors(populated_repo):
    """recent_errors only surfaces failures; recent_activity must surface
    ordinary successful spans too - a call that raised nothing can still
    have produced the wrong (or no) result, which is invisible to an
    errors-only view."""
    retriever = SubgraphRetriever(populated_repo)

    evidence = retriever.retrieve("recent_activity")

    span_ids = {n["id"] for n in evidence.nodes if n["label"] == "RuntimeSpan"}
    assert "span1" in span_ids  # status="OK" in the fixture, not an error
    entity_ids = {n["id"] for n in evidence.nodes if n["label"] == "CodeEntity"}
    assert "pkg/math_utils.py::add" in entity_ids


def test_unknown_intent_raises(populated_repo):
    retriever = SubgraphRetriever(populated_repo)

    with pytest.raises(ValueError):
        retriever.retrieve("not_a_real_intent")
