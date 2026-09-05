import datetime
import json
from pathlib import Path

import pytest

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.issues import write_static_issues
from codeatlas.graph.models import RuntimeSpan
from codeatlas.graph.repository import GraphRepository
from codeatlas.reasoning.pipeline import ReasoningPipeline
from tests.conftest import KNOWN_WORKING_SERVICE_PY

SAMPLE_REPO = Path(__file__).parent.parent / "sample_repo"
SERVICE_PY = SAMPLE_REPO / "pkg" / "service.py"


@pytest.fixture
def populated_repo(tmp_path):
    # sample_repo/pkg/service.py is the user's own live sandbox and
    # changes independently of this suite (see conftest.py) - pin it so
    # this fixture's analysis result is deterministic.
    original_service_py = SERVICE_PY.read_text()
    SERVICE_PY.write_text(KNOWN_WORKING_SERVICE_PY)

    repo = GraphRepository(tmp_path / "graph.db")
    result = PythonAstAnalyzer().analyze(str(SAMPLE_REPO))
    SERVICE_PY.write_text(original_service_py)
    write_analysis_result(repo, result)

    now = datetime.datetime.now(datetime.timezone.utc)
    repo.upsert_runtime_span(RuntimeSpan(
        id="span1", trace_id="trace1", name="add",
        start_time=now, end_time=now, duration_ms=1.5, status="OK",
        code_filepath=str(SAMPLE_REPO / "pkg" / "math_utils.py"),
        code_function="add",
    ))
    repo.add_produces("pkg/math_utils.py::add", "span1")

    yield repo
    repo.close()


def test_pipeline_answers_callers_question_end_to_end(populated_repo, ollama_client):
    pipeline = ReasoningPipeline(populated_repo, ollama_client)

    result = pipeline.ask("Who calls the add function?")

    assert result.classification.intent == "callers"
    assert result.resolved_target == ("entity", "pkg/math_utils.py::add")
    node_ids = {n.get("id") for n in result.evidence.nodes}
    assert "pkg/service.py::Calculator.compute" in node_ids
    assert isinstance(result.answer, str) and len(result.answer) > 0
    assert result.validation.is_grounded


def test_pipeline_falls_back_to_recent_activity_when_target_unresolvable(populated_repo, ollama_client):
    pipeline = ReasoningPipeline(populated_repo, ollama_client)

    result = pipeline.ask("Who calls totally_nonexistent_function_xyz?")

    assert result.resolved_target == (None, None)
    assert result.evidence.intent == "recent_activity"


class _FakeFollowUpClient:
    """Deterministic stand-in for a real LLM: always classifies a pure
    follow-up (empty target), so this test exercises pipeline.py's
    carry-forward logic itself, not the model's reference resolution."""

    def generate(self, prompt, format=None, temperature=0.0):
        if format == "json":
            return '{"intent": "callees", "target": ""}'
        return "no evidence needed for this test"


def test_pipeline_carries_forward_target_for_follow_up_question(populated_repo):
    pipeline = ReasoningPipeline(populated_repo, _FakeFollowUpClient())
    history = [{
        "question": "Tell me about the compute method",
        "answer": "It's a method on Calculator.",
        "resolved_target": {"kind": "entity", "id": "pkg/service.py::Calculator.compute"},
    }]

    result = pipeline.ask("What does it call?", history=history)

    assert result.resolved_target == ("entity", "pkg/service.py::Calculator.compute")
    callee_ids = {n.get("id") for n in result.evidence.nodes if n.get("label") == "CodeEntity"}
    assert "pkg/math_utils.py::add" in callee_ids
    assert "pkg/math_utils.py::square" in callee_ids


class _FakeEntityIntentButModuleTargetClient:
    """Deterministic reproduction of a real crash: the classifier picks an
    entity-shaped intent (entity_overview) but the target it extracted
    actually resolves to a Module, not a CodeEntity - e.g. "what is
    run_workload.py". Before the _ENTITY_INTENTS/_MODULE_INTENTS kind
    check in pipeline.py, this crashed with
    "SubgraphRetriever.entity_overview() got an unexpected keyword
    argument 'module_path'" instead of falling back gracefully."""

    def generate(self, prompt, format=None, temperature=0.0):
        if format == "json":
            return '{"intent": "entity_overview", "target": "pkg/service.py"}'
        return "no evidence needed for this test"


def test_pipeline_falls_back_to_diagnosis_when_target_resolves_to_wrong_kind_for_intent(populated_repo):
    pipeline = ReasoningPipeline(populated_repo, _FakeEntityIntentButModuleTargetClient())

    result = pipeline.ask("what is pkg/service.py")

    assert result.resolved_target == ("module", "pkg/service.py")
    # diagnosis, not module_dependencies or recent_activity: it's a strict
    # superset of module_dependencies for a module target (same Module
    # node + CONTAINS, plus each entity's own runtime evidence), so it's
    # the most informative fallback for a resolved-but-mismatched module -
    # in particular it's what surfaces Module.parse_error for a file that
    # failed to parse, which neither of the others would.
    assert result.evidence.intent == "diagnosis"
    assert isinstance(result.answer, str)


