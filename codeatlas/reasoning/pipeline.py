"""Orchestrates the full question-answering flow: classify intent,
resolve the target, retrieve bounded evidence, then reason over it.

Kept thin on purpose — each stage (classifier, resolver, retriever,
reasoner) is independently testable; this just wires them together.

Follow-up questions are supported via an optional `history` list (each
item: {"question": str, "answer": str, "resolved_target": {"kind", "id"}
or None}), passed straight through to this session's caller (the API
is stateless — the client owns conversation state). The intent
classifier sees history so it can resolve references like "it" or
"that function"; as a deterministic backstop, if the classifier
extracts no new target for such a follow-up, the pipeline carries
forward the previous turn's resolved target itself rather than relying
on the model alone.
"""

from dataclasses import dataclass

from ..retrieval.resolve import resolve_target
from ..retrieval.subgraph import Evidence, SubgraphRetriever
from ..validation.validator import ValidationResult, validate, validate_structured_diagnosis
from .intent_classifier import IntentClassification, IntentClassifier
from .ollama_client import OllamaClient
from .reasoner import GroundedReasoner, StructuredDiagnosis, render_diagnosis_as_text

# For these intents, finding literally nothing is itself a real, positive
# answer ("it ran and nothing was wrong") rather than a sign of missing
# data - unlike e.g. recent_activity, where zero spans really does mean
# "nothing has been executed yet." Conflating the two produced a
# confusing "I don't have enough evidence... try ingesting more data"
# refusal for a question like "what are the issues in this repo" even
# when the repo had just run cleanly (the dashboard's Alerts panel
# already got this right via the same underlying query - the Q&A path
# didn't).
_CLEAN_RESULT_MESSAGES = {
    "recent_errors": "No errors or failed calls were found in the current runtime evidence — "
                      "every traced execution completed without a recorded problem.",
    "slow_calls": "No calls exceeded the slowness threshold in the current runtime evidence.",
}

# Which kind of resolved target each intent's retriever method actually
# accepts (SubgraphRetriever.entity_overview/callers/callees/runtime_behavior
# take entity_id; module_dependencies takes module_path; diagnosis takes
# either; the rest take neither). A question can classify as one of these
# while its target resolves to a DIFFERENT kind (e.g. "what is
# run_workload.py" resolving to a module while the model guesses the
# generic "entity_overview" intent) - passing the wrong kind of id straight
# through crashes with a TypeError from the retriever method rejecting an
# argument it doesn't accept.
_ENTITY_INTENTS = frozenset({"entity_overview", "callers", "callees", "runtime_behavior"})
_MODULE_INTENTS = frozenset({"module_dependencies"})
_DUAL_KIND_INTENTS = frozenset({"diagnosis"})  # accepts either an entity or a module target
_NO_TARGET_INTENTS = frozenset({"recent_errors", "slow_calls", "recent_activity"})


@dataclass
class AnswerResult:
    question: str
    classification: IntentClassification
    resolved_target: tuple  # (kind, id) or (None, None)
    evidence: Evidence
    answer: str
    validation: ValidationResult
    # Populated only for the "diagnosis" intent - see reasoner.py's
    # diagnose(). `answer` above still carries a flattened prose
    # rendering of it (render_diagnosis_as_text) for backward-compatible
    # display and for conversation history; this carries the full
    # root_cause/proposed_fix/improved_code/confidence_basis/limitations
    # breakdown for a caller that wants it (e.g. the dashboard's
    # Proposed Fix panel).
    structured_diagnosis: StructuredDiagnosis | None = None


