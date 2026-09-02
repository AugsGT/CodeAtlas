# This file contains functions to check if a reasoner's answer is grounded in the evidence it was given.

import re
from dataclasses import dataclass, field

from ..retrieval.subgraph import Evidence

_CITATION_PATTERN = re.compile(r"\[([^\[\]]+)\]")


def _citations_against_known_ids(answer: str, known_ids) -> list[str]:
    """Extracts citations from the answer that match known IDs in the evidence.
    
    Handles cases where the citation might include additional text or a stray bracket.
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
            continue
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
    """Mask backtick-quoted brackets to avoid misinterpretation as citation delimiters."""
    text = "".join(chars)
    for match in re.finditer(r"`[\[\]]`", text):
        chars[match.start() + 1] = " "


@dataclass
class ValidationResult:
    citations: list[str] = field(default_factory=list)
    unsupported_citations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    has_evidence: bool = False

    @property
    def is_grounded(self) -> bool:
        if self.has_evidence and not self.citations:
            return False
        return not self.unsupported_citations


def extract_citations(answer: str) -> list[str]:
    """Extracts all citations from the answer."""
    return _CITATION_PATTERN.findall(answer)


def known_node_ids(evidence: Evidence) -> set:
    """Returns a set of known node IDs from the evidence."""
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
    """Normalizes affected code entries to full qualified IDs."""
    if entry in known_ids:
        return entry
    matches = [kid for kid in known_ids if isinstance(kid, str) and kid.rsplit("::", 1)[-1] == entry]
    if len(matches) == 1:
        return matches[0]
    return entry


def validate_structured_diagnosis(diagnosis, evidence: Evidence) -> ValidationResult:
    """Validates a structured diagnosis by checking its affected code entries."""
    known_ids = known_node_ids(evidence)
    normalized = [_normalize_affected_code_entry(cid, known_ids) for cid in diagnosis.affected_code]
    affected_code_citations = " ".join(f"[{cid}]" for cid in normalized)
    combined_text = f"{diagnosis.diagnosis} {diagnosis.root_cause} {affected_code_citations}"
    return validate(combined_text, evidence)