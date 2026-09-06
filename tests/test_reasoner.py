import json

from codeatlas.reasoning.reasoner import (
    GroundedReasoner,
    StructuredDiagnosis,
    _parse_structured_diagnosis,
    _strip_code_fence,
    format_evidence,
    format_history,
    render_diagnosis_as_text,
)
from codeatlas.retrieval.subgraph import Evidence


def make_evidence():
    return Evidence(
        intent="callers",
        nodes=[
            {"label": "CodeEntity", "id": "pkg/math_utils.py::add", "name": "add", "kind": "function"},
            {"label": "CodeEntity", "id": "pkg/service.py::Calculator.compute", "name": "compute", "kind": "method"},
        ],
        edges=[
            {"type": "CALLS", "from": "pkg/service.py::Calculator.compute", "to": "pkg/math_utils.py::add", "call_line": 6},
        ],
    )


class RecordingClient:
    """Fake OllamaClient that records the prompt instead of calling out."""

    def __init__(self):
        self.last_prompt = None

    def generate(self, prompt, format=None, temperature=0.0):
        self.last_prompt = prompt
        return "recorded"


def test_format_evidence_includes_node_and_edge_ids():
    text = format_evidence(make_evidence())
    assert "pkg/math_utils.py::add" in text
    assert "pkg/service.py::Calculator.compute" in text
    assert "CALLS" in text


def test_format_history_empty_is_blank():
    assert format_history(None) == ""
    assert format_history([]) == ""


def test_format_history_includes_prior_turns():
    history = [{"question": "What is add?", "answer": "It sums two numbers [pkg/math_utils.py::add]."}]
    text = format_history(history)
    assert "What is add?" in text
    assert "It sums two numbers" in text


def test_reasoner_cites_a_node_id_from_evidence(ollama_client):
    reasoner = GroundedReasoner(ollama_client)
    evidence = make_evidence()

    answer = reasoner.answer("Who calls add?", evidence)

    assert "pkg/service.py::Calculator.compute" in answer or "pkg/math_utils.py::add" in answer


def test_reasoner_refuses_deterministically_when_evidence_is_empty():
    client = RecordingClient()
    reasoner = GroundedReasoner(client)
    empty_evidence = Evidence(intent="callers", nodes=[], edges=[])

    answer = reasoner.answer("Who calls a function that doesn't exist?", empty_evidence)

    assert "don't have enough evidence" in answer
    assert client.last_prompt is None  # short-circuited before ever calling the LLM


def test_reasoner_includes_history_in_prompt_when_evidence_present():
    client = RecordingClient()
    reasoner = GroundedReasoner(client)
    history = [{"question": "What is add?", "answer": "It sums two numbers."}]

    reasoner.answer("Who calls it?", make_evidence(), history=history)

    assert "What is add?" in client.last_prompt


def test_diagnosis_intent_adds_blocking_vs_additional_vs_unverifiable_guidance():
    client = RecordingClient()
    reasoner = GroundedReasoner(client)

    reasoner.answer("what is wrong with add?", make_evidence(), intent="diagnosis")

    assert "immediate blocking error" in client.last_prompt
    assert "Never invent an additional bug" in client.last_prompt


def test_non_diagnosis_intent_omits_diagnosis_guidance():
    client = RecordingClient()
    reasoner = GroundedReasoner(client)

    reasoner.answer("who calls add?", make_evidence(), intent="callers")

    assert "immediate blocking error" not in client.last_prompt


class JsonRecordingClient:
    """Fake client that records the prompt and returns pre-set JSON."""

    def __init__(self, response_obj):
        self.last_prompt = None
        self.last_format = None
        self._response_obj = response_obj

    def generate(self, prompt, format=None, temperature=0.0):
        self.last_prompt = prompt
        self.last_format = format
        return json.dumps(self._response_obj)


def test_parse_structured_diagnosis_from_valid_json():
    raw = json.dumps({
        "diagnosis": "tt.py has a syntax error [tt.py].",
        "root_cause": "An unclosed bracket at line 65 [tt.py].",
        "affected_code": ["tt.py"],
        "proposed_fix": "Close the bracket.",
        "improved_code": "stack[-1]",
        "confidence_basis": "VERIFIED: the parse error. SUGGESTED: the fix.",
        "limitations": "Nothing past the syntax error could be checked.",
    })

    result = _parse_structured_diagnosis(raw)

    assert result.diagnosis == "tt.py has a syntax error [tt.py]."
    assert result.affected_code == ["tt.py"]
    assert result.proposed_fix == "Close the bracket."
    assert "VERIFIED" in result.confidence_basis
    assert result.raw_response == raw