class ReasoningPipeline:
    def __init__(self, repo, client: OllamaClient | None = None):
        client = client or OllamaClient()
        self.repo = repo
        self.retriever = SubgraphRetriever(repo)
        self.classifier = IntentClassifier(client)
        self.reasoner = GroundedReasoner(client)

    def ask(self, question: str, history=None) -> AnswerResult:
        classification = self.classifier.classify(question, history)
        kind, resolved_id = resolve_target(self.repo, classification.target)

        if kind is None and not classification.target and history:
            # The classifier found no new target, meaning it read this as a
            # pure follow-up ("what does it call?") — carry the previous
            # turn's resolved target forward deterministically rather than
            # leaving the question unanswerable.
            last_target = history[-1].get("resolved_target") or {}
            if last_target.get("kind") and last_target.get("id"):
                kind, resolved_id = last_target["kind"], last_target["id"]

        intent = classification.intent
        params = {}
        if intent in _DUAL_KIND_INTENTS and kind in ("entity", "module"):
            params = {"kind": kind, "target_id": resolved_id}
        elif intent in _ENTITY_INTENTS and kind == "entity":
            params["entity_id"] = resolved_id
        elif intent in _MODULE_INTENTS and kind == "module":
            params["module_path"] = resolved_id
        elif kind == "module":
            # The classifier picked an entity-shaped (or no-target) intent,
            # but the name it extracted actually resolved to a Module, not
            # a CodeEntity (e.g. "what's happening in tt.py" naming a file
            # rather than a function). diagnosis is a strict superset of
            # module_dependencies for a module target (same Module node +
            # CONTAINS, plus each contained entity's own runtime evidence),
            # so it's the more informative fallback - in particular it's
            # what actually surfaces Module.parse_error for a file that
            # failed to parse, which a plain module_dependencies or
            # recent_activity survey would either miss or never mention.
            intent = "diagnosis"
            params = {"kind": "module", "target_id": resolved_id}
        elif intent in _DUAL_KIND_INTENTS:
            # A diagnosis question ("why does this fail?", "what caused
            # the error?") with no resolvable target at all. recent_errors
            # is the more capable fallback specifically here - it now
            # includes every critical/high-severity Issue (static parse
            # errors included, not just runtime evidence), so a vague
            # diagnosis question can surface a syntax error even when the
            # repository has never been executed. A real, previously
            # observed gap: this used to fall to recent_activity below,
            # which only surveys RuntimeSpans and so answered "I don't
            # have enough evidence" for a repo whose only problem was a
            # file that didn't even parse.
            intent = "recent_errors"
        elif intent not in _NO_TARGET_INTENTS:
            # Either nothing resolved, or it resolved to an entity but this
            # intent needs something else - no usable target either way.
            # Fall back to a general survey of recent activity rather than
            # crashing or answering nothing - it's a superset of
            # recent_errors (every error is also recent activity), so it's
            # strictly more informative as a fallback.
            intent = "recent_activity"

        evidence = self.retriever.retrieve(intent, **params)

        structured_diagnosis = None
        clean_message = _CLEAN_RESULT_MESSAGES.get(intent)
        if intent == "diagnosis":
            # A resolved, specific-target diagnosis question gets the
            # full structured breakdown (root cause, proposed fix,
            # confidence basis) rather than plain prose - see spec
            # section 12. The no-target diagnosis fallback above already
            # rewrites intent to recent_errors before this point, so this
            # only ever fires for an actual named target, where a single
            # root-cause/fix narrative makes sense.
            structured_diagnosis = self.reasoner.diagnose(question, evidence, history)
            answer = render_diagnosis_as_text(structured_diagnosis)
            validation = validate_structured_diagnosis(structured_diagnosis, evidence)
        elif not evidence.nodes and clean_message and self.repo.has_any_runtime_span():
            answer = clean_message
            validation = validate(answer, evidence)
        else:
            answer = self.reasoner.answer(question, evidence, history, intent=intent)
            validation = validate(answer, evidence)

        return AnswerResult(
            question=question,
            classification=classification,
            resolved_target=(kind, resolved_id),
            evidence=evidence,
            answer=answer,
            validation=validation,
            structured_diagnosis=structured_diagnosis,
        )
