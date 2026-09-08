"""End-to-end test of the subprocess execution fallback - the path used
automatically whenever Docker isn't available (see
pipeline.select_executor). Unlike the Docker sandbox tests, these need
no external dependency at all, so they're the primary proof that
automatic execution genuinely works end-to-end on a plain machine.
"""

from pathlib import Path

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.execution.pipeline import run_repository, select_executor
from codeatlas.execution.subprocess_sandbox import SubprocessExecutor
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.repository import GraphRepository

IDENTITY_TEST_REPO = Path(__file__).parent.parent / "codeatlas_identity_test_repo"


def test_runs_unmodified_repo_and_ingests_telemetry(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        analysis = PythonAstAnalyzer().analyze(str(IDENTITY_TEST_REPO))
        write_analysis_result(repo, analysis)

        report = run_repository(repo, str(IDENTITY_TEST_REPO), executor=SubprocessExecutor())

        assert report.success, report.reason
        assert report.isolation == "subprocess"
        assert report.isolation_warning  # weaker-isolation disclosure is always present
        assert report.exit_code == 0
        assert not report.timed_out
        assert report.spans_captured >= 6
        assert report.spans_resolved >= 6
        assert report.crash is None
    finally:
        repo.close()


def test_reports_failure_clearly_for_repo_with_no_entrypoint(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        no_entrypoint_repo = tmp_path / "no_entrypoint_repo"
        no_entrypoint_repo.mkdir()
        (no_entrypoint_repo / "lib.py").write_text("def helper():\n    return 1\n")

        report = run_repository(repo, str(no_entrypoint_repo), executor=SubprocessExecutor())

        assert not report.success
        assert "entry point" in report.reason
    finally:
        repo.close()


def test_crash_is_captured_and_ingested_as_error_log(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        crashing_repo = tmp_path / "crashing_repo"
        crashing_repo.mkdir()
        (crashing_repo / "main.py").write_text(
            "def boom():\n    raise ValueError('kaboom')\n"
            "if __name__ == '__main__':\n    boom()\n"
        )
        analysis = PythonAstAnalyzer().analyze(str(crashing_repo))
        write_analysis_result(repo, analysis)

        report = run_repository(repo, str(crashing_repo), executor=SubprocessExecutor())

        assert report.success  # execution ran; the target code crashing is not an executor failure
        assert report.exit_code == 1
        assert report.crash["type"] == "ValueError"

        rows = repo.query("MATCH (l:LogEntry) WHERE l.level = 'ERROR' RETURN l.message")
        assert any("kaboom" in row[0] for row in rows)
    finally:
        repo.close()


def test_timeout_kills_process_and_preserves_partial_telemetry(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        slow_repo = tmp_path / "slow_repo"
        slow_repo.mkdir()
        (slow_repo / "main.py").write_text(
            "import time\n"
            "def quick():\n    return 1\n"
            "if __name__ == '__main__':\n"
            "    quick()\n"
            "    time.sleep(30)\n"
        )
        analysis = PythonAstAnalyzer().analyze(str(slow_repo))
        write_analysis_result(repo, analysis)

        report = run_repository(repo, str(slow_repo), executor=SubprocessExecutor(timeout_s=2))

        assert report.success
        assert report.timed_out
        assert report.spans_captured >= 1  # quick() completed and was streamed before the kill
    finally:
        repo.close()


def test_select_executor_falls_back_to_subprocess_when_docker_unavailable(monkeypatch):
    import codeatlas.execution.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "docker_availability", lambda: (False, "not running"))
    assert isinstance(select_executor(), SubprocessExecutor)


def test_select_executor_prefers_docker_when_available(monkeypatch):
    import codeatlas.execution.pipeline as pipeline_module
    from codeatlas.execution.sandbox import DockerExecutor

    monkeypatch.setattr(pipeline_module, "docker_availability", lambda: (True, "ok"))
    assert isinstance(select_executor(), DockerExecutor)
