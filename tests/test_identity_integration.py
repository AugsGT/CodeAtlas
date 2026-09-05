"""Phase 2 validation: identity resolution against a real, instrumented
Python repository (sample_repo/), not synthetic spans.

Runs actual traced code through the OpenTelemetry SDK, collects the
real emitted spans, seeds the graph with CodeEntity nodes describing
sample_repo's functions (standing in for Phase 3's static analyzer,
which doesn't exist yet), and verifies every span resolves to the
correct entity.
"""

import pytest

from codeatlas.graph.identity import resolve_and_link_span
from codeatlas.graph.models import CodeEntity
from codeatlas.graph.repository import GraphRepository
from codeatlas.telemetry.tracing import to_runtime_span
from tests.conftest import KNOWN_BUGGY_SERVICE_PY


def seed_known_entities(repo):
    known = [
        ("pkg/math_utils.py::add", "add", "function"),
        ("pkg/math_utils.py::square", "square", "function"),
        ("pkg/math_utils.py::a_times_a", "a_times_a", "function"),
        ("pkg/service.py::Calculator.compute", "compute", "method"),
        ("pkg/service.py::run", "run", "function"),
    ]
    for entity_id, name, kind in known:
        module_path = entity_id.split("::")[0]
        repo.upsert_code_entity(CodeEntity(
            id=entity_id,
            qualified_name=entity_id.replace("/", ".").replace(".py::", "."),
            name=name,
            kind=kind,
            module_path=module_path,
            start_line=1,
            end_line=1,
        ))


@pytest.mark.parametrize("sample_repo_service", [KNOWN_BUGGY_SERVICE_PY], indirect=True)
def test_real_instrumented_spans_resolve_to_known_entities(tmp_path, sample_repo_service):
    """pkg/service.py::Calculator.compute is pinned to a known NameError
    for this test (see conftest.py's sample_repo_service docstring — the
    real file is the user's own live sandbox and changes independently
    of this suite). run() and compute() still run far enough to emit
    ERROR-status spans before the NameError propagates; add()/square()/
    a_times_a() are never reached because the exception happens while
    evaluating that call's arguments."""
    service, tracing_setup = sample_repo_service
    repo = GraphRepository(tmp_path / "graph.db")
    seed_known_entities(repo)

    with pytest.raises(NameError):
        service.run()

    finished_spans = tracing_setup.exporter.get_finished_spans()
    assert len(finished_spans) == 2  # run, compute — add/square/a_times_a never reached

    resolved = {}
    for readable_span in finished_spans:
        runtime_span = to_runtime_span(readable_span)
        entity_id = resolve_and_link_span(repo, runtime_span)
        resolved[runtime_span.code_function] = (entity_id, runtime_span.status, runtime_span.error_message)

    assert resolved["run"][0] == "pkg/service.py::run"
    assert resolved["Calculator.compute"][0] == "pkg/service.py::Calculator.compute"
    assert resolved["run"][1] == "ERROR"
    assert resolved["Calculator.compute"][1] == "ERROR"
    assert "NameError" in resolved["Calculator.compute"][2]

    for entity_id, _, _ in resolved.values():
        spans = repo.spans_for_entity(entity_id)
        assert len(spans) == 1

    repo.close()
