"""End-to-end regression for the real "why is there no output" report:
a missing return statement raises no exception, so status/error_message
alone give CodeAtlas nothing to work with. return_value capture (added
to RuntimeSpan) is what actually lets a question like this be answered
from real evidence instead of getting a deterministic "no evidence"
refusal forever.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codeatlas.api.app import create_app
from tests.conftest import KNOWN_SILENT_BUG_SERVICE_PY

SAMPLE_REPO = Path(__file__).parent.parent / "sample_repo"


@pytest.mark.parametrize("pinned_service_py_content", [KNOWN_SILENT_BUG_SERVICE_PY], indirect=True)
def test_workload_captures_none_return_value_with_no_error(tmp_path, pinned_service_py_content):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(SAMPLE_REPO)})

        response = client.post("/api/execute", json={"repo_root": str(SAMPLE_REPO)}).json()

        assert response["crash"] is None  # no exception - this is the whole point
        assert response["spans_captured"] == 4  # Calculator, run, compute, add — square is never called

        stats = client.get("/api/stats").json()
        assert stats["RuntimeSpan"] == 4


@pytest.mark.parametrize("pinned_service_py_content", [KNOWN_SILENT_BUG_SERVICE_PY], indirect=True)
def test_ask_why_no_output_is_grounded_in_return_value_evidence(tmp_path, pinned_service_py_content, ollama_client):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(SAMPLE_REPO)})
        client.post("/api/execute", json={"repo_root": str(SAMPLE_REPO)})

        response = client.post("/api/ask", json={"question": "What does the compute method return?"})

        assert response.status_code == 200
        body = response.json()
        return_values = {n.get("return_value") for n in body["evidence"]["nodes"] if n.get("label") == "RuntimeSpan"}
        assert "None" in return_values
        assert body["validation"]["is_grounded"] is True


@pytest.mark.parametrize("pinned_service_py_content", [KNOWN_SILENT_BUG_SERVICE_PY], indirect=True)
def test_ask_vague_why_no_output_question_gets_real_evidence(tmp_path, pinned_service_py_content, ollama_client):
    """The literal question a real user asked, naming no specific
    function. Before recent_activity existed, the classifier's guessed
    intent (e.g. runtime_behavior) had no target to resolve, fell back to
    recent_errors, and came back with zero evidence — there IS no error
    in this scenario. recent_activity is what actually gives this vague
    question something real to answer from."""
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(SAMPLE_REPO)})
        client.post("/api/execute", json={"repo_root": str(SAMPLE_REPO)})

        response = client.post("/api/ask", json={"question": "why is there no output"})

        assert response.status_code == 200
        body = response.json()
        assert len(body["evidence"]["nodes"]) > 0
        return_values = {n.get("return_value") for n in body["evidence"]["nodes"] if n.get("label") == "RuntimeSpan"}
        assert "None" in return_values
