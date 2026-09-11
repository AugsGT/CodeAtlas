"""Issue nodes must actually show up in retrieval evidence, not just
exist in the graph - a schema nobody reads from doesn't fix anything.
Covers the three intents that link to Issue: module_dependencies and
diagnosis (via FOUND_IN), entity_overview/runtime_behavior (via
AFFECTS), and recent_errors (which now also surfaces Issues directly,
regardless of target - see reasoning/pipeline.py's diagnosis fallback).
"""

import datetime

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.issues import sync_runtime_issues, write_static_issues
from codeatlas.graph.models import RuntimeSpan
from codeatlas.graph.repository import GraphRepository
from codeatlas.retrieval.subgraph import SubgraphRetriever

NOW = datetime.datetime.now(datetime.timezone.utc)


def _ingest_with_static_issues(repo, repo_root):
    result = PythonAstAnalyzer().analyze(str(repo_root))
    write_analysis_result(repo, result)
    write_static_issues(repo, result)
    return result


def test_module_dependencies_includes_the_issue_node_for_a_parse_error(tmp_path):
    (tmp_path / "tt.py").write_text("def broken(:\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        _ingest_with_static_issues(repo, tmp_path)

        evidence = SubgraphRetriever(repo).retrieve("module_dependencies", module_path="tt.py")

        labels = {n["label"] for n in evidence.nodes}
        assert "Issue" in labels
        issue_node = next(n for n in evidence.nodes if n["label"] == "Issue")
        assert issue_node["type"] == "SyntaxError"
        assert issue_node["detection_method"] == "static"
        edge_types = {(e["type"], e["to"]) for e in evidence.edges}
        assert ("FOUND_IN", "tt.py") in edge_types
    finally:
        repo.close()


def test_diagnosis_includes_the_issue_node_for_a_parse_error(tmp_path):
    (tmp_path / "tt.py").write_text("def broken(:\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        _ingest_with_static_issues(repo, tmp_path)

        evidence = SubgraphRetriever(repo).diagnosis("module", "tt.py")

        labels = {n["label"] for n in evidence.nodes}
        assert "Issue" in labels
    finally:
        repo.close()


def test_entity_overview_includes_issue_affecting_that_entity(tmp_path):
    (tmp_path / "mod.py").write_text("def broken():\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        write_analysis_result(repo, PythonAstAnalyzer().analyze(str(tmp_path)))
        repo.upsert_runtime_span(RuntimeSpan(
            id="s1", trace_id="t1", name="broken", start_time=NOW, end_time=NOW,
            duration_ms=1.0, status="ERROR", error_message="boom",
        ))
        repo.add_produces("mod.py::broken", "s1")
        sync_runtime_issues(repo)

        evidence = SubgraphRetriever(repo).entity_overview("mod.py::broken")

        labels = {n["label"] for n in evidence.nodes}
        assert "Issue" in labels
        issue_node = next(n for n in evidence.nodes if n["label"] == "Issue")
        assert issue_node["type"] == "UncaughtException"
    finally:
        repo.close()


def test_recent_activity_surfaces_a_static_issue_with_no_target_and_no_execution(tmp_path):
    """Real, live-reported gap: "what happened" (no target) classifies
    directly as recent_activity (not routed through the diagnosis
    no-target fallback at all, since the classifier picks this intent
    on its own for exactly this phrasing) - on a repo that's never been
    executed, this used to answer "I don't have enough evidence" despite
    a real syntax error already sitting in the Alerts panel, because
    recent_activity only ever looked at RuntimeSpan."""
    (tmp_path / "tt.py").write_text("def broken(:\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        _ingest_with_static_issues(repo, tmp_path)

        evidence = SubgraphRetriever(repo).recent_activity()

        labels = {n["label"] for n in evidence.nodes}
        assert "Issue" in labels
    finally:
        repo.close()


def test_recent_errors_surfaces_a_static_issue_with_no_target_and_no_execution(tmp_path):
    """The exact gap found live: "why does this fail" with no named
    target, on a repo that has never been executed at all, used to fall
    through to recent_activity (RuntimeSpan-only) and answer "no
    evidence" even though a syntax error was sitting right there."""
    (tmp_path / "tt.py").write_text("def broken(:\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        _ingest_with_static_issues(repo, tmp_path)

        evidence = SubgraphRetriever(repo).recent_errors()

        labels = {n["label"] for n in evidence.nodes}
        assert "Issue" in labels
    finally:
        repo.close()
