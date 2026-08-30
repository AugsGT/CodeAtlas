# This file contains the main logic for handling a user's question and providing an answer. It orchestrates several stages:
# 1. Classifying the intent of the question.
# 2. Resolving the target entity or module based on the intent.
# 3. Retrieving evidence related to the resolved target.
# 4. Reasoning over the retrieved evidence to generate a structured diagnosis or answer.

from dataclasses import dataclass

from ..retrieval.resolve import resolve_target
from ..retrieval.subgraph import Evidence, SubgraphRetriever
from ..validation.validator import ValidationResult, validate, validate_structured_diagnosis
from .intent_classifier import IntentClassification, IntentClassifier
from .ollama_client import OllamaClient
from .reasoner import GroundedReasoner, StructuredDiagnosis, render_diagnosis_as_text

# Messages for clean results where no issues are found.
_CLEAN_RESULT_MESSAGES = {
    "recent_errors": "No errors or failed calls were found in the current runtime evidence — every traced execution completed without a recorded problem.",
    "slow_calls": "No calls exceeded the slowness threshold in the current runtime evidence.",
}

# Sets of intents that accept different types of targets.
_ENTITY_INTENTS = frozenset({"entity_overview", "callers", "callees", "runtime_behavior"})
_MODULE_INTENTS = frozenset({"module_dependencies"})
_DUAL_KIND_INTENTS = frozenset({"diagnosis"})  # Accepts either an entity or a module target
_NO_TARGET_INTENTS = frozenset({"recent_errors", "slow_calls", "recent_activity"})


@dataclass
class AnswerResult:
    question: str
    classification: IntentClassification
    resolved_target: tuple  # (kind, id) or (None, None)
    evidence: Evidence
    answer: str
    validation: ValidationResult
    structured_diagnosis: StructuredDiagnosis | None = None


class ReasoningPipeline:
    def __init__(self, repo, client: OllamaClient | None = None):
        # Initialize the pipeline with a repository and an optional client.
        client = client or OllamaClient()
        self.repo = repo
        self.retriever = SubgraphRetriever(repo)
        self.classifier = IntentClassifier(client)
        self.reasoner = GroundedReasoner(client)

    def ask(self, question: str, history=None) -> AnswerResult:
        # Classify the intent of the user's question.
        classification = self.classifier.classify(question, history)
        kind, resolved_id = resolve_target(self.repo, classification.target)

        if kind is None and not classification.target and history:
            # If no new target is found, carry forward the previous turn's resolved target.
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
            # Fallback to diagnosis if the classifier picked an entity-shaped intent but the name resolves to a Module.
            intent = "diagnosis"
            params = {"kind": "module", "target_id": resolved_id}
        elif intent in _DUAL_KIND_INTENTS:
            # Fallback to recent_errors for diagnosis questions with no resolvable target.
            intent = "recent_errors"
        elif intent not in _NO_TARGET_INTENTS:
            # Fallback to recent_activity if nothing resolves or the target is unusable.
            intent = "recent_activity"

        evidence = self.retriever.retrieve(intent, **params)

        structured_diagnosis = None
        clean_message = _CLEAN_RESULT_MESSAGES.get(intent)
        if intent == "diagnosis":
            # Generate a structured diagnosis for resolved questions.
            structured_diagnosis = self.reasoner.diagnose(question, evidence, history)
            answer = render_diagnosis_as_text(structured_diagnosis)
            validation = validate_structured_diagnosis(structured_diagnosis, evidence)
        elif not evidence.nodes and clean_message and self.repo.has_any_runtime_span():
            # Provide a clean message if no evidence is found.
            answer = clean_message
            validation = validate(answer, evidence)
        else:
            # Generate an answer based on the retrieved evidence.
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