def test_parse_structured_diagnosis_falls_back_on_malformed_json():
    result = _parse_structured_diagnosis("this is not json")

    assert result.diagnosis == "this is not json"
    assert result.affected_code == []


def test_parse_structured_diagnosis_falls_back_on_non_dict_json():
    result = _parse_structured_diagnosis("[1, 2, 3]")

    assert result.diagnosis == "[1, 2, 3]"


def test_strip_code_fence_removes_a_full_markdown_wrap():
    fenced = "```python\ndef helper():\n    return 1\n```"
    assert _strip_code_fence(fenced) == "def helper():\n    return 1"


def test_strip_code_fence_removes_a_bare_fence_with_no_language_tag():
    fenced = "```\ndef helper():\n    return 1\n```"
    assert _strip_code_fence(fenced) == "def helper():\n    return 1"


def test_strip_code_fence_leaves_unfenced_code_untouched():
    code = "def helper():\n    return 1"
    assert _strip_code_fence(code) == code


def test_strip_code_fence_leaves_a_stray_triple_backtick_untouched():
    """Only strips when the fence wraps the WHOLE string (opening line,
    closing line) - a stray ``` that isn't a genuine full wrap is left
    alone rather than guessed at."""
    code = "some code ``` with a stray fence marker in it"
    assert _strip_code_fence(code) == code


def test_parse_structured_diagnosis_strips_a_markdown_fence_from_improved_code():
    """Real corruption risk if left unfixed: improved_code gets written
    verbatim into the user's actual file by apply_fix_to_repository - a
    stray ```python fence ending up there would break the file, not just
    look untidy."""
    raw = json.dumps({
        "diagnosis": "ok", "root_cause": "", "affected_code": [], "proposed_fix": "fix it",
        "improved_code": "```python\ndef helper():\n    return 2\n```",
        "confidence_basis": "", "limitations": "",
    })

    result = _parse_structured_diagnosis(raw)

    assert result.improved_code == "def helper():\n    return 2"


def test_parse_structured_diagnosis_tolerates_missing_and_wrong_typed_keys():
    raw = json.dumps({"diagnosis": "ok", "affected_code": "not-a-list", "proposed_fix": 5})

    result = _parse_structured_diagnosis(raw)

    assert result.diagnosis == "ok"
    assert result.affected_code == []
    assert result.proposed_fix == ""


def test_render_diagnosis_as_text_combines_fields_present():
    d = StructuredDiagnosis(
        diagnosis="tt.py has a syntax error [tt.py].",
        root_cause="An unclosed bracket.",
        proposed_fix="Close the bracket.",
        limitations="Nothing past this could be checked.",
    )

    text = render_diagnosis_as_text(d)

    assert "tt.py has a syntax error" in text
    assert "Root cause: An unclosed bracket." in text
    assert "Proposed fix: Close the bracket." in text
    assert "Limitations: Nothing past this could be checked." in text


def test_render_diagnosis_as_text_omits_empty_fields():
    d = StructuredDiagnosis(diagnosis="tt.py has a syntax error [tt.py].")

    text = render_diagnosis_as_text(d)

    assert text == "tt.py has a syntax error [tt.py]."


def test_render_diagnosis_as_text_falls_back_to_raw_response_when_empty():
    d = StructuredDiagnosis(raw_response="this is not json")

    assert render_diagnosis_as_text(d) == "this is not json"


def test_diagnose_refuses_deterministically_when_evidence_is_empty():
    client = JsonRecordingClient({})
    reasoner = GroundedReasoner(client)
    empty_evidence = Evidence(intent="diagnosis", nodes=[], edges=[])

    result = reasoner.diagnose("what is wrong?", empty_evidence)

    assert "don't have enough evidence" in result.diagnosis
    assert client.last_prompt is None  # short-circuited before ever calling the LLM


def test_diagnose_uses_json_format_and_parses_the_response():
    client = JsonRecordingClient({
        "diagnosis": "add() is called by compute() [pkg/math_utils.py::add].",
        "root_cause": "",
        "affected_code": ["pkg/math_utils.py::add"],
        "proposed_fix": "",
        "improved_code": "",
        "confidence_basis": "VERIFIED: the call edge.",
        "limitations": "",
    })
    reasoner = GroundedReasoner(client)

    result = reasoner.diagnose("what is wrong with add?", make_evidence())

    assert client.last_format == "json"
    assert "add() is called by compute()" in result.diagnosis
    assert result.affected_code == ["pkg/math_utils.py::add"]
    assert "VERIFIED" in result.confidence_basis
