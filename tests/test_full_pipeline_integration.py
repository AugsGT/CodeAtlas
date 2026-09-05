"""Phase 9: the complete pipeline, run against the real sample_repo.

source code -> static analysis -> graph -> runtime telemetry ->
identity resolution -> retrieval -> reasoning -> validation -> API

Each stage was already tested in isolation in earlier phases; this
test exercises them wired together exactly as a real user session
would, using the actually-instrumented sample_repo end to end rather
than hand-seeded fixture data.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.api.app import create_app
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.repository import GraphRepository
from codeatlas.reasoning.pipeline import ReasoningPipeline
from codeatlas.retrieval.subgraph import SubgraphRetriever
from codeatlas.telemetry.tracing import GraphSpanExporter
from tests.conftest import KNOWN_BUGGY_SERVICE_PY, KNOWN_WORKING_SERVICE_PY

SAMPLE_REPO = Path(__file__).parent.parent / "sample_repo"


@pytest.mark.parametrize("sample_repo_service", [KNOWN_BUGGY_SERVICE_PY], indirect=True)
def test_full_pipeline_source_to_validated_answer(tmp_path, sample_repo_service, ollama_client):
    """pkg/service.py::Calculator.compute is pinned to a known NameError
    for this test (see conftest.py's sample_repo_service docstring), so
    run() raises before add()/square() ever execute. This exercises the
    full chain including the error-surfacing path: compute()'s span ends
    up ERROR-status, and a "what's failing" question should retrieve it
    as grounded evidence."""
    service, tracing_setup = sample_repo_service
    repo = GraphRepository(tmp_path / "graph.db")

    # 1. Static analysis -> graph.
    analysis_result = PythonAstAnalyzer().analyze(str(SAMPLE_REPO))
    write_analysis_result(repo, analysis_result)
    assert repo.get_code_entity("pkg/service.py::Calculator.compute") is not None

    # 2. Runtime telemetry: run real instrumented code, export real spans.
    with pytest.raises(NameError):
        service.run()
    finished_spans = tracing_setup.exporter.get_finished_spans()
    assert len(finished_spans) == 2  # run, compute — add/square never reached

    # 3. Identity resolution: spans -> CodeEntity via the graph exporter.
    GraphSpanExporter(repo).export(finished_spans)
    produced_spans = repo.spans_for_entity("pkg/service.py::Calculator.compute")
    assert len(produced_spans) == 1

    # 4. Retrieval: bounded evidence for compute() includes both its
    # static caller and its failed runtime span.
    retriever = SubgraphRetriever(repo)
    evidence = retriever.retrieve("entity_overview", entity_id="pkg/service.py::Calculator.compute")
    node_ids = {n.get("id") for n in evidence.nodes}
    assert "pkg/service.py::run" in node_ids
    error_spans = [n for n in evidence.nodes if n["label"] == "RuntimeSpan"]
    assert error_spans and error_spans[0]["status"] == "ERROR"

    # 5. Reasoning + 6. Validation: a "what's failing" question surfaces
    # the error and is grounded in real evidence.
    pipeline = ReasoningPipeline(repo, ollama_client)
    result = pipeline.ask("What functions are failing right now?")
    assert result.evidence.intent in ("recent_errors", "entity_overview", "runtime_behavior", "recent_activity")
    node_ids = {n.get("id") for n in result.evidence.nodes}
    assert "pkg/service.py::Calculator.compute" in node_ids or "pkg/service.py::run" in node_ids
    assert result.validation.is_grounded
    assert isinstance(result.answer, str) and len(result.answer) > 0

    repo.close()


@pytest.mark.parametrize("pinned_service_py_content", [KNOWN_WORKING_SERVICE_PY], indirect=True)
def test_full_pipeline_via_api(tmp_path, pinned_service_py_content, ollama_client):
    """Same flow, but through the HTTP API a dashboard would actually call."""
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        ingest_response = client.post("/api/ingest", json={"repo_root": str(SAMPLE_REPO)})
        assert ingest_response.status_code == 200
        assert ingest_response.json()["entities"] == 6

        ask_response = client.post("/api/ask", json={"question": "What does the run function call?"})
        assert ask_response.status_code == 200
        body = ask_response.json()
        assert body["intent"] == "callees"
        assert body["validation"]["is_grounded"] is True
