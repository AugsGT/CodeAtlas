"""End-to-end test of the Docker sandbox: automatic entry-point detection
-> isolated container execution -> telemetry decoded and ingested into
the graph, against a real, unmodified, non-sample repository. Skipped
when Docker itself isn't available (daemon not running) rather than
failing, since that's an environment precondition, not a code defect -
see sandbox.py's docker_availability().
"""

from pathlib import Path

import pytest

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.execution.pipeline import run_repository
from codeatlas.execution.sandbox import DockerExecutor, docker_availability
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.repository import GraphRepository

IDENTITY_TEST_REPO = Path(__file__).parent.parent / "codeatlas_identity_test_repo"

_available, _detail = docker_availability()
requires_docker = pytest.mark.skipif(not _available, reason=f"Docker not available: {_detail}")


def test_docker_availability_reports_a_reason_when_unavailable():
    available, detail = docker_availability()
    assert isinstance(available, bool)
    assert detail


@requires_docker
def test_runs_unmodified_repo_in_sandbox_and_ingests_telemetry(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        analysis = PythonAstAnalyzer().analyze(str(IDENTITY_TEST_REPO))
        write_analysis_result(repo, analysis)

        report = run_repository(repo, str(IDENTITY_TEST_REPO), executor=DockerExecutor())

        assert report.success, report.reason
        assert report.exit_code == 0
        assert not report.timed_out
        assert report.spans_captured >= 6
        assert report.spans_resolved >= 6
        assert report.crash is None
    finally:
        repo.close()


@requires_docker
def test_reports_failure_clearly_for_repo_with_no_entrypoint(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        no_entrypoint_repo = tmp_path / "no_entrypoint_repo"
        no_entrypoint_repo.mkdir()
        (no_entrypoint_repo / "lib.py").write_text("def helper():\n    return 1\n")

        report = run_repository(repo, str(no_entrypoint_repo), executor=DockerExecutor())

        assert not report.success
        assert "entry point" in report.reason
    finally:
        repo.close()


@requires_docker
def test_timeout_kills_container_and_preserves_partial_telemetry(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        slow_repo = tmp_path / "slow_repo"
        slow_repo.mkdir()
        (slow_repo / "main.py").write_text(
            "import time\n"
            "def quick():\n"
            "    return 1\n"
            "if __name__ == '__main__':\n"
            "    quick()\n"
            "    time.sleep(60)\n"
        )
        analysis = PythonAstAnalyzer().analyze(str(slow_repo))
        write_analysis_result(repo, analysis)

        report = run_repository(repo, str(slow_repo), executor=DockerExecutor(execute_timeout_s=3))

        assert report.success
        assert report.timed_out
        # `quick()` completed and was streamed to disk before the sleep was
        # killed - proving the timeout doesn't lose telemetry captured
        # before it fired.
        assert report.spans_captured >= 1
    finally:
        repo.close()
