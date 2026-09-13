"""Deterministic grounding check for a reasoner's answer: every citation
the model made (its "[id]" markers) must match a real node id from the
evidence it was actually given.

This alone catches the specific failure mode that matters here — the
model naming a node/edge id it wasn't given, i.e. fabricating evidence.
A secondary LLM-based semantic validator (checking whether the prose
itself is actually supported by the evidence, beyond citation matching)
was considered, per the spec, but isn't implemented: it would spend
another model call re-litigating the same trust boundary the citation
check already enforces deterministically, for a failure mode
(plausible-sounding but uncited text) that citation-matching already
flags via the "no citations despite evidence" warning below.
"""

import re
from dataclasses import dataclass, field

from ..retrieval.subgraph import Evidence

_CITATION_PATTERN = re.compile(r"\[([^\[\]]+)\]")


def _citations_against_known_ids(answer: str, known_ids) -> list[str]:
    """Like a plain "[id]" regex extraction, but resilient to two things
    a small local model does in practice: (1) appending descriptive text
    after the id inside the same bracket instead of citing a bare id
    (e.g. "[tt.py: SyntaxError: ...]" instead of "[tt.py]"), and (2) that
    descriptive text - or even earlier, unrelated prose in the same
    answer - containing a stray literal bracket (a Python syntax error
    message quoting "'['"). A naive scan that treats every "[" as a
    citation start breaks on both: it can seize on a stray bracket in
    plain prose (before the real citation even starts) as if it opened
    one, or stop at the first "[" it hits while scanning a citation's own
    trailing text. Both are real, live-reproduced failures, not
    hypotheticals.

    Instead of scanning for "[" generally, this anchors on the known ids
    themselves: it searches for "[" immediately followed by each known id
    (so a stray bracket in ordinary prose, never followed by a real id,
    is simply never a match), then closes the citation at the next "]"
    found after that point regardless of what's in between. When two
    known ids both match at the same position (a CodeEntity id and its
    own module's bare path, one a prefix of the other), the longer/more
    specific one wins.

    Whatever text isn't claimed by a real citation this way is then
    checked with a plain bracket regex, so a genuinely fabricated
    citation (naming something not in evidence at all) is still caught.
    """
    best_by_pos = {}
    for kid in known_ids:
        marker = "[" + kid
        start = 0
        while True:
            pos = answer.find(marker, start)
            if pos == -1:
                break
            if pos not in best_by_pos or len(kid) > len(best_by_pos[pos]):
                best_by_pos[pos] = kid
            start = pos + 1

    citations = []
    spans = []
    consumed_until = -1
    for pos, kid in sorted(best_by_pos.items()):
        if pos <= consumed_until:
            continue  # overlaps a citation already claimed (e.g. a shorter, less specific id)
        close = answer.find("]", pos + len(kid) + 1)
        end = close if close != -1 else len(answer) - 1
        citations.append(kid)
        spans.append((pos, end))
        consumed_until = end

    masked = list(answer)
    for start, end in spans:
        for k in range(start, min(end + 1, len(masked))):
            masked[k] = " "
    _mask_backtick_quoted_brackets(masked)
    other = _CITATION_PATTERN.findall("".join(masked))

    return citations + other


def _mask_backtick_quoted_brackets(chars: list) -> None:
    """A backtick-quoted single bracket character (`` `[` `` or `` `]` ``)
    is prose describing the character itself, not a citation delimiter -
    a real, live-reproduced failure: asked to explain a syntax error
    like "'[' was never closed", the model naturally writes something
    like "an opening bracket `[` was never closed ... a corresponding
    closing bracket `]`", and the plain fallback regex below pairs that
    stray, unrelated `[` with the later `]` - misreading the entire
    explanation in between as one fabricated citation. Blanks the
    bracket character itself (in place) wherever this exact backtick-
    bracket-backtick pattern occurs, so the fallback regex never sees it
    as an unmatched delimiter to begin with.
    """
    text = "".join(chars)
    for match in re.finditer(r"`[\[\]]`", text):
        chars[match.start() + 1] = " "


@dataclass
class ValidationResult:
    citations: list[str] = field(default_factory=list)
    unsupported_citations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Whether the evidence handed to the reasoner had anything in it at
    # all - needed to tell "answered with no citations because there was
    # nothing to cite" (fine: see reasoner.py's deterministic no-evidence
    # refusal) apart from "answered with no citations despite real
    # evidence being available" (not fine - an uncited claim is exactly as
    # unverifiable as a wrong one).
    has_evidence: bool = False

    @property
    def is_grounded(self) -> bool:
        if self.has_evidence and not self.citations:
            return False
        return not self.unsupported_citations


def extract_citations(answer: str) -> list[str]:
    return _CITATION_PATTERN.findall(answer)


def known_node_ids(evidence: Evidence) -> set:
    return {node.get("id", node.get("path")) for node in evidence.nodes} - {None}


def validate(answer: str, evidence: Evidence) -> ValidationResult:
    valid_ids = known_node_ids(evidence)
    citations = _citations_against_known_ids(answer, valid_ids)
    unsupported = [c for c in citations if c not in valid_ids]

    warnings = [
        f"Citation {c!r} does not match any node in the retrieved evidence."
        for c in unsupported
    ]
    if not citations and evidence.nodes:
        warnings.append("Answer made no citations despite evidence being available.")

    return ValidationResult(
        citations=citations, unsupported_citations=unsupported, warnings=warnings,
        has_evidence=bool(evidence.nodes),
    )


def _normalize_affected_code_entry(entry: str, known_ids: set) -> str:
    """affected_code sometimes names a bare function/method name instead
    of its full qualified id (e.g. "calculate_discounted_price" instead
    of "pricing.py::calculate_discounted_price") - a real, live-observed
    case, not a fabrication: the model is clearly referring to a real
    entity in evidence, just imprecisely. If `entry` exactly matches the
    trailing qualified-name component of exactly ONE known id, substitute
    that full id so it validates the same way a proper citation would.
    Ambiguous (matches more than one known id) or no match at all - leave
    it as-is, so a genuinely fabricated or ambiguous entry is still
    flagged rather than guessed through.
    """
    if entry in known_ids:
        return entry
    matches = [kid for kid in known_ids if isinstance(kid, str) and kid.rsplit("::", 1)[-1] == entry]
    if len(matches) == 1:
        return matches[0]
    return entry


def validate_structured_diagnosis(diagnosis, evidence: Evidence) -> ValidationResult:
    """Structured counterpart to validate(), for a StructuredDiagnosis
    (see reasoning/reasoner.py::GroundedReasoner.diagnose) rather than a
    plain prose answer. Reuses validate() unchanged rather than
    duplicating citation-matching logic: `affected_code` is itself a
    list of ids the model claims are relevant, so it's checked the same
    way a citation is - by rendering it as [id] markers and folding it
    into the same text validate() already knows how to check alongside
    the diagnosis/root_cause prose's own inline citations. Each entry is
    normalized first (see _normalize_affected_code_entry) so a bare name
    referring to a real entity isn't misread as fabricated.
    """
    known_ids = known_node_ids(evidence)
    normalized = [_normalize_affected_code_entry(cid, known_ids) for cid in diagnosis.affected_code]
    affected_code_citations = " ".join(f"[{cid}]" for cid in normalized)
    combined_text = f"{diagnosis.diagnosis} {diagnosis.root_cause} {affected_code_citations}"
    return validate(combined_text, evidence)
