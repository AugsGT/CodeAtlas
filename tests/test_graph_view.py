from pathlib import Path

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.issues import write_static_issues
from codeatlas.graph.models import Issue
from codeatlas.graph.repository import GraphRepository
from codeatlas.retrieval.graph_view import build_repo_graph


def _write_repo(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "a.py").write_text(
        "from b import helper\n\n\ndef caller():\n    return helper()\n"
    )
    (repo_dir / "b.py").write_text("def helper():\n    return 1\n")
    return repo_dir


def test_build_repo_graph_includes_modules_entities_and_structural_edges(tmp_path):
    repo_dir = _write_repo(tmp_path)
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(repo_dir))
        write_analysis_result(repo, result)

        graph = build_repo_graph(repo)

        node_ids = {n["id"] for n in graph["nodes"]}
        assert "module:a.py" in node_ids
        assert "module:b.py" in node_ids
        assert "entity:a.py::caller" in node_ids
        assert "entity:b.py::helper" in node_ids

        edge_types = {(e["source"], e["target"], e["type"]) for e in graph["edges"]}
        assert ("module:a.py", "entity:a.py::caller", "CONTAINS") in edge_types
        assert ("module:b.py", "entity:b.py::helper", "CONTAINS") in edge_types
        assert ("entity:a.py::caller", "entity:b.py::helper", "CALLS") in edge_types
        assert ("module:a.py", "module:b.py", "DEPENDS_ON") in edge_types
    finally:
        repo.close()


def test_build_repo_graph_annotates_nodes_with_the_most_severe_linked_issue(tmp_path):
    repo_dir = _write_repo(tmp_path)
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(repo_dir))
        write_analysis_result(repo, result)
        write_static_issues(repo, result)

        # A second, lower-severity issue on the same module a critical
        # parse-error-shaped issue would already flag - the graph should
        # keep the MORE severe of the two, not the last one written.
        repo.upsert_issue(Issue(
            id="issue:extra-low", type="UnusedImport", severity="low",
            detection_method="static", message="unused", file="a.py", line=1,
        ))
        repo.add_issue_found_in("issue:extra-low", "a.py")
        repo.upsert_issue(Issue(
            id="issue:extra-high", type="UndefinedName", severity="high",
            detection_method="static", message="undefined name", file="", line=5,
        ))
        repo.add_issue_affects("issue:extra-high", "a.py::caller")

        graph = build_repo_graph(repo)
        by_id = {n["id"]: n for n in graph["nodes"]}

        assert by_id["module:a.py"]["severity"] == "low"
        assert by_id["entity:a.py::caller"]["severity"] == "high"
        assert by_id["module:b.py"]["severity"] is None
    finally:
        repo.close()


def test_build_repo_graph_marks_a_parse_error_module_as_critical_even_with_no_issue_node(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "broken.py").write_text("def broken(:\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(repo_dir))
        write_analysis_result(repo, result)

        graph = build_repo_graph(repo)

        by_id = {n["id"]: n for n in graph["nodes"]}
        assert by_id["module:broken.py"]["severity"] == "critical"
    finally:
        repo.close()
