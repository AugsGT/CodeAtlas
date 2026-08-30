"""Grounded reasoning: answer a question using only the retrieved
evidence, citing the node/edge ids the answer is based on so the
answer can be checked against the graph (Phase 7 validation).

Diagnosis questions ("what's wrong", "how do I fix this") get a
separate, structured JSON output (StructuredDiagnosis) instead of plain
prose - the spec's own reasoner contract (diagnosis/root_cause/
affected_code/proposed_fix/improved_code/confidence_basis/limitations),
scoped to just the diagnosis intent since a fix/root-cause breakdown
doesn't apply to a structural question like "who calls X". Other
intents keep the simpler prose+citation path below.
"""

import json
from dataclasses import dataclass, field

from ..retrieval.subgraph import Evidence
from .ollama_client import OllamaClient

_PROMPT_TEMPLATE = """You are CodeAtlas, a tool that answers questions about a codebase using \
only graph evidence retrieved from static analysis and runtime telemetry.

Rules:
- Answer using ONLY the evidence below. Do not invent facts not present in it.
- After EVERY factual statement, cite the exact node id(s) in square brackets it came from, \
copied character-for-character from the evidence's [id] markers. Never paraphrase or shorten an id.
- Example of the required style: "add() is called by Calculator.compute() [pkg/service.py::Calculator.compute]."
- If the evidence includes a CALLS edge between two entities that both have their own runtime \
evidence (spans, errors, slow durations), explain the likely causal relationship between them \
(e.g. "X is slow because it calls Y, which took 2.8s") rather than reporting the two facts \
separately - that correlation is exactly why both were retrieved together.
- If a CodeEntity's source code is included in the evidence, use it to explain WHAT the code does \
and why it behaves the way the runtime evidence shows, not just that it does - and still cite that \
entity's [id] in the sentence explaining it, the same as any other claim. Quoting or describing its \
source is not a substitute for citing it.
- If the evidence does not contain enough information to answer, say so plainly instead of guessing.
{diagnosis_block}{history_block}
Evidence:
{evidence}

Question: {question}

Answer:"""

_DIAGNOSIS_GUIDANCE = """\
- This is a diagnosis question ("what's wrong", "why is it failing"). Structure the answer around:
  (a) the immediate blocking error shown in the evidence (a parse_error property, an ERROR-status \
span, an ERROR/WARN log) - state it first;
  (b) any OTHER problem the evidence actually shows - only if it is actually present;
  (c) if the evidence shows execution never got past an earlier failure (e.g. a syntax error means \
the file never ran), say plainly that anything past that point could not be checked, instead of \
guessing what else might be wrong.
- Never invent an additional bug the evidence doesn't show.
- Example for a module-level error: "pricing.py has a syntax error on line 65 [pricing.py]. \
Because it failed to parse, nothing else in this file could be checked."
"""

_NO_EVIDENCE_ANSWER = (
    "I don't have enough evidence in the graph to answer this — no matching nodes were "
    "retrieved for this question. Try ingesting more data, or asking about something "
    "already present in the codebase."
)

_DIAGNOSIS_JSON_PROMPT = """You are CodeAtlas, a tool that diagnoses problems in a codebase using \
only graph evidence retrieved from static analysis and runtime telemetry.

Respond with ONLY a JSON object with exactly these keys:
{{
  "diagnosis": "what is wrong, in plain language. Cite evidence with [id] markers exactly as they appear below, character-for-character.",
  "root_cause": "WHY it happens - the underlying mechanism. Cite [id] markers for anything directly observed in evidence.",
  "affected_code": ["evidence id(s) - without brackets - this diagnosis is grounded in, copied character-for-character"],
  "proposed_fix": "a minimal, concrete fix for the root cause - empty string if the evidence isn't enough to propose one",
  "improved_code": "REQUIRED whenever proposed_fix is non-empty AND a source snippet for the affected function/method is present in evidence below - see format rules for this field. Empty string only when no fix is being proposed, or no source snippet exists to correct.",
  "confidence_basis": "explicitly separate what is VERIFIED (directly shown in evidence), INFERRED (a reasonable conclusion beyond what's directly shown), and SUGGESTED (an improvement/fix recommendation, not an observed fact) - use those three words",
  "limitations": "what could NOT be checked from this evidence, or what would need more information"
}}

Rules:
- Every claim in "diagnosis" and "root_cause" must cite the evidence [id](s) it's based on.
- Never invent a fact, node, or id not present in the evidence.
- If the evidence shows execution never got past an earlier failure (e.g. a syntax error means the \
file never ran), say so plainly in "limitations" instead of guessing what else might be wrong.
- If there isn't enough evidence to propose ANY fix (e.g. the root cause is genuinely ambiguous), \
leave "proposed_fix" and "improved_code" as empty strings rather than guessing.
- "improved_code" format, when a source snippet IS present and you ARE proposing a fix: it must be \
the COMPLETE function or method, from its "def" line through its final line, not a diff and not just \
the changed lines - it replaces the entire original snippet verbatim, so an incomplete fragment would \
corrupt the file. Do not wrap it in markdown code fences. Example, given a source snippet showing \
"def divide(a, b):\\n    return a / b" and a ZeroDivisionError when b is 0:
  "proposed_fix": "Guard against b being zero before dividing.",
  "improved_code": "def divide(a, b):\\n    if b == 0:\\n        return 0\\n    return a / b"
{history_block}
Evidence:
{evidence}

Question: {question}

JSON:"""


@dataclass
class StructuredDiagnosis:
    diagnosis: str = ""
    root_cause: str = ""
    affected_code: list = field(default_factory=list)
    proposed_fix: str = ""
    improved_code: str = ""
    confidence_basis: str = ""
    limitations: str = ""
    raw_response: str = ""


