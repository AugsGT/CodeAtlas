import sys
from pathlib import Path

import pytest

from codeatlas.reasoning.ollama_client import OllamaClient, OllamaError

TEST_MODEL = "qwen2.5-coder:7b"

# This fixture creates an OllamaClient and checks if it can generate text.
@pytest.fixture(scope="session")
def ollama_client():
    client = OllamaClient(model=TEST_MODEL, timeout=180)
    try:
        client.generate("Say OK", temperature=0.0)
    except OllamaError as exc:
        pytest.skip(f"Ollama not reachable, skipping LLM-dependent tests: {exc}")
    return client

# Path to the sample repository and service file.
SAMPLE_REPO = Path(__file__).parent.parent / "sample_repo"
SERVICE_PY = SAMPLE_REPO / "pkg" / "service.py"

# Sample code with a known bug in it.
KNOWN_BUGGY_SERVICE_PY = '''\
from codeatlas.telemetry.tracing import traced
from pkg.tracing_setup import tracer
from pkg.math_utils import add, square


class Calculator:
    @traced(tracer)
    def compute(self, a, b):
        total = add(a, c)
        return square(total)


@traced(tracer)
def run():
    calc = Calculator()
    return calc.compute(2, 3)
'''

# Sample code with no known bugs.
KNOWN_WORKING_SERVICE_PY = '''\
from codeatlas.telemetry.tracing import traced
from pkg.tracing_setup import tracer
from pkg.math_utils import add, square


class Calculator:
    @traced(tracer)
    def compute(self, a, b):
        total = add(a, b)
        return square(total)


@traced(tracer)
def run():
    calc = Calculator()
    return calc.compute(2, 3)
'''

# Sample code with a silent bug (no exception, but missing return).
KNOWN_SILENT_BUG_SERVICE_PY = '''\
from codeatlas.telemetry.tracing import traced
from pkg.tracing_setup import tracer
from pkg.math_utils import add, square


class Calculator:
    @traced(tracer)
    def compute(self, a, b):
        total = add(a, b)
        #return square(total)


@traced(tracer)
def run():
    calc = Calculator()
    return calc.compute(2, 3)
'''

# Fixture to import the service module from the sample repository.
@pytest.fixture
def sample_repo_service(request):
    """Import pkg.service (and pkg.tracing_setup) fresh, optionally
    pinning sample_repo/pkg/service.py to known content first.

    Use plainly for tests that just want *some* real, currently-running
    version of sample_repo. Use with indirect parametrization when a test
    needs a specific scenario:

        @pytest.mark.parametrize("sample_repo_service", [KNOWN_BUGGY_SERVICE_PY], indirect=True)
        def test_x(sample_repo_service):
            service, tracing_setup = sample_repo_service
            ...

    Whatever was actually on disk before the test is always restored
    afterward, whether or not content was pinned.
    """
    content = getattr(request, "param", None)
    original = SERVICE_PY.read_text()
    if content is not None:
        SERVICE_PY.write_text(content)

    sys.path.insert(0, str(SAMPLE_REPO))
    try:
        from pkg import service, tracing_setup  # noqa: PLC0415

        tracing_setup.exporter.clear()
        yield service, tracing_setup
    finally:
        sys.path.remove(str(SAMPLE_REPO))
        for mod_name in list(sys.modules):
            if mod_name == "pkg" or mod_name.startswith("pkg."):
                del sys.modules[mod_name]
        if content is not None:
            SERVICE_PY.write_text(original)

# Fixture to pin the file content of sample_repo/pkg/service.py.
@pytest.fixture
def pinned_service_py_content(request):
    """Like sample_repo_service, but only pins the file content - for
    tests that exercise sample_repo through the API (which imports
    pkg.service internally) rather than importing it directly themselves.
    Requires indirect parametrization: the param is the exact content
    to write.
    """
    original = SERVICE_PY.read_text()
    SERVICE_PY.write_text(request.param)
    try:
        yield
    finally:
        SERVICE_PY.write_text(original)
        for mod_name in list(sys.modules):
            if mod_name == "pkg" or mod_name.startswith("pkg."):
                del sys.modules[mod_name]