from codeatlas.reasoning.intent_classifier import IntentClassifier, KNOWN_INTENTS


def test_classifies_callers_question(ollama_client):
    classifier = IntentClassifier(ollama_client)
    result = classifier.classify("Who calls the add function?")
    assert result.intent == "callers"
    assert "add" in result.target.lower()


def test_classifies_callees_question(ollama_client):
    classifier = IntentClassifier(ollama_client)
    result = classifier.classify("What does compute call?")
    assert result.intent == "callees"


def test_classifies_recent_errors_question(ollama_client):
    classifier = IntentClassifier(ollama_client)
    result = classifier.classify("What errors happened recently in the system?")
    assert result.intent == "recent_errors"


def test_classifies_runtime_behavior_question(ollama_client):
    classifier = IntentClassifier(ollama_client)
    result = classifier.classify("How long does the run function take to execute at runtime?")
    assert result.intent == "runtime_behavior"


def test_classifies_slow_calls_question(ollama_client):
    classifier = IntentClassifier(ollama_client)
    result = classifier.classify("Show me all slow function calls in the system")
    assert result.intent == "slow_calls"


def test_classifies_recent_activity_question(ollama_client):
    classifier = IntentClassifier(ollama_client)
    result = classifier.classify("Why is there no output?")
    assert result.intent == "recent_activity"


def test_classifies_file_level_whats_wrong_question_as_diagnosis(ollama_client):
    classifier = IntentClassifier(ollama_client)
    result = classifier.classify("what is wrong in tt.py?")
    assert result.intent == "diagnosis"
    assert "tt.py" in result.target


def test_classifies_why_is_this_failing_as_diagnosis(ollama_client):
    classifier = IntentClassifier(ollama_client)
    result = classifier.classify("why is service.py failing?")
    assert result.intent == "diagnosis"


def test_classifies_function_level_crash_question_as_diagnosis(ollama_client):
    classifier = IntentClassifier(ollama_client)
    result = classifier.classify("why does OrderService crash?")
    assert result.intent == "diagnosis"
    assert "orderservice" in result.target.lower()


def test_classifies_where_is_the_bug_as_diagnosis(ollama_client):
    classifier = IntentClassifier(ollama_client)
    result = classifier.classify("where is the bug in payment_service.py?")
    assert result.intent == "diagnosis"


def test_naming_a_file_does_not_force_module_dependencies_when_asking_whats_wrong(ollama_client):
    """The filename is a target constraint, not the intent itself - a
    genuine "what does this file import" question should still classify
    as module_dependencies, but "what's wrong with" the same file must
    not, just because both name a .py file."""
    classifier = IntentClassifier(ollama_client)
    diagnosis_result = classifier.classify("what is wrong with pricing.py?")
    dependency_result = classifier.classify("what modules does pricing.py depend on?")

    assert diagnosis_result.intent == "diagnosis"
    assert dependency_result.intent == "module_dependencies"


def test_history_helps_resolve_follow_up_target(ollama_client):
    classifier = IntentClassifier(ollama_client)
    history = [{
        "question": "Tell me about the add function",
        "answer": "It sums two numbers.",
        "resolved_target": {"kind": "entity", "id": "pkg/math_utils.py::add"},
    }]

    result = classifier.classify("Who calls it?", history=history)

    assert result.intent == "callers"


def test_result_intent_always_in_known_set(ollama_client):
    classifier = IntentClassifier(ollama_client)
    result = classifier.classify("Tell me about the Calculator class")
    assert result.intent in KNOWN_INTENTS
