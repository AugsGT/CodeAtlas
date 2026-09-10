""""What are the issues in this repo" against a repo that ran cleanly
must say so plainly ("no errors found") rather than the generic "I don't
have enough evidence... try ingesting more data" refusal, which reads as
if data is missing when it isn't - the dashboard's Alerts panel already
gets this right (same underlying query), the Q&A path needs to match it.

Empty evidence for a target-seeking intent (or when no execution has
ever happened at all) is a genuinely different situation and must still
get the original refusal - only recent_errors/slow_calls with *some*
runtime data already in the graph get the "clean" answer.
"""

import datetime
from pathlib import Path

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.models import RuntimeSpan
from codeatlas.graph.repository import GraphRepository
from codeatlas.reasoning.pipeline import ReasoningPipeline
from codeatlas.reasoning.reasoner import _NO_EVIDENCE_ANSWER

SAMPLE_REPO = Path(__file__).parent.parent / "sample_repo"


class _FakeClient:
    """Deterministic stand-in for the LLM so these tests don't need
    Ollama and don't depend on classifier non-determinism - only the
    pipeline's own empty-evidence handling is under test here."""

    def __init__(self, intent):
        self._intent = intent

    def generate(self, prompt, format=None, temperature=0.0):
        if format == "json":
            return f'{{"intent": "{self._intent}", "target": ""}}'
        return "the reasoner should never be called in the clean-result case"


def _repo_with_one_successful_span(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    result = PythonAstAnalyzer().analyze(str(SAMPLE_REPO))
    write_analysis_result(repo, result)

    now = datetime.datetime.now(datetime.timezone.utc)
    repo.upsert_runtime_span(RuntimeSpan(
        id="span-ok", trace_id="t1", name="add",
        start_time=now, end_time=now, duration_ms=1.0, status="OK",
        code_filepath=str(SAMPLE_REPO / "pkg" / "math_utils.py"), code_function="add",
    ))
    repo.add_produces("pkg/math_utils.py::add", "span-ok")
    return repo


def test_recent_errors_with_clean_run_says_no_errors_found(tmp_path):
    repo = _repo_with_one_successful_span(tmp_path)
    try:
        pipeline = ReasoningPipeline(repo, _FakeClient("recent_errors"))
        result = pipeline.ask("what are the issues in this repo")

        assert result.evidence.nodes == []
        assert "no errors" in result.answer.lower()
        assert result.answer != _NO_EVIDENCE_ANSWER
        assert result.validation.is_grounded
    finally:
        repo.close()


def test_slow_calls_with_clean_run_says_nothing_slow(tmp_path):
    repo = _repo_with_one_successful_span(tmp_path)
    try:
        pipeline = ReasoningPipeline(repo, _FakeClient("slow_calls"))
        result = pipeline.ask("is anything slow")

        assert result.evidence.nodes == []
        assert "slow" in result.answer.lower()
        assert result.answer != _NO_EVIDENCE_ANSWER
    finally:
        repo.close()


def test_recent_errors_with_no_execution_at_all_still_refuses(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(SAMPLE_REPO))
        write_analysis_result(repo, result)
        # No RuntimeSpan ever recorded - genuinely missing data, not a clean run.

        pipeline = ReasoningPipeline(repo, _FakeClient("recent_errors"))
        answer_result = pipeline.ask("what are the issues in this repo")

        assert answer_result.answer == _NO_EVIDENCE_ANSWER
    finally:
        repo.close()