class _FakeDiagnosisClient:
    def __init__(self, target):
        self._target = target

    def generate(self, prompt, format=None, temperature=0.0):
        if format == "json":
            return f'{{"intent": "diagnosis", "target": "{self._target}"}}'
        return "no evidence needed for this test"


class _FakeStructuredDiagnosisClient:
    """First format="json" call is the intent classifier; the second is
    the structured diagnose() call - distinguished by call order since
    pipeline.ask() always classifies before reasoning."""

    def __init__(self, target, diagnosis_obj):
        self._target = target
        self._diagnosis_json = json.dumps(diagnosis_obj)
        self._json_calls = 0

    def generate(self, prompt, format=None, temperature=0.0):
        if format == "json":
            self._json_calls += 1
            if self._json_calls == 1:
                return f'{{"intent": "diagnosis", "target": "{self._target}"}}'
            return self._diagnosis_json
        return "unused"


def test_pipeline_populates_structured_diagnosis_for_a_module_target(populated_repo):
    client = _FakeStructuredDiagnosisClient("pkg/service.py", {
        "diagnosis": "pkg/service.py has a real problem [pkg/service.py].",
        "root_cause": "explained here [pkg/service.py].",
        "affected_code": ["pkg/service.py"],
        "proposed_fix": "do X.",
        "improved_code": "",
        "confidence_basis": "VERIFIED: the module exists. SUGGESTED: the fix.",
        "limitations": "",
    })
    pipeline = ReasoningPipeline(populated_repo, client)

    result = pipeline.ask("what is wrong with pkg/service.py")

    assert result.evidence.intent == "diagnosis"
    assert result.structured_diagnosis is not None
    assert result.structured_diagnosis.proposed_fix == "do X."
    assert "pkg/service.py has a real problem" in result.answer
    assert "Proposed fix: do X." in result.answer
    assert result.validation.is_grounded


def test_pipeline_routes_diagnosis_for_a_module_target(populated_repo):
    pipeline = ReasoningPipeline(populated_repo, _FakeDiagnosisClient("pkg/service.py"))

    result = pipeline.ask("what is wrong with pkg/service.py")

    assert result.resolved_target == ("module", "pkg/service.py")
    assert result.evidence.intent == "diagnosis"
    node_ids = {n.get("id") or n.get("path") for n in result.evidence.nodes}
    assert "pkg/service.py" in node_ids
    assert "pkg/service.py::Calculator.compute" in node_ids


def test_pipeline_routes_diagnosis_for_an_entity_target(populated_repo):
    pipeline = ReasoningPipeline(populated_repo, _FakeDiagnosisClient("add"))

    result = pipeline.ask("why does add fail?")

    assert result.resolved_target == ("entity", "pkg/math_utils.py::add")
    assert result.evidence.intent == "diagnosis"
    node_ids = {n.get("id") for n in result.evidence.nodes}
    assert "pkg/math_utils.py::add" in node_ids
    assert "span1" in node_ids  # the OK span from the fixture - still relevant runtime evidence


def test_pipeline_diagnosis_with_no_resolvable_target_falls_back_to_recent_errors(populated_repo):
    """recent_errors, not the generic recent_activity - it's Issue-aware
    (see graph/issues.py), so a vague diagnosis question can surface a
    static-only problem (a syntax error) even on a repo that's never been
    executed, which a RuntimeSpan-only survey structurally cannot."""
    pipeline = ReasoningPipeline(populated_repo, _FakeDiagnosisClient("totally_nonexistent_xyz"))

    result = pipeline.ask("what is wrong with totally_nonexistent_xyz?")

    assert result.resolved_target == (None, None)
    assert result.evidence.intent == "recent_errors"


class _FakeModuleDependenciesClient:
    def generate(self, prompt, format=None, temperature=0.0):
        if format == "json":
            return '{"intent": "module_dependencies", "target": "pkg/service.py"}'
        return "no evidence needed for this test"


def test_module_dependencies_is_not_rerouted_to_diagnosis_when_it_already_matches(populated_repo):
    """A genuine "what does this module depend on" question that already
    resolves cleanly must stay module_dependencies - the diagnosis
    fallback only kicks in for an actual intent/kind mismatch, not every
    question naming a module."""
    pipeline = ReasoningPipeline(populated_repo, _FakeModuleDependenciesClient())

    result = pipeline.ask("what does pkg/service.py depend on?")

    assert result.evidence.intent == "module_dependencies"


