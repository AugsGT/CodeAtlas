from dataclasses import dataclass, field

from codeatlas.retrieval.subgraph import Evidence
from codeatlas.validation.validator import extract_citations, validate, validate_structured_diagnosis


@dataclass
class _FakeStructuredDiagnosis:
    diagnosis: str = ""
    root_cause: str = ""
    affected_code: list = field(default_factory=list)


def make_evidence():
    return Evidence(
        intent="callers",
        nodes=[
            {"label": "CodeEntity", "id": "pkg/math_utils.py::add", "name": "add"},
            {"label": "Module", "path": "pkg/math_utils.py", "name": "math_utils"},
        ],
        edges=[],
    )


def test_extract_citations_finds_bracketed_ids():
    answer = "add() is defined here [pkg/math_utils.py::add] in this module [pkg/math_utils.py]."
    assert extract_citations(answer) == ["pkg/math_utils.py::add", "pkg/math_utils.py"]


def test_valid_citations_are_grounded():
    answer = "add() [pkg/math_utils.py::add] lives in [pkg/math_utils.py]."
    result = validate(answer, make_evidence())
    assert result.is_grounded
    assert result.unsupported_citations == []
    assert result.warnings == []


def test_fabricated_citation_is_flagged_unsupported():
    answer = "add() is called by a function not in evidence [pkg/other.py::mystery]."
    result = validate(answer, make_evidence())
    assert not result.is_grounded
    assert result.unsupported_citations == ["pkg/other.py::mystery"]
    assert len(result.warnings) == 1


def test_no_citations_with_evidence_present_is_not_grounded():
    """An uncited claim is exactly as unverifiable as a wrong one - a
    "Grounded" badge on an answer that cites nothing, despite real
    evidence being available, is a false claim of grounding. This was a
    real bug: is_grounded used to check only unsupported_citations, so an
    answer with ZERO citations (none of them unsupported, since there
    were none at all) was incorrectly reported as grounded."""
    answer = "add() does some arithmetic."
    result = validate(answer, make_evidence())
    assert not result.is_grounded
    assert any("no citations" in w.lower() for w in result.warnings)


def test_no_citations_with_no_evidence_is_fine():
    answer = "I don't have enough information to answer that."
    empty_evidence = Evidence(intent="callers", nodes=[], edges=[])
    result = validate(answer, empty_evidence)
    assert result.is_grounded
    assert result.warnings == []


def test_mix_of_valid_and_unsupported_citations():
    answer = "add() [pkg/math_utils.py::add] is called by [pkg/fake.py::ghost]."
    result = validate(answer, make_evidence())
    assert result.citations == ["pkg/math_utils.py::add", "pkg/fake.py::ghost"]
    assert result.unsupported_citations == ["pkg/fake.py::ghost"]
    assert not result.is_grounded


def test_citation_content_containing_a_literal_bracket_still_validates():
    """Real, live-reproduced bug: a small model cited a node by appending
    descriptive text after its id inside the same bracket, and that text
    (a Python syntax error message quoting "'['") contained a literal
    unmatched '[' - a naive "[^\\[\\]]+" regex stops at that embedded
    bracket and extracts garbage from the middle of the citation instead
    of the real id, wrongly flagging a correct, well-grounded answer as
    unsupported."""
    evidence = Evidence(
        intent="diagnosis",
        nodes=[{"label": "Module", "path": "tt.py", "parse_error": "SyntaxError: '[' was never closed (line 65)"}],
        edges=[],
    )
    answer = (
        "tt.py has a syntax error "
        "[tt.py: SyntaxError: '[' was never closed (line 65)]."
    )
    result = validate(answer, evidence)
    assert result.citations == ["tt.py"]
    assert result.unsupported_citations == []
    assert result.is_grounded


