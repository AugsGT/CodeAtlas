import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codeatlas.api.app import create_app
from tests.conftest import KNOWN_BUGGY_SERVICE_PY, KNOWN_WORKING_SERVICE_PY

SAMPLE_REPO = Path(__file__).parent.parent / "sample_repo"


def test_health(tmp_path):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_stats_starts_at_zero(tmp_path):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        response = client.get("/api/stats")
        assert response.status_code == 200
        assert response.json() == {
            "Module": 0, "CodeEntity": 0, "RuntimeSpan": 0, "Metric": 0, "LogEntry": 0, "Issue": 0,
        }


@pytest.mark.parametrize("pinned_service_py_content", [KNOWN_WORKING_SERVICE_PY], indirect=True)
def test_ingest_populates_graph_and_stats(tmp_path, pinned_service_py_content):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        response = client.post("/api/ingest", json={"repo_root": str(SAMPLE_REPO)})
        assert response.status_code == 200
        body = response.json()
        assert body["modules"] == 5  # pkg/__init__, math_utils, service, tracing_setup, run_workload
        assert body["entities"] == 6
        assert body["calls"] == 5

        stats = client.get("/api/stats").json()
        assert stats["Module"] == 5
        assert stats["CodeEntity"] == 6


def test_ingest_a_second_repo_clears_the_first(tmp_path):
    """Real observed bug: ingesting repo B after repo A left A's entities
    and telemetry in the graph forever (nothing ever cleared them), so a
    whole-graph survey question (recent_errors, recent_activity - no
    per-repo filter exists) about B could silently be answered using A's
    leftover evidence instead. /api/ingest must reset the graph to just
    the newly-ingested repo, not merge on top of whatever was there."""
    identity_test_repo = Path(__file__).parent.parent / "codeatlas_identity_test_repo"
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        first = client.post("/api/ingest", json={"repo_root": str(identity_test_repo)}).json()
        assert first["entities"] > 0
        assert client.get("/api/stats").json()["Module"] == first["modules"]

        second = client.post("/api/ingest", json={"repo_root": str(SAMPLE_REPO)}).json()

        stats = client.get("/api/stats").json()
        assert stats["Module"] == second["modules"]
        assert stats["CodeEntity"] == second["entities"]
        # The first repo's entities must be genuinely gone, not just
        # outnumbered - order_service.py belongs only to
        # codeatlas_identity_test_repo.
        assert app.state.repo.get_module("src/shop/services/order_service.py") is None


def test_ingest_records_a_static_issue_for_a_parse_error_and_exposes_it_via_api(tmp_path):
    broken_repo = tmp_path / "broken_repo"
    broken_repo.mkdir()
    (broken_repo / "tt.py").write_text("def broken(:\n    pass\n")

    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        ingest_body = client.post("/api/ingest", json={"repo_root": str(broken_repo)}).json()
        assert ingest_body["parse_errors"] == [{"path": "tt.py", "error": ingest_body["parse_errors"][0]["error"]}]

        stats = client.get("/api/stats").json()
        assert stats["Issue"] == 1

        issues = client.get("/api/issues").json()["issues"]
        assert len(issues) == 1
        assert issues[0]["type"] == "SyntaxError"
        assert issues[0]["detection_method"] == "static"
        assert issues[0]["severity"] == "critical"

        alerts = client.get("/api/alerts").json()["alerts"]
        assert len(alerts) == 1
        assert alerts[0]["type"] == "SyntaxError"

        # A second ingest of a clean repo must clear the stale issue, same
        # as it already clears stale modules/entities/telemetry.
        client.post("/api/ingest", json={"repo_root": str(SAMPLE_REPO)})
        assert client.get("/api/stats").json()["Issue"] == 0


def test_issues_endpoint_filters_by_severity(tmp_path):
    broken_repo = tmp_path / "broken_repo"
    broken_repo.mkdir()
    (broken_repo / "tt.py").write_text("def broken(:\n    pass\n")

    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(broken_repo)})

        matching = client.get("/api/issues", params={"severity": "critical"}).json()["issues"]
        assert len(matching) == 1

        no_match = client.get("/api/issues", params={"severity": "medium"}).json()["issues"]
        assert no_match == []


