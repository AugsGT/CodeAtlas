from pathlib import Path

import pytest

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from tests.conftest import KNOWN_WORKING_SERVICE_PY

SAMPLE_REPO = Path(__file__).parent.parent / "sample_repo"
SERVICE_PY = SAMPLE_REPO / "pkg" / "service.py"


@pytest.fixture(autouse=True)
def pinned_service_py():
    """sample_repo/pkg/service.py is the user's own live sandbox and
    changes independently of this suite (see conftest.py). Every test in
    this file analyzes the real sample_repo directory, so all of them
    need its content pinned to a known state, not autouse-conditionally
    for just one test."""
    original = SERVICE_PY.read_text()
    SERVICE_PY.write_text(KNOWN_WORKING_SERVICE_PY)
    try:
        yield
    finally:
        SERVICE_PY.write_text(original)


def analyze():
    return PythonAstAnalyzer().analyze(str(SAMPLE_REPO))


def test_discovers_modules():
    result = analyze()
    paths = {m.path for m in result.modules}
    assert paths == {
        "pkg/__init__.py",
        "pkg/math_utils.py",
        "pkg/service.py",
        "pkg/tracing_setup.py",
        "run_workload.py",
    }


def test_discovers_functions_methods_and_classes():
    result = analyze()
    entities = {(e.id, e.kind) for e in result.entities}
    assert entities == {
        ("pkg/math_utils.py::add", "function"),
        ("pkg/math_utils.py::square", "function"),
        ("pkg/math_utils.py::a_times_a", "function"),
        ("pkg/service.py::Calculator", "class"),
        ("pkg/service.py::Calculator.compute", "method"),
        ("pkg/service.py::run", "function"),
    }


def test_entity_qualified_names():
    result = analyze()
    by_id = {e.id: e for e in result.entities}
    assert by_id["pkg/math_utils.py::add"].qualified_name == "pkg.math_utils.add"
    assert by_id["pkg/service.py::Calculator.compute"].qualified_name == "pkg.service.Calculator.compute"


def test_contains_links_every_entity_to_its_module():
    result = analyze()
    assert set(result.contains) == {
        ("pkg/math_utils.py", "pkg/math_utils.py::add"),
        ("pkg/math_utils.py", "pkg/math_utils.py::square"),
        ("pkg/math_utils.py", "pkg/math_utils.py::a_times_a"),
        ("pkg/service.py", "pkg/service.py::Calculator"),
        ("pkg/service.py", "pkg/service.py::Calculator.compute"),
        ("pkg/service.py", "pkg/service.py::run"),
    }


def test_resolves_same_module_call():
    result = analyze()
    calls = {(c.caller_id, c.callee_id) for c in result.calls}
    assert ("pkg/math_utils.py::square", "pkg/math_utils.py::a_times_a") in calls


def test_resolves_calls_to_imported_functions():
    result = analyze()
    calls = {(c.caller_id, c.callee_id) for c in result.calls}
    assert ("pkg/service.py::Calculator.compute", "pkg/math_utils.py::add") in calls
    assert ("pkg/service.py::Calculator.compute", "pkg/math_utils.py::square") in calls


def test_resolves_constructor_call_and_local_var_method_call():
    result = analyze()
    calls = {(c.caller_id, c.callee_id) for c in result.calls}
    assert ("pkg/service.py::run", "pkg/service.py::Calculator") in calls
    assert ("pkg/service.py::run", "pkg/service.py::Calculator.compute") in calls


def test_no_extraneous_calls():
    result = analyze()
    assert len(result.calls) == 5


def test_depends_on_resolves_intra_repo_imports_only():
    result = analyze()
    assert set(result.depends_on) == {
        ("pkg/math_utils.py", "pkg/tracing_setup.py"),
        ("pkg/service.py", "pkg/tracing_setup.py"),
        ("pkg/service.py", "pkg/math_utils.py"),
        ("run_workload.py", "pkg/service.py"),
    }
