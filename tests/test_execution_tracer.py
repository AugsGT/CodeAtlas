"""End-to-end test of the settrace-based universal tracer, run as a real
subprocess (exactly how the sandbox's bootstrap.py is actually invoked)
against codeatlas_identity_test_repo - a real, independently-authored
repository with no tracing decorators and no CodeAtlas awareness at all,
proving automatic instrumentation needs zero source modification.

Also proves the full loop end-to-end: static analysis -> streamed
OTLP-encoded spans -> existing (unmodified) identity resolution -> real
CodeEntity links, i.e. the new automatic-execution path produces evidence
the existing, already-tested reasoning/retrieval pipeline can consume
with no changes.
"""

import json
import subprocess
import sys
from pathlib import Path

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.execution.streaming import read_records
from codeatlas.graph.identity import resolve_and_link_span
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.repository import GraphRepository
from codeatlas.telemetry.otlp_receiver import parse_trace_request

IDENTITY_TEST_REPO = Path(__file__).parent.parent / "codeatlas_identity_test_repo"


def _run_bootstrap(repo_root, output_dir, extra_args=()):
    return subprocess.run(
        [sys.executable, "-m", "codeatlas.execution.bootstrap",
         "--repo-root", str(repo_root), "--output-dir", str(output_dir), *extra_args],
        capture_output=True, text=True, timeout=30,
    )


def _decode_spans(spans_path):
    spans = []
    with open(spans_path, "rb") as f:
        for record in read_records(f):
            spans.extend(parse_trace_request(record))
    return spans


def test_traces_unmodified_repo_with_no_decorators(tmp_path):
    result = _run_bootstrap(IDENTITY_TEST_REPO, tmp_path, extra_args=["--script", "run_workload.py"])
    assert result.returncode == 0, result.stderr

    spans_path = tmp_path / "spans.otlp"
    assert spans_path.is_file()
    spans = _decode_spans(spans_path)

    names = {(s.code_namespace, s.code_function) for s in spans}
    assert ("src.shop.services.order_service", "OrderService.calculate_total") in names
    assert ("src.shop.services.payment_service", "PaymentService.process_payment") in names
    assert ("src.shop.reports.sales_report", "generate_report") in names
    assert ("__main__", "main") in names

    # Module-level top-level frames are noise (never resolve to a
    # CodeEntity) and are filtered out.
    assert all(f != "<module>" for _, f in names)

    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["span_count"] == len(spans)
    assert summary["truncated"] is False


def test_captured_spans_resolve_to_real_code_entities(tmp_path):
    result = _run_bootstrap(IDENTITY_TEST_REPO, tmp_path, extra_args=["--script", "run_workload.py"])
    assert result.returncode == 0, result.stderr
    spans = _decode_spans(tmp_path / "spans.otlp")

    repo = GraphRepository(str(tmp_path / "graph.kuzu"))
    try:
        analysis = PythonAstAnalyzer().analyze(str(IDENTITY_TEST_REPO))
        write_analysis_result(repo, analysis)

        resolved = [s for s in spans if resolve_and_link_span(repo, s) is not None]
        # The 6 real function/method calls plus the OrderService/PaymentService
        # class-body-execution spans - every span with an actual CodeEntity
        # counterpart resolves; only <module>-level noise (already filtered
        # at capture time) would not.
        function_call_spans = [s for s in spans if s.code_function not in ("<module>",)]
        assert len(resolved) == len(function_call_spans)
        assert len(resolved) >= 6
    finally:
        repo.close()


def test_crash_is_captured_when_entrypoint_raises(tmp_path):
    repo_root = tmp_path / "crashing_repo"
    repo_root.mkdir()
    (repo_root / "main.py").write_text(
        "def boom():\n"
        "    raise ValueError('kaboom')\n"
        "if __name__ == '__main__':\n"
        "    boom()\n"
    )
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = _run_bootstrap(repo_root, output_dir, extra_args=["--script", "main.py"])
    assert result.returncode == 1

    crash = json.loads((output_dir / "crash.json").read_text())
    assert crash["type"] == "ValueError"
    assert "kaboom" in crash["message"]

    spans = _decode_spans(output_dir / "spans.otlp")
    boom_span = next(s for s in spans if s.code_function == "boom")
    assert boom_span.status == "ERROR"
    assert "kaboom" in boom_span.error_message