def test_graph_endpoint_serves_structural_nodes_and_edges(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "main.py").write_text("def add(a, b):\n    return a + b\n\n\ndef run():\n    return add(1, 2)\n")

    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(repo_dir)})

        body = client.get("/api/graph").json()
        node_ids = {n["id"] for n in body["nodes"]}
        assert "module:main.py" in node_ids
        assert "entity:main.py::add" in node_ids
        assert "entity:main.py::run" in node_ids

        edge_types = {(e["source"], e["target"], e["type"]) for e in body["edges"]}
        assert ("entity:main.py::run", "entity:main.py::add", "CALLS") in edge_types


def test_modules_and_file_endpoints_serve_ingested_source(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "good.py").write_text("def helper():\n    return 1\n")
    (repo_dir / "broken.py").write_text("def broken(:\n    pass\n")

    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(repo_dir)})

        modules_body = client.get("/api/modules").json()
        by_path = {m["path"]: m for m in modules_body["modules"]}
        assert set(by_path) == {"good.py", "broken.py"}
        assert by_path["broken.py"]["parse_error"] != ""
        assert by_path["good.py"]["parse_error"] == ""

        good_file = client.get("/api/file", params={"path": "good.py"})
        assert good_file.status_code == 200
        assert "def helper" in good_file.json()["content"]

        # A file with a syntax error is still readable as raw source -
        # only the AST-derived facts (entities/calls) are unavailable for it.
        broken_file = client.get("/api/file", params={"path": "broken.py"})
        assert broken_file.status_code == 200
        assert "def broken(:" in broken_file.json()["content"]


def test_file_endpoint_404s_for_a_never_ingested_module(tmp_path):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        response = client.get("/api/file", params={"path": "nope.py"})
        assert response.status_code == 404


def test_file_endpoint_410s_when_the_file_no_longer_exists_on_disk(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "gone.py").write_text("def helper():\n    return 1\n")

    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(repo_dir)})
        (repo_dir / "gone.py").unlink()

        response = client.get("/api/file", params={"path": "gone.py"})
        assert response.status_code == 410


def test_validate_fix_end_to_end_via_api(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "main.py").write_text(
        "def divide(a, b):\n    return a / b\n\n\nif __name__ == '__main__':\n    divide(10, 0)\n"
    )
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(repo_dir)})
        client.post("/api/execute", json={"repo_root": str(repo_dir)})

        response = client.post("/api/validate-fix", json={
            "repo_root": str(repo_dir),
            "entity_id": "main.py::divide",
            "improved_code": "def divide(a, b):\n    if b == 0:\n        return 0\n    return a / b\n",
        })

        assert response.status_code == 200
        body = response.json()
        assert body["attempted"] is True
        assert body["before_status"] == "ERROR"
        assert body["after_status"] == "OK"
        assert body["resolved"] is True

        # The original repository must be untouched.
        assert "return a / b\n\n\nif __name__" in (repo_dir / "main.py").read_text()


def test_validate_fix_rejects_missing_repo_root(tmp_path):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        response = client.post("/api/validate-fix", json={
            "repo_root": str(tmp_path / "does_not_exist"),
            "entity_id": "main.py::divide",
            "improved_code": "def divide(a, b):\n    return 0\n",
        })
        assert response.status_code == 400


def test_apply_fix_end_to_end_via_api(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    original_content = "def divide(a, b):\n    return a / b\n\n\nif __name__ == '__main__':\n    divide(10, 0)\n"
    (repo_dir / "main.py").write_text(original_content)
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(repo_dir)})

        response = client.post("/api/apply-fix", json={
            "repo_root": str(repo_dir),
            "entity_id": "main.py::divide",
            "improved_code": "def divide(a, b):\n    if b == 0:\n        return 0\n    return a / b\n",
        })

        assert response.status_code == 200
        body = response.json()
        assert body["applied"] is True
        assert body["file"] == "main.py"
        assert os.path.isfile(body["backup_path"])

        new_content = (repo_dir / "main.py").read_text()
        assert "if b == 0" in new_content
        assert Path(body["backup_path"]).read_text() == original_content


