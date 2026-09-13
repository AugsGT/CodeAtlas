# This file contains a class that classifies natural-language questions into intents and extracts the entity/module name being asked about.

import json
import re
from dataclasses import dataclass

from .ollama_client import OllamaClient

# Deterministic backstop for the exact failure mode this project was
# originally built to fix: "what is wrong in tt.py" classified as
# module_dependencies instead of diagnosis. Prompt wording alone (the
# _INTENT_DESCRIPTIONS text above) has closed most of that gap, but
# nothing previously stopped the model from picking a structurally
# valid-but-wrong intent for an unambiguously diagnosis-shaped question.
# Deliberately narrow - only the clearest phrasings match, so a
# genuinely different, more nuanced question (e.g. "why does X import Y"
# - about dependencies, not a problem) isn't second-guessed by a coarse
# keyword match. This overrides the model's choice; it never invents a
# target, so an untargeted match still goes through the same no-target
# diagnosis fallback in pipeline.py (-> recent_errors) as before.
_DIAGNOSIS_PHRASE_RE = re.compile(
    r"what'?s wrong|what is wrong|"
    r"why (?:is|does|did|isn'?t|doesn'?t|didn'?t)\b.{0,40}\b"
    r"(?:fail|failing|failed|crash|crashing|crashed|break|breaking|broke|broken|error|bug)|"
    r"what caused|what'?s causing|"
    r"how (?:do i|to) fix|"
    r"where'?s the bug|where is the bug|"
    r"what'?s the bug|what is the bug",
    re.IGNORECASE,
)


def _looks_like_diagnosis_question(question: str) -> bool:
    return bool(_DIAGNOSIS_PHRASE_RE.search(question))

# List of known intents that can be classified
KNOWN_INTENTS = [
    "entity_overview",
    "callers",
    "callees",
    "runtime_behavior",
    "module_dependencies",
    "diagnosis",
    "recent_errors",
    "slow_calls",
    "recent_activity",
]

# Descriptions of each intent
_INTENT_DESCRIPTIONS = """\
- entity_overview: general "what is/tell me about X" questions about a function, method, class, or module
- callers: "who calls X" / "what depends on X" questions
- callees: "what does X call" / "what does X depend on" questions
- runtime_behavior: questions about how one SPECIFIC named function/method/class behaved at \
runtime (its timing, metrics, logs) — always names a target
- module_dependencies: questions about which modules/files a module imports or depends on \
(NOT "what's wrong with" a module - that's diagnosis, even though both name a file)
- diagnosis: "what's wrong with X", "why is X failing/crashing", "what caused this error", \
"how do I fix X", "where is the bug in X" — questions asking to identify or explain a PROBLEM \
with one specific file, function, method, or class. The named file/function is a target \
constraint, not evidence that the question is about dependencies or general structure - a \
filename alone never makes a question module_dependencies if the actual ask is "what's wrong."
- recent_errors: "what's failing", "what broke", "any exceptions", "show me recent errors" \
questions with NO specific target — a search across the whole codebase for logged errors AND \
functions that raised uncaught exceptions
- slow_calls: "what's slow", "what's taking too long overall", "any performance problems", \
"is anything slower than it should be" questions with NO specific target — a search across \
the whole codebase for calls whose recorded duration is unusually high
- recent_activity: "why is there no output", "what happened when it ran", "what did it return", \
"is anything wrong" questions with NO specific target, where the question is NOT specifically \
about errors or slowness — a general survey of the most recent runtime activity (successful or \
not) across the whole codebase"""

# Template for the prompt to be sent to the language model
_PROMPT_TEMPLATE = """You are an intent classifier for a code intelligence tool called CodeAtlas.
Given a question about a codebase, choose exactly one intent from this list:
{intents}

Also extract the target: the specific function, method, class, or module name the question is about \
(just the bare name, e.g. "add" or "Calculator.compute"). If the question refers back to something \
already discussed (e.g. "it", "that function", "its callers", "why does it fail") instead of naming \
something new, use the target from the most recent turn below instead of an empty string. If the \
question names no target and there is no earlier turn to refer back to, use an empty string.
{history_block}
Question: {question}

Respond with only a JSON object: {{"intent": "...", "target": "..."}}"""

# Data class to hold the result of intent classification
@dataclass
class IntentClassification:
    intent: str
    target: str
    raw_response: str

# Function to format conversation history for the prompt
def format_history(history) -> str:
    if not history:
        return ""
    lines = ["\nPrevious turns:"]
    for turn in history:
        target = turn.get("resolved_target") or {}
        lines.append(f"- Q: {turn['question']!r} -> target used: {target.get('id') or '(none)'}")
    return "\n".join(lines) + "\n"

# Class to handle intent classification
class IntentClassifier:
    def __init__(self, client: OllamaClient | None = None):
        self.client = client or OllamaClient()

    # Method to classify a question into an intent and extract the target
    def classify(self, question: str, history=None) -> IntentClassification:
        prompt = _PROMPT_TEMPLATE.format(
            intents=_INTENT_DESCRIPTIONS,
            history_block=format_history(history),
            question=question,
        )
        raw_response = self.client.generate(prompt, format="json")

        intent = "entity_overview"
        target = ""
        try:
            data = json.loads(raw_response)
            if isinstance(data.get("intent"), str) and data["intent"] in KNOWN_INTENTS:
                intent = data["intent"]
            if isinstance(data.get("target"), str):
                target = data["target"].strip()
        except (json.JSONDecodeError, AttributeError):
            pass  # fall through to the safe defaults above

        if intent != "diagnosis" and _looks_like_diagnosis_question(question):
            intent = "diagnosis"

        return IntentClassification(intent=intent, target=target, raw_response=raw_response)