"""Evidence gathering needs to go deeper than a single flagged entity's
own facts: it should show the code itself (not just its shape in the
graph) and, when a problem is found, the neighboring calls most likely
responsible for it - the exact "payment.process_payment is slow because
database.save_transaction times out" chain from the project's own spec.

Uses a small synthetic repo built fresh per test rather than sample_repo:
sample_repo/pkg/service.py is the user's own live-edited sandbox (see
conftest.py) and these tests need a specific, stable call structure
(Calculator.compute calls add), so - per the project's own testing
convention - they own their fixture instead of depending on sample_repo's
current content.
"""

import datetime

import pytest

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.models import RuntimeSpan
from codeatlas.graph.repository import GraphRepository
from codeatlas.retrieval.subgraph import SubgraphRetriever


def _write_synthetic_repo(root):
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
    (pkg / "service.py").write_text(
        "from pkg.math_utils import add\n\n\n"
        "class Calculator:\n"
        "    def compute(self, a, b):\n"
        "        return add(a, b)\n"
    )
    return root


@pytest.fixture
def synthetic_repo(tmp_path):
    return _write_synthetic_repo(tmp_path / "synth_repo")


@pytest.fixture
def repo_with_chained_problem(tmp_path, synthetic_repo):
    """add() is slow, and add() is called by Calculator.compute() - a
    two-entity chain where the caller's problem is really the callee's."""
    repo = GraphRepository(tmp_path / "graph.db")
    result = PythonAstAnalyzer().analyze(str(synthetic_repo))
    write_analysis_result(repo, result)

    now = datetime.datetime.now(datetime.timezone.utc)
    slow_span = RuntimeSpan(
        id="span-slow", trace_id="t1", name="add",
        start_time=now, end_time=now, duration_ms=900.0, status="OK",
        code_filepath=str(synthetic_repo / "pkg" / "math_utils.py"), code_function="add",
    )
    repo.upsert_runtime_span(slow_span)
    repo.add_produces("pkg/math_utils.py::add", "span-slow")

    caller_span = RuntimeSpan(
        id="span-caller", trace_id="t1", name="compute",
        start_time=now, end_time=now, duration_ms=910.0, status="OK",
        code_filepath=str(synthetic_repo / "pkg" / "service.py"), code_function="Calculator.compute",
    )
    repo.upsert_runtime_span(caller_span)
    repo.add_produces("pkg/service.py::Calculator.compute", "span-caller")

    yield repo
    repo.close()


def test_source_snippet_attached_to_function_entities(repo_with_chained_problem):
    retriever = SubgraphRetriever(repo_with_chained_problem)

    evidence = retriever.retrieve("entity_overview", entity_id="pkg/math_utils.py::add")

    add_node = next(n for n in evidence.nodes if n.get("id") == "pkg/math_utils.py::add")
    assert "source" in add_node
    assert "def add" in add_node["source"]


def test_source_snippet_not_attached_to_classes_or_modules(repo_with_chained_problem):
    retriever = SubgraphRetriever(repo_with_chained_problem)

    evidence = retriever.retrieve("module_dependencies", module_path="pkg/service.py")

    module_node = next(n for n in evidence.nodes if n.get("label") == "Module")
    assert "source" not in module_node


def test_slow_calls_correlates_caller_and_callee_evidence(repo_with_chained_problem):
    retriever = SubgraphRetriever(repo_with_chained_problem)

    evidence = retriever.retrieve("slow_calls", threshold_ms=500.0)

    node_ids = {n.get("id") for n in evidence.nodes}
    # Both the directly-flagged slow entity AND the one that calls it
    # (pulled in via correlation, not the original slow_calls query alone)
    # must be present, connected by the real CALLS edge between them.
    assert "pkg/math_utils.py::add" in node_ids
    assert "pkg/service.py::Calculator.compute" in node_ids
    edge_pairs = {(e["type"], e["from"], e["to"]) for e in evidence.edges}
    assert ("CALLS", "pkg/service.py::Calculator.compute", "pkg/math_utils.py::add") in edge_pairs

    # And the correlated entity's own runtime span too, not just its id.
    assert "span-caller" in node_ids


def test_recent_errors_correlates_neighboring_calls(tmp_path):
    synthetic_repo = _write_synthetic_repo(tmp_path / "synth_repo")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(synthetic_repo))
        write_analysis_result(repo, result)

        now = datetime.datetime.now(datetime.timezone.utc)
        failed_span = RuntimeSpan(
            id="span-fail", trace_id="t1", name="add",
            start_time=now, end_time=now, duration_ms=1.0, status="ERROR",
            error_message="boom", code_filepath=str(synthetic_repo / "pkg" / "math_utils.py"),
            code_function="add",
        )
        repo.upsert_runtime_span(failed_span)
        repo.add_produces("pkg/math_utils.py::add", "span-fail")

        retriever = SubgraphRetriever(repo)
        evidence = retriever.retrieve("recent_errors")

        node_ids = {n.get("id") for n in evidence.nodes}
        assert "pkg/math_utils.py::add" in node_ids
        # Calculator.compute calls add() - correlation should surface it
        # as the affected caller even though it has no error of its own.
        assert "pkg/service.py::Calculator.compute" in node_ids
    finally:
        repo.close()


def test_runtime_behavior_expand_callees_can_be_disabled(repo_with_chained_problem):
    retriever = SubgraphRetriever(repo_with_chained_problem)

    expanded = retriever.runtime_behavior("pkg/service.py::Calculator.compute", expand_callees=True)
    bare = retriever.runtime_behavior("pkg/service.py::Calculator.compute", expand_callees=False)

    expanded_ids = {n.get("id") for n in expanded.nodes}
    bare_ids = {n.get("id") for n in bare.nodes}
    assert "pkg/math_utils.py::add" in expanded_ids
    assert "pkg/math_utils.py::add" not in bare_ids