def test_apply_fix_refreshes_the_graph_so_a_second_fix_in_the_same_file_lands_correctly(tmp_path):
    """Real bug: applying a fix used to leave the graph's CodeEntity line
    ranges stale. If the fix changed the file's line count, a SECOND
    apply-fix to a different entity in the SAME file - without an
    explicit re-ingest in between - would patch using the old, now-wrong
    line numbers and corrupt the file. /api/apply-fix now re-runs static
    analysis after a successful apply so the next apply always sees
    correct line ranges; this reproduces the exact two-functions-one-file
    scenario end-to-end and asserts the final file content is exactly
    what both fixes, applied correctly in sequence, should produce."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    original_content = (
        "def first_broken(x):\n"
        "    return x * 2\n"
        "\n"
        "\n"
        "def second_broken(a, b):\n"
        "    return a - b\n"
    )
    (repo_dir / "main.py").write_text(original_content)
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(repo_dir)})

        # Fixing first_broken grows it from 2 lines to 4, shifting
        # second_broken's real position two lines further down than
        # whatever was recorded at ingest time.
        first_response = client.post("/api/apply-fix", json={
            "repo_root": str(repo_dir),
            "entity_id": "main.py::first_broken",
            "improved_code": "def first_broken(x):\n    if x < 0:\n        return 0\n    return x * 2\n",
        })
        assert first_response.json()["applied"] is True

        second_response = client.post("/api/apply-fix", json={
            "repo_root": str(repo_dir),
            "entity_id": "main.py::second_broken",
            "improved_code": "def second_broken(a, b):\n    if b == 0:\n        return 0\n    return a - b\n",
        })
        assert second_response.json()["applied"] is True

        final_content = (repo_dir / "main.py").read_text()
        assert final_content == (
            "def first_broken(x):\n"
            "    if x < 0:\n"
            "        return 0\n"
            "    return x * 2\n"
            "\n"
            "\n"
            "def second_broken(a, b):\n"
            "    if b == 0:\n"
            "        return 0\n"
            "    return a - b\n"
        )


def test_apply_fix_rejects_missing_repo_root(tmp_path):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        response = client.post("/api/apply-fix", json={
            "repo_root": str(tmp_path / "does_not_exist"),
            "entity_id": "main.py::divide",
            "improved_code": "def divide(a, b):\n    return 0\n",
        })
        assert response.status_code == 400


def test_ingest_rejects_missing_repo_root(tmp_path):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        response = client.post("/api/ingest", json={"repo_root": str(tmp_path / "does_not_exist")})
        assert response.status_code == 400


@pytest.mark.parametrize("pinned_service_py_content", [KNOWN_BUGGY_SERVICE_PY], indirect=True)
def test_execute_sample_repo_captures_crash(tmp_path, pinned_service_py_content):
    """pkg/service.py::Calculator.compute is pinned to a known NameError
    for this test — sample_repo is the user's own live sandbox and its
    real content changes independently of this suite (see conftest.py's
    sample_repo_service/pinned_service_py_content docstrings). Runs
    through the same generic /api/execute path any other repo uses (via
    sample_repo/run_workload.py), rather than a sample-repo-specific
    endpoint."""
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(SAMPLE_REPO)})

        response = client.post("/api/execute", json={"repo_root": str(SAMPLE_REPO)})

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["exit_code"] == 1
        assert body["crash"]["type"] == "NameError"
        assert body["spans_captured"] == 3  # Calculator, run, compute — add/square never reached

        stats = client.get("/api/stats").json()
        assert stats["RuntimeSpan"] == 3


@pytest.mark.parametrize("pinned_service_py_content", [KNOWN_BUGGY_SERVICE_PY], indirect=True)
def test_execute_does_not_accumulate_stale_spans_across_runs(tmp_path, pinned_service_py_content):
    """A RuntimeSpan's id is a fresh random id every execution, so without
    clearing prior telemetry first, running the workload twice would leave
    6 spans instead of 3 - and any error from an earlier, since-fixed run
    would linger in evidence forever. This is the regression guard for a
    real reported bug: after removing sample_repo's NameError, the
    dashboard kept citing the old error because nothing ever cleared it."""
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(SAMPLE_REPO)})

        first = client.post("/api/execute", json={"repo_root": str(SAMPLE_REPO)}).json()
        assert first["crash"] is not None

        second = client.post("/api/execute", json={"repo_root": str(SAMPLE_REPO)}).json()
        assert second["crash"] is not None

        stats = client.get("/api/stats").json()
        assert stats["RuntimeSpan"] == 3  # not 6 - the first run's spans were cleared


@pytest.mark.parametrize("pinned_service_py_content", [KNOWN_WORKING_SERVICE_PY], indirect=True)
def test_ask_end_to_end(tmp_path, pinned_service_py_content, ollama_client):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(SAMPLE_REPO)})

        response = client.post("/api/ask", json={"question": "Who calls the add function?"})

        assert response.status_code == 200
        body = response.json()
        assert body["intent"] == "callers"
        assert body["resolved_target"]["id"] == "pkg/math_utils.py::add"
        node_ids = {n.get("id") for n in body["evidence"]["nodes"]}
        assert "pkg/service.py::Calculator.compute" in node_ids
        assert isinstance(body["answer"], str) and len(body["answer"]) > 0
        assert "warnings" in body["validation"]


def test_ask_diagnosis_question_includes_structured_diagnosis_detail(tmp_path, ollama_client):
    """A diagnosis question against a real, named target must come back
    with the structured diagnosis_detail breakdown (root_cause/
    proposed_fix/improved_code/confidence_basis/limitations), not just
    the flattened prose "answer" - see reasoning/pipeline.py."""
    broken_repo = tmp_path / "broken_repo"
    broken_repo.mkdir()
    (broken_repo / "tt.py").write_text("def broken(:\n    pass\n")

    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(broken_repo)})

        response = client.post("/api/ask", json={"question": "What is wrong in tt.py?"})

        assert response.status_code == 200
        body = response.json()
        assert body["intent"] == "diagnosis"
        assert "diagnosis_detail" in body
        detail = body["diagnosis_detail"]
        assert set(detail.keys()) == {
            "root_cause", "affected_code", "proposed_fix", "improved_code",
            "confidence_basis", "limitations",
        }
        assert isinstance(detail["affected_code"], list)


def test_ask_with_no_evidence_is_grounded_refusal(tmp_path, ollama_client):
    """No ingest happened, so nothing resolves and no evidence is found.
    Intent classification still calls the LLM, but the reasoning step
    must refuse deterministically instead of inventing an answer."""
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        response = client.post("/api/ask", json={"question": "What does main() do?"})

        assert response.status_code == 200
        body = response.json()
        assert body["evidence"]["nodes"] == []
        assert "don't have enough evidence" in body["answer"]
        assert body["validation"]["is_grounded"] is True


@pytest.mark.parametrize("pinned_service_py_content", [KNOWN_WORKING_SERVICE_PY], indirect=True)
def test_ask_follow_up_carries_history(tmp_path, pinned_service_py_content, ollama_client):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(SAMPLE_REPO)})

        first = client.post("/api/ask", json={"question": "Tell me about the compute method"}).json()
        history = [{
            "question": first["question"],
            "answer": first["answer"],
            "resolved_target": first["resolved_target"],
        }]

        second = client.post("/api/ask", json={"question": "What does it call?", "history": history})

        assert second.status_code == 200
        body = second.json()
        callee_ids = {n.get("id") for n in body["evidence"]["nodes"]}
        assert "pkg/math_utils.py::add" in callee_ids or "pkg/math_utils.py::square" in callee_ids
