"""GraphSpanExporter exercised directly against real finished spans from
the instrumented sample_repo (functions there are bound to their own
fixed tracer/exporter at decoration time, so this drives the exporter
with real ReadableSpan objects rather than re-wiring a new provider)."""

import pytest

from codeatlas.graph.models import CodeEntity
from codeatlas.graph.repository import GraphRepository
from codeatlas.telemetry.tracing import GraphSpanExporter
from tests.conftest import KNOWN_BUGGY_SERVICE_PY


@pytest.mark.parametrize("sample_repo_service", [KNOWN_BUGGY_SERVICE_PY], indirect=True)
def test_graph_span_exporter_links_real_spans_to_entities(tmp_path, sample_repo_service):
    """pkg/service.py::Calculator.compute is pinned to a known NameError
    for this test (see conftest.py's sample_repo_service docstring), so
    this exercises GraphSpanExporter against a real ERROR-status span
    rather than a successful one."""
    service, tracing_setup = sample_repo_service
    repo = GraphRepository(tmp_path / "graph.db")
    repo.upsert_code_entity(CodeEntity(
        id="pkg/service.py::Calculator.compute",
        qualified_name="pkg.service.Calculator.compute",
        name="compute",
        kind="method",
        module_path="pkg/service.py",
        start_line=1,
        end_line=2,
    ))

    try:
        service.run()
    except NameError:
        pass
    finished_spans = tracing_setup.exporter.get_finished_spans()

    exporter = GraphSpanExporter(repo)
    exporter.export(finished_spans)

    spans = repo.spans_for_entity("pkg/service.py::Calculator.compute")
    assert len(spans) == 1
    assert spans[0]["name"] == "Calculator.compute"

    span = repo.get_runtime_span(spans[0]["id"])
    assert span.status == "ERROR"
    assert "NameError" in span.error_message
    repo.close()
