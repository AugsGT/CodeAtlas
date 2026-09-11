"""The `diagnosis` retrieval intent - "what's wrong with X" for one
named file or function - composes existing, already-tested retrieval
methods (entity_overview, runtime_behavior with callee expansion,
callers, the bare-module query) rather than new Cypher, and must stay
bounded (never "the whole repository") regardless of target shape.

Uses small synthetic repos built fresh per test rather than sample_repo,
per the project's established convention (sample_repo/pkg/service.py is
a live, user-edited sandbox - see conftest.py) - these tests need
specific, stable structures a live file can't guarantee.
"""

import datetime

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.models import RuntimeSpan
from codeatlas.graph.repository import GraphRepository
from codeatlas.retrieval.subgraph import SubgraphRetriever

NOW = datetime.datetime.now(datetime.timezone.utc)


def _ingest(repo, repo_root):
    result = PythonAstAnalyzer().analyze(str(repo_root))
    write_analysis_result(repo, result)
    return result


def _error_span(span_id, entity_id, function, filepath, message="boom"):
    return RuntimeSpan(
        id=span_id, trace_id="t1", name=function, start_time=NOW, end_time=NOW,
        duration_ms=1.0, status="ERROR", error_message=message,
        code_filepath=str(filepath), code_function=function,
    )


def _ok_span(span_id, function, filepath):
    return RuntimeSpan(
        id=span_id, trace_id="t1", name=function, start_time=NOW, end_time=NOW,
        duration_ms=1.0, status="OK", code_filepath=str(filepath), code_function=function,
    )


def test_file_level_diagnosis_includes_module_entities_and_runtime_error(tmp_path):
    (tmp_path / "pricing.py").write_text(
        "def calculate_discounted_price(price, discount_percent):\n"
        "    return price * 100 / (100 - discount_percent)\n\n\n"
        "def calculate_order_total(items, discount_percent):\n"
        "    total = sum(i['price'] for i in items)\n"
        "    return calculate_discounted_price(total, discount_percent)\n"
    )
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        _ingest(repo, tmp_path)
        repo.upsert_runtime_span(_error_span(
            "s1", "pricing.py::calculate_discounted_price", "calculate_discounted_price",
            tmp_path / "pricing.py", message="ZeroDivisionError: division by zero",
        ))
        repo.add_produces("pricing.py::calculate_discounted_price", "s1")

        evidence = SubgraphRetriever(repo).diagnosis("module", "pricing.py")

        labels_and_ids = {(n["label"], n.get("id") or n.get("path")) for n in evidence.nodes}
        assert ("Module", "pricing.py") in labels_and_ids
        assert ("CodeEntity", "pricing.py::calculate_discounted_price") in labels_and_ids
        assert ("CodeEntity", "pricing.py::calculate_order_total") in labels_and_ids
        assert ("RuntimeSpan", "s1") in labels_and_ids
        # The caller of the failing function is pulled in too, not just the
        # failing function in isolation.
        edge_types = {(e["type"], e["from"], e["to"]) for e in evidence.edges}
        assert ("CALLS", "pricing.py::calculate_order_total", "pricing.py::calculate_discounted_price") in edge_types
    finally:
        repo.close()


def test_function_level_diagnosis_includes_overview_and_runtime_evidence(tmp_path):
    (tmp_path / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "service.py").write_text(
        "from math_utils import add\n\n\n"
        "class Calculator:\n"
        "    def compute(self, a, b):\n"
        "        return add(a, b)\n"
    )
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        _ingest(repo, tmp_path)
        repo.upsert_runtime_span(_error_span(
            "s1", "math_utils.py::add", "add", tmp_path / "math_utils.py",
            message="TypeError: unsupported operand type(s)",
        ))
        repo.add_produces("math_utils.py::add", "s1")

        evidence = SubgraphRetriever(repo).diagnosis("entity", "math_utils.py::add")

        node_ids = {n.get("id") for n in evidence.nodes}
        assert "math_utils.py::add" in node_ids
        assert "s1" in node_ids
        # entity_overview's own callers query surfaces the caller too.
        assert "service.py::Calculator.compute" in node_ids
    finally:
        repo.close()


def test_syntax_error_diagnosis_returns_only_the_module_and_its_parse_error(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        _ingest(repo, tmp_path)

        evidence = SubgraphRetriever(repo).diagnosis("module", "broken.py")

        assert len(evidence.nodes) == 1
        module_node = evidence.nodes[0]
        assert module_node["label"] == "Module"
        assert "SyntaxError" in module_node["parse_error"]
        assert evidence.edges == []
    finally:
        repo.close()


def test_diagnosis_with_no_runtime_evidence_still_returns_static_structure(tmp_path):
    (tmp_path / "clean.py").write_text("def helper():\n    return 1\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        _ingest(repo, tmp_path)

        evidence = SubgraphRetriever(repo).diagnosis("module", "clean.py")

        labels = {n["label"] for n in evidence.nodes}
        assert "Module" in labels
        assert "CodeEntity" in labels
        assert "RuntimeSpan" not in labels
    finally:
        repo.close()


def test_diagnosis_surfaces_multiple_errors_in_the_same_file(tmp_path):
    (tmp_path / "multi.py").write_text(
        "def first():\n    return 1\n\n\ndef second():\n    return 2\n"
    )
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        _ingest(repo, tmp_path)
        repo.upsert_runtime_span(_error_span("s1", "multi.py::first", "first", tmp_path / "multi.py", "boom one"))
        repo.add_produces("multi.py::first", "s1")
        repo.upsert_runtime_span(_error_span("s2", "multi.py::second", "second", tmp_path / "multi.py", "boom two"))
        repo.add_produces("multi.py::second", "s2")

        evidence = SubgraphRetriever(repo).diagnosis("module", "multi.py")

        error_messages = {n.get("error_message") for n in evidence.nodes if n["label"] == "RuntimeSpan"}
        assert error_messages == {"boom one", "boom two"}
    finally:
        repo.close()


def test_diagnosis_is_bounded_not_the_whole_repository(tmp_path):
    """Even a diagnosis-worthy module doesn't drag in unrelated files that
    have no CALLS/DEPENDS_ON relationship to it."""
    (tmp_path / "target.py").write_text("def broken_fn():\n    return 1\n")
    (tmp_path / "unrelated.py").write_text("def totally_unrelated():\n    return 2\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        _ingest(repo, tmp_path)
        repo.upsert_runtime_span(_error_span("s1", "target.py::broken_fn", "broken_fn", tmp_path / "target.py"))
        repo.add_produces("target.py::broken_fn", "s1")

        evidence = SubgraphRetriever(repo).diagnosis("module", "target.py")

        node_ids = {n.get("id") or n.get("path") for n in evidence.nodes}
        assert "unrelated.py" not in node_ids
        assert "unrelated.py::totally_unrelated" not in node_ids
    finally:
        repo.close()