def test_backtick_quoted_bracket_characters_in_prose_are_not_mistaken_for_a_citation():
    """Real, live-reproduced bug (a different shape than the one above):
    asked to explain "'[' was never closed", the model correctly quoted
    the bracket characters themselves in prose using backticks - "an
    opening bracket `[` was never closed ... a corresponding closing
    bracket `]`" - and the fallback regex paired that stray, unrelated
    backtick-quoted `[` with the later backtick-quoted `]`, misreading
    the entire explanation in between as one fabricated citation, even
    though both of the model's REAL citations were well-formed."""
    evidence = Evidence(
        intent="diagnosis",
        nodes=[
            {"label": "Module", "path": "tt.py"},
            {"label": "Issue", "id": "issue:parse-error:tt.py"},
        ],
        edges=[],
    )
    answer = (
        "The codebase contains a critical syntax error in `tt.py` at line 65 "
        "[issue:parse-error:tt.py]. The error message indicates that an opening square "
        "bracket `[` was never closed. To fix this issue, you need to ensure that every "
        "opening square bracket has a corresponding closing square bracket `]`. Review "
        "the code around line 65 in `tt.py` and make the necessary corrections "
        "[issue:parse-error:tt.py]."
    )

    result = validate(answer, evidence)

    assert result.citations == ["issue:parse-error:tt.py", "issue:parse-error:tt.py"]
    assert result.unsupported_citations == []
    assert result.is_grounded


def test_validate_structured_diagnosis_grounded_via_prose_citations():
    diagnosis = _FakeStructuredDiagnosis(
        diagnosis="add() is called by [pkg/math_utils.py::add].",
        root_cause="It lives in [pkg/math_utils.py].",
    )
    result = validate_structured_diagnosis(diagnosis, make_evidence())

    assert result.is_grounded
    assert result.unsupported_citations == []


def test_validate_structured_diagnosis_checks_affected_code_list_too():
    """affected_code is itself a claim about which evidence is relevant -
    a fabricated entry there must be caught the same way an inline [id]
    citation would be, even with no prose citations at all."""
    diagnosis = _FakeStructuredDiagnosis(
        diagnosis="Something is wrong.",
        affected_code=["pkg/fake.py::ghost"],
    )
    result = validate_structured_diagnosis(diagnosis, make_evidence())

    assert not result.is_grounded
    assert "pkg/fake.py::ghost" in result.unsupported_citations


def test_validate_structured_diagnosis_valid_affected_code_is_grounded():
    diagnosis = _FakeStructuredDiagnosis(
        diagnosis="Something is wrong.",
        affected_code=["pkg/math_utils.py::add"],
    )
    result = validate_structured_diagnosis(diagnosis, make_evidence())

    assert result.is_grounded
    assert "pkg/math_utils.py::add" in result.citations


def test_validate_structured_diagnosis_normalizes_a_bare_name_in_affected_code():
    """Real, live-reproduced case: the model's affected_code sometimes
    names a bare function name ("calculate_discounted_price") instead of
    its full qualified id ("pricing.py::calculate_discounted_price") -
    not a fabrication, just imprecise. A genuinely well-grounded,
    correctly-cited diagnosis was showing "Ungrounded" purely because of
    this one unrelated field."""
    diagnosis = _FakeStructuredDiagnosis(
        diagnosis="Something is wrong [pkg/math_utils.py::add].",
        affected_code=["add"],
    )
    result = validate_structured_diagnosis(diagnosis, make_evidence())

    assert result.is_grounded
    assert "pkg/math_utils.py::add" in result.citations
    assert result.unsupported_citations == []


def test_validate_structured_diagnosis_does_not_guess_an_ambiguous_bare_name():
    """Two known ids share the same trailing name - normalizing to
    either one would be a guess, so the bare entry is left as-is and
    correctly flagged as unsupported rather than silently matched to the
    wrong one."""
    evidence = Evidence(
        intent="diagnosis",
        nodes=[
            {"label": "CodeEntity", "id": "pkg/a.py::helper"},
            {"label": "CodeEntity", "id": "pkg/b.py::helper"},
        ],
        edges=[],
    )
    diagnosis = _FakeStructuredDiagnosis(diagnosis="ok", affected_code=["helper"])

    result = validate_structured_diagnosis(diagnosis, evidence)

    assert "helper" in result.unsupported_citations


def test_fabricated_citation_with_no_known_id_prefix_is_still_flagged():
    """A citation that doesn't correspond to any known id at all (not
    just one with extra trailing text) must still be caught - the
    resilience to embedded brackets shouldn't come at the cost of
    catching genuine fabrication."""
    evidence = Evidence(
        intent="diagnosis",
        nodes=[{"label": "Module", "path": "tt.py", "parse_error": "SyntaxError: '[' was never closed (line 65)"}],
        edges=[],
    )
    answer = "tt.py has a syntax error [some.other.file::not_real]."
    result = validate(answer, evidence)
    assert result.unsupported_citations == ["some.other.file::not_real"]
    assert not result.is_grounded
