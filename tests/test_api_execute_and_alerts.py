"""API-level tests for /api/execute and /api/alerts.

/api/execute's actual sandboxed execution is exercised end-to-end by
test_execution_sandbox.py (skipped when Docker isn't available). Here,
the pipeline call is monkeypatched so the API wiring itself - request
validation, response shape, error passthrough - is tested independently
of whether Docker is running in this environment.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codeatlas.api import app as app_module
from codeatlas.api.app import create_app
from codeatlas.execution.pipeline import ExecutionReport

SAMPLE_REPO = Path(__file__).parent.parent / "sample_repo"


def test_execute_rejects_missing_repo_root(tmp_path):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        response = client.post("/api/execute", json={"repo_root": str(tmp_path / "does_not_exist")})
        assert response.status_code == 400


def test_execute_reports_failure_clearly_when_no_entrypoint_found(tmp_path, monkeypatch):
    monkeypatch.setattr(
        app_module, "run_repository",
        lambda repo, repo_root: ExecutionReport(success=False, reason="no safe entry point"),
    )
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        response = client.post("/api/execute", json={"repo_root": str(SAMPLE_REPO)})
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is False
        assert body["reason"] == "no safe entry point"


def test_execute_returns_telemetry_summary_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        app_module, "run_repository",
        lambda repo, repo_root: ExecutionReport(
            success=True, entrypoint_source="main.py", exit_code=0,
            spans_captured=5, spans_resolved=4,
        ),
    )
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        response = client.post("/api/execute", json={"repo_root": str(SAMPLE_REPO)})
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["spans_captured"] == 5
        assert body["spans_resolved"] == 4
        assert body["exit_code"] == 0


def test_alerts_empty_on_fresh_graph(tmp_path):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        response = client.get("/api/alerts")
        assert response.status_code == 200
        assert response.json() == {"alerts": []}


def test_alerts_surfaces_runtime_errors(tmp_path):
    app = create_app(str(tmp_path / "graph.db"))
    with TestClient(app) as client:
        client.post("/api/ingest", json={"repo_root": str(SAMPLE_REPO)})

        import datetime
        from codeatlas.graph.models import RuntimeSpan

        span = RuntimeSpan(
            id="s1", trace_id="t1", name="add", start_time=datetime.datetime.now(datetime.timezone.utc),
            end_time=datetime.datetime.now(datetime.timezone.utc), duration_ms=1.0, status="ERROR",
            error_message="boom", code_filepath=str(SAMPLE_REPO / "pkg" / "math_utils.py"), code_function="add",
        )
        app.state.repo.upsert_runtime_span(span)
        app.state.repo.add_produces("pkg/math_utils.py::add", "s1")

        response = client.get("/api/alerts")
        assert response.status_code == 200
        alerts = response.json()["alerts"]
        assert any(a["evidence_id"] == "s1" and a["severity"] == "error" for a in alerts)