class _FakeTTPyDiagnosisClient:
    """Reproduces the exact production incident this was built to fix: a
    module with a real syntax error, no execution at all, asked "what is
    wrong in tt.py?" - with an answer shaped exactly like what the real
    small local model produced live: citing the Issue's id with
    descriptive text appended in the same bracket, including a literal
    '[' from the quoted syntax error message itself. That specific shape
    is what broke the old citation regex and got a correct answer wrongly
    flagged "Ungrounded". Diagnosis now goes through the structured JSON
    path (see reasoning/reasoner.py::diagnose), so this returns the
    pathological citation inside the "diagnosis" field of that JSON
    rather than as plain prose - the underlying bracket-matching problem
    is identical either way."""

    def __init__(self):
        self._json_calls = 0

    def generate(self, prompt, format=None, temperature=0.0):
        if format == "json":
            self._json_calls += 1
            if self._json_calls == 1:
                return '{"intent": "diagnosis", "target": "tt.py"}'
            return json.dumps({
                "diagnosis": (
                    "The immediate blocking error in tt.py is a SyntaxError: '[' was never "
                    "closed (line 65) [issue:parse-error:tt.py: SyntaxError: '[' was never "
                    "closed (line 65)]."
                ),
                "root_cause": "",
                "affected_code": [],
                "proposed_fix": "",
                "improved_code": "",
                "confidence_basis": "",
                "limitations": "",
            })
        return "unused"


def test_tt_py_diagnosis_end_to_end_reproduces_the_reported_bug(tmp_path):
    """Full reproduction, from ingest through validation, of the reported
    incident: "What is wrong in tt.py?" on a file with a real syntax
    error and zero execution. Before this change: the Issue didn't exist
    so evidence was just a bare Module property, and even a correct
    citation of it would have been misparsed as unsupported. Confirms
    all the fixed pieces work together, not just in isolation."""
    (tmp_path / "tt.py").write_text("def broken(:\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(tmp_path))
        write_analysis_result(repo, result)
        write_static_issues(repo, result)

        pipeline = ReasoningPipeline(repo, _FakeTTPyDiagnosisClient())
        answer_result = pipeline.ask("What is wrong in tt.py?")

        assert answer_result.resolved_target == ("module", "tt.py")
        assert answer_result.evidence.intent == "diagnosis"
        labels = {n["label"] for n in answer_result.evidence.nodes}
        assert "Issue" in labels
        assert answer_result.validation.is_grounded
        assert answer_result.validation.citations == ["issue:parse-error:tt.py"]
        assert answer_result.validation.unsupported_citations == []
    finally:
        repo.close()


class _FakeRecentActivityClient:
    """The classifier picks recent_activity DIRECTLY for a phrasing like
    "what happened" - it's not routed through the diagnosis no-target
    fallback at all, since the classifier's own prompt explicitly maps
    this phrasing to recent_activity. Reproduces that real routing."""

    def generate(self, prompt, format=None, temperature=0.0):
        if format == "json":
            return '{"intent": "recent_activity", "target": ""}'
        return "unused"


def test_what_happened_surfaces_a_static_issue_on_a_never_executed_repo(tmp_path):
    """Full pipeline reproduction of the reported bug: "what happened" on
    a repo with a real syntax error and zero execution used to answer
    "I don't have enough evidence" despite the exact same issue already
    being visible in the Alerts panel - recent_activity only ever
    surveyed RuntimeSpans, never Issue nodes."""
    (tmp_path / "tt.py").write_text("def broken(:\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(tmp_path))
        write_analysis_result(repo, result)
        write_static_issues(repo, result)

        pipeline = ReasoningPipeline(repo, _FakeRecentActivityClient())
        answer_result = pipeline.ask("what happened")

        assert answer_result.evidence.intent == "recent_activity"
        labels = {n["label"] for n in answer_result.evidence.nodes}
        assert "Issue" in labels
        assert answer_result.answer != (
            "I don't have enough evidence in the graph to answer this — no matching nodes were "
            "retrieved for this question. Try ingesting more data, or asking about something "
            "already present in the codebase."
        )
    finally:
        repo.close()


def test_pipeline_follow_up_conversation_end_to_end(populated_repo, ollama_client):
    pipeline = ReasoningPipeline(populated_repo, ollama_client)

    first = pipeline.ask("Tell me about the compute method")
    history = [{
        "question": first.question,
        "answer": first.answer,
        "resolved_target": {"kind": first.resolved_target[0], "id": first.resolved_target[1]},
    }]

    second = pipeline.ask("What does it call?", history=history)

    callee_ids = {n.get("id") for n in second.evidence.nodes if n.get("label") == "CodeEntity"}
    assert "pkg/math_utils.py::add" in callee_ids or "pkg/math_utils.py::square" in callee_ids
