"""Backend API connecting the graph and reasoning pipeline to the
dashboard: an ingest endpoint to populate the graph from a repo, and an
ask endpoint that runs the full classify -> resolve -> retrieve ->
reason -> validate pipeline and returns everything the dashboard needs
to display (evidence, answer, and validation results).

`create_app(db_path)` is a factory rather than a bare module-level
singleton so tests can each get their own isolated Kuzu database;
`app` below is the default instance used when running the server.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from ..alerts.detector import detect_alerts
from ..analysis.python_ast import PythonAstAnalyzer
from ..execution.pipeline import run_repository
from ..graph.identity import resolve_and_link_log, resolve_and_link_metric, resolve_and_link_span
from ..graph.ingest import write_analysis_result
from ..graph.issues import sync_runtime_issues, write_static_issues
from ..graph.repository import GraphRepository
from ..reasoning.ollama_client import OllamaError
from ..reasoning.pipeline import ReasoningPipeline
from ..retrieval.graph_view import build_repo_graph
from ..telemetry import otlp_receiver
from ..validation.fix_cycle import apply_fix_to_repository, validate_fix
from .schemas import ApplyFixRequest, AskRequest, IngestRequest, ValidateFixRequest

_OTLP_CONTENT_TYPE = "application/x-protobuf"

NODE_LABELS = ["Module", "CodeEntity", "RuntimeSpan", "Metric", "LogEntry", "Issue"]
MAX_HISTORY_TURNS = 5  # bounds prompt size for long conversations


def create_app(db_path: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.repo = GraphRepository(db_path)
        app.state.pipeline = ReasoningPipeline(app.state.repo)
        yield
        app.state.repo.close()

    app = FastAPI(title="CodeAtlas API", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/stats")
    def stats():
        repo = app.state.repo
        counts = {}
        for label in NODE_LABELS:
            rows = repo.query(f"MATCH (n:{label}) RETURN count(n)")
            counts[label] = rows[0][0] if rows else 0
        return counts

    @app.post("/api/ingest")
    def ingest(request: IngestRequest):
        """Analyze repo_root and replace the graph's contents with it.

        Clears whatever was there before (both the static graph and any
        runtime telemetry) rather than merging on top of it: CodeAtlas has
        no per-repo scoping on its whole-graph queries (recent_errors,
        slow_calls, etc.), so leaving a previously-ingested repo's
        entities/spans around after ingesting a different one lets a
        question about the new repo silently get answered using stale
        evidence from the old one - a real, observed bug where a question
        about a never-ingested file returned an unrelated repo's error,
        narrated as if it belonged to the named file.
        """
        if not os.path.isdir(request.repo_root):
            raise HTTPException(status_code=400, detail=f"repo_root does not exist: {request.repo_root}")
        app.state.repo.clear_static_graph()
        app.state.repo.clear_runtime_telemetry()
        app.state.repo.clear_all_issues()
        result = PythonAstAnalyzer().analyze(request.repo_root)
        write_analysis_result(app.state.repo, result)
        write_static_issues(app.state.repo, result)
        return {
            "modules": len(result.modules),
            "entities": len(result.entities),
            "calls": len(result.calls),
            "depends_on": len(result.depends_on),
            "parse_errors": [
                {"path": m.path, "error": m.parse_error} for m in result.modules if m.parse_error
            ],
        }

    @app.post("/api/execute")
    def execute(request: IngestRequest):
        """Automatically execute an arbitrary (already-ingested) Python
        repository via the Docker sandbox, falling back to a restricted
        subprocess when Docker isn't available, and ingest whatever
        telemetry it produces. Reports clearly (200 with success=False,
        not a fake success) when no safe entry point could be found or no
        executor is usable, rather than pretending execution happened.
        See execution/pipeline.py.
        """
        if not os.path.isdir(request.repo_root):
            raise HTTPException(status_code=400, detail=f"repo_root does not exist: {request.repo_root}")
        report = run_repository(app.state.repo, request.repo_root)
        return {
            "success": report.success,
            "reason": report.reason,
            "isolation": report.isolation,
            "isolation_warning": report.isolation_warning,
            "entrypoint_source": report.entrypoint_source,
            "exit_code": report.exit_code,
            "timed_out": report.timed_out,
            "stdout": report.stdout,
            "stderr": report.stderr,
            "dependency_warning": report.dependency_warning,
            "spans_captured": report.spans_captured,
            "spans_resolved": report.spans_resolved,
            "truncated": report.truncated,
            "otel_instrumentors_enabled": report.otel_instrumentors_enabled,
            "crash": report.crash,
        }

    @app.get("/api/alerts")
    def alerts():
        """Proactive problem detection over whatever evidence is
        currently in the graph - static or runtime, no question needs to
        be asked first. See alerts/detector.py: this resyncs Issue nodes
        from current RuntimeSpan/LogEntry state, then reads every Issue
        (static parse errors included)."""
        found = detect_alerts(app.state.repo)
        return {
            "alerts": [
                {
                    "severity": a.severity,
                    "message": a.message,
                    "entity_id": a.entity_id,
                    "evidence_id": a.evidence_id,
                    "issue_id": a.issue_id,
                    "type": a.type,
                    "detection_method": a.detection_method,
                    "file": a.file,
                    "line": a.line,
                }
                for a in found
            ]
        }

    @app.get("/api/issues")
    def issues(severity: str | None = None, file: str | None = None,
               type: str | None = None, detection_method: str | None = None):
        """Every Issue currently in the graph (static and runtime alike),
        optionally filtered - the structured counterpart to /api/alerts'
        flat proactive list, for a dashboard that wants to browse/filter
        rather than just see the latest summary."""
        sync_runtime_issues(app.state.repo)
        found = app.state.repo.list_issues()
        if severity:
            found = [i for i in found if i["severity"] == severity]
        if file:
            found = [i for i in found if i["file"] == file or i["module_path"] == file]
        if type:
            found = [i for i in found if i["type"] == type]
        if detection_method:
            found = [i for i in found if i["detection_method"] == detection_method]
        return {"issues": found}

    @app.get("/api/graph")
    def graph():
        """The whole current structural graph (every Module/CodeEntity
        and the CONTAINS/CALLS/DEPENDS_ON edges between them, with each
        node annotated by the most severe Issue found on it) for the
        dashboard's graph visualization panel - see retrieval/graph_view.py.
        Unlike every other retrieval method, this isn't scoped to one
        question's evidence; it's the full picture for whatever repo is
        currently ingested."""
        return build_repo_graph(app.state.repo)

    @app.get("/api/modules")
    def modules():
        """Every ingested Module - path/name/language/parse_error - for
        the dashboard's file navigator. Deliberately does not include
        CodeEntity/issue counts per module (keep this cheap and generic);
        a client wanting per-file detail uses /api/issues?file=... or
        /api/file for source."""
        return {
            "modules": [
                {"path": m.path, "name": m.name, "language": m.language, "parse_error": m.parse_error}
                for m in app.state.repo.list_modules()
            ]
        }

    @app.get("/api/file")
    def file(path: str):
        """Raw source text of an ingested module, for the dashboard's
        code viewer - resolved via the Module's own recorded abs_path
        (populated at ingest time), so the caller never needs to resupply
        whatever repo_root was used to ingest it. 404 if the module was
        never ingested; 410 if it was but the file is no longer readable
        (moved/deleted since ingest)."""
        module = app.state.repo.get_module(path)
        if module is None:
            raise HTTPException(status_code=404, detail=f"module not ingested: {path}")
        if not module.abs_path:
            raise HTTPException(status_code=410, detail=f"no source location recorded for: {path}")
        try:
            with open(module.abs_path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as exc:
            raise HTTPException(status_code=410, detail=f"could not read {path}: {exc}") from exc
        return {"path": module.path, "language": module.language, "content": content}

    # --- OTLP receiver: how telemetry from an ARBITRARY, separately-run
    # repository reaches CodeAtlas safely. The traced application runs
    # entirely in its own process (the user's own machine/container/
    # sandbox of choice) and exports over the network in the standard
    # OTLP wire format - CodeAtlas never executes that code itself, it
    # only decodes what arrives here. See telemetry/otlp_receiver.py and
    # telemetry/tracing.py's make_otlp_tracer() for the client side.
    # No authentication: run this on localhost / a trusted network only.

    @app.post("/v1/traces")
    async def receive_traces(request: Request):
        body = await request.body()
        try:
            spans = otlp_receiver.parse_trace_request(body)
        except Exception as exc:  # noqa: BLE001 - malformed input, not a server bug
            raise HTTPException(status_code=400, detail=f"invalid OTLP trace payload: {exc}") from exc
        for span in spans:
            resolve_and_link_span(app.state.repo, span)
        return Response(content=b"", media_type=_OTLP_CONTENT_TYPE)

    @app.post("/v1/metrics")
    async def receive_metrics(request: Request):
        body = await request.body()
        try:
            metrics = otlp_receiver.parse_metrics_request(body)
        except Exception as exc:  # noqa: BLE001 - malformed input, not a server bug
            raise HTTPException(status_code=400, detail=f"invalid OTLP metrics payload: {exc}") from exc
        for metric in metrics:
            resolve_and_link_metric(app.state.repo, metric)
        return Response(content=b"", media_type=_OTLP_CONTENT_TYPE)

    @app.post("/v1/logs")
    async def receive_logs(request: Request):
        body = await request.body()
        try:
            logs = otlp_receiver.parse_logs_request(body)
        except Exception as exc:  # noqa: BLE001 - malformed input, not a server bug
            raise HTTPException(status_code=400, detail=f"invalid OTLP logs payload: {exc}") from exc
        for log in logs:
            resolve_and_link_log(app.state.repo, log)
        return Response(content=b"", media_type=_OTLP_CONTENT_TYPE)

    @app.post("/api/ask")
    def ask(request: AskRequest):
        history = [
            {
                "question": turn.question,
                "answer": turn.answer,
                "resolved_target": turn.resolved_target.model_dump() if turn.resolved_target else None,
            }
            for turn in request.history[-MAX_HISTORY_TURNS:]
        ]
        try:
            result = app.state.pipeline.ask(request.question, history)
        except OllamaError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _serialize_answer(result)

    @app.post("/api/validate-fix")
    def validate_fix_route(request: ValidateFixRequest):
        """Apply a proposed improved_code snippet (from a diagnosis
        answer's diagnosis_detail) to a disposable COPY of repo_root,
        re-run the repository, and report whether the target entity's
        previously-observed failure is actually gone - see
        validation/fix_cycle.py. Never modifies repo_root itself.
        """
        if not os.path.isdir(request.repo_root):
            raise HTTPException(status_code=400, detail=f"repo_root does not exist: {request.repo_root}")
        result = validate_fix(app.state.repo, request.repo_root, request.entity_id, request.improved_code)
        return {
            "attempted": result.attempted,
            "resolved": result.resolved,
            "before_status": result.before_status,
            "before_error": result.before_error,
            "after_status": result.after_status,
            "after_error": result.after_error,
            "exit_code": result.exit_code,
            "isolation": result.isolation,
            "isolation_warning": result.isolation_warning,
            "explanation": result.explanation,
            "reason": result.reason,
        }

    @app.post("/api/apply-fix")
    def apply_fix_route(request: ApplyFixRequest):
        """Write a proposed improved_code snippet directly into the
        REAL repository, in place of the target entity's current source
        - unlike /api/validate-fix, this modifies the user's actual
        files. Always writes a timestamped backup of the original file
        first (see validation/fix_cycle.py::apply_fix_to_repository).
        A deliberate, explicit action - the dashboard gates this behind
        its own separate confirmation, never triggered automatically by
        diagnosis or validation.

        Re-runs static analysis over repo_root after a successful apply
        (same as /api/ingest's static half, but leaving runtime telemetry
        alone - the just-applied fix doesn't invalidate past execution
        evidence, it just means it's due for a re-run). Without this, two
        real problems followed from the same stale state: the issue just
        fixed kept showing as open until a manual re-ingest, and every
        OTHER entity in the same file kept the line numbers recorded at
        the PREVIOUS ingest - if the fix changed the file's line count, a
        second apply-fix to a different entity in that file would patch
        the wrong lines using those stale numbers, silently corrupting
        the file. Both are closed by simply keeping the graph in sync
        with what was just written to disk.
        """
        if not os.path.isdir(request.repo_root):
            raise HTTPException(status_code=400, detail=f"repo_root does not exist: {request.repo_root}")
        result = apply_fix_to_repository(app.state.repo, request.repo_root, request.entity_id, request.improved_code)
        if result.applied:
            analysis = PythonAstAnalyzer().analyze(request.repo_root)
            app.state.repo.clear_static_graph()
            app.state.repo.clear_all_issues()
            write_analysis_result(app.state.repo, analysis)
            write_static_issues(app.state.repo, analysis)
        return {
            "applied": result.applied,
            "file": result.file,
            "backup_path": result.backup_path,
            "reason": result.reason,
        }

    return app


def _serialize_answer(result):
    payload = {
        "question": result.question,
        # The intent actually used to retrieve evidence — NOT necessarily
        # what the classifier guessed: the pipeline falls back to a
        # no-target intent when the classifier's target doesn't resolve,
        # and showing the raw guess here would misrepresent what the
        # answer is actually grounded in.
        "intent": result.evidence.intent,
        "classified_intent": result.classification.intent,
        "target": result.classification.target,
        "resolved_target": {"kind": result.resolved_target[0], "id": result.resolved_target[1]},
        "evidence": {"nodes": result.evidence.nodes, "edges": result.evidence.edges},
        "answer": result.answer,
        "validation": {
            "citations": result.validation.citations,
            "unsupported_citations": result.validation.unsupported_citations,
            "warnings": result.validation.warnings,
            "is_grounded": result.validation.is_grounded,
        },
    }
    # Only present for the "diagnosis" intent (see reasoning/pipeline.py)
    # - the full root_cause/proposed_fix/improved_code/confidence_basis
    # breakdown, beyond what the flattened "answer" prose already shows.
    if result.structured_diagnosis is not None:
        d = result.structured_diagnosis
        payload["diagnosis_detail"] = {
            "root_cause": d.root_cause,
            "affected_code": d.affected_code,
            "proposed_fix": d.proposed_fix,
            "improved_code": d.improved_code,
            "confidence_basis": d.confidence_basis,
            "limitations": d.limitations,
        }
    return payload


app = create_app(os.environ.get("CODEATLAS_DB_PATH", "data/codeatlas.db"))