def _parse_structured_diagnosis(raw: str) -> StructuredDiagnosis:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # A small model occasionally produces malformed JSON even in
        # "format": "json" mode - fall back to treating the raw text as
        # the diagnosis itself rather than crashing or silently hiding
        # the failure. Validation still runs against this (via
        # validate_structured_diagnosis), so a malformed response with
        # no real citations is visibly less-grounded, not silently wrong.
        return StructuredDiagnosis(diagnosis=raw, raw_response=raw)

    if not isinstance(data, dict):
        return StructuredDiagnosis(diagnosis=raw, raw_response=raw)

    def _str(key):
        value = data.get(key)
        return value if isinstance(value, str) else ""

    affected_code = data.get("affected_code")
    if not isinstance(affected_code, list):
        affected_code = []
    affected_code = [str(x) for x in affected_code if isinstance(x, (str, int, float))]

    return StructuredDiagnosis(
        diagnosis=_str("diagnosis"),
        root_cause=_str("root_cause"),
        affected_code=affected_code,
        proposed_fix=_str("proposed_fix"),
        improved_code=_strip_code_fence(_str("improved_code")),
        confidence_basis=_str("confidence_basis"),
        limitations=_str("limitations"),
        raw_response=raw,
    )


def _strip_code_fence(code: str) -> str:
    """Defensive cleanup: the prompt explicitly tells the model not to
    wrap improved_code in markdown fences, but a small model doesn't
    always comply - this is applied on the write path
    (apply_fix_to_repository) to replace real source, so a stray
    ```python fence ending up in the user's actual file is a real,
    concrete corruption risk, not just a cosmetic issue. Only strips a
    fence that wraps the ENTIRE string (leading ``` line, trailing ```
    line) - leaves anything else untouched rather than guessing."""
    stripped = code.strip()
    if not stripped.startswith("```"):
        return code
    lines = stripped.splitlines()
    if len(lines) < 2 or lines[-1].strip() != "```":
        return code
    return "\n".join(lines[1:-1])


def render_diagnosis_as_text(d: StructuredDiagnosis) -> str:
    """Flatten a StructuredDiagnosis into prose - used for the
    dashboard's existing single-text conversation display and for
    conversation history (format_history re-injects prior answers into
    later prompts, where prose reads far better than raw JSON)."""
    if not d.diagnosis and not d.root_cause:
        return d.raw_response or ""
    parts = [d.diagnosis]
    if d.root_cause:
        parts.append(f"Root cause: {d.root_cause}")
    if d.proposed_fix:
        parts.append(f"Proposed fix: {d.proposed_fix}")
    if d.limitations:
        parts.append(f"Limitations: {d.limitations}")
    return "\n\n".join(p for p in parts if p)


def format_evidence(evidence: Evidence) -> str:
    lines = ["Nodes:"]
    for node in evidence.nodes:
        node_id = node.get("id", node.get("path"))
        source = node.get("source")
        props = {k: v for k, v in node.items() if k not in ("label", "id", "path", "source")}
        lines.append(f"- [{node_id}] ({node['label']}) {props}")
        # Kept out of the props dict and rendered as a real code block -
        # multi-line source squashed into a dict repr (escaped newlines,
        # no indentation) is much harder for a small model to actually
        # read as code than a fenced block is.
        if source:
            lines.append(f"  source of [{node_id}]:\n```python\n{source}```")

    lines.append("Edges:")
    for edge in evidence.edges:
        props = {k: v for k, v in edge.items() if k not in ("type", "from", "to")}
        extra = f" {props}" if props else ""
        lines.append(f"- [{edge['from']}] -{edge['type']}-> [{edge['to']}]{extra}")

    return "\n".join(lines)


def format_history(history) -> str:
    if not history:
        return ""
    lines = ["Conversation so far:"]
    for turn in history:
        lines.append(f"- Q: {turn['question']}")
        lines.append(f"  A: {turn['answer']}")
    return "\n".join(lines) + "\n"


class GroundedReasoner:
    def __init__(self, client: OllamaClient | None = None):
        self.client = client or OllamaClient()

    def answer(self, question: str, evidence: Evidence, history=None, intent: str = "") -> str:
        if not evidence.nodes:
            # No evidence to reason over: refuse deterministically rather than
            # risk the model inventing something plausible-sounding. A small
            # model does not reliably follow the "say so plainly" instruction
            # below when there's nothing at all to ground an answer in.
            return _NO_EVIDENCE_ANSWER

        prompt = _PROMPT_TEMPLATE.format(
            diagnosis_block=_DIAGNOSIS_GUIDANCE if intent == "diagnosis" else "",
            history_block=format_history(history),
            evidence=format_evidence(evidence),
            question=question,
        )
        return self.client.generate(prompt, temperature=0.0)

    def diagnose(self, question: str, evidence: Evidence, history=None) -> StructuredDiagnosis:
        """Structured counterpart to answer(), for diagnosis questions
        specifically - see spec section 12: diagnosis/root_cause/
        affected_code/proposed_fix/improved_code/confidence_basis/
        limitations, with VERIFIED/INFERRED/SUGGESTED distinguished in
        confidence_basis rather than left implicit."""
        if not evidence.nodes:
            return StructuredDiagnosis(
                diagnosis=_NO_EVIDENCE_ANSWER,
                limitations="No evidence was retrieved for this question.",
            )

        prompt = _DIAGNOSIS_JSON_PROMPT.format(
            history_block=format_history(history),
            evidence=format_evidence(evidence),
            question=question,
        )
        raw = self.client.generate(prompt, format="json", temperature=0.0)
        return _parse_structured_diagnosis(raw)
