"""Regression test for the real dashboard bug that motivated this
architecture rework: a user ingested sample_repo/pkg (the wrong
directory - a subfolder, not the repo root) while the actually-running
instrumented code had sample_repo itself on sys.path. That produced
CodeEntity ids/qualified_names with a different namespace ("service.py"
instead of "pkg/service.py", "service.Calculator.compute" instead of
"pkg.service.Calculator.compute") than what the runtime spans reported,
and the old repo_root-string-matching resolver silently failed to link
anything.

This reproduces that exact mismatch with the real sample_repo and
proves resolution still succeeds - via the entity's canonical absolute
file path, which doesn't care what directory was chosen as the
analysis root - with no user having to "select the right parent
directory."
"""

from pathlib import Path

import pytest

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.repository import GraphRepository
from codeatlas.telemetry.tracing import GraphSpanExporter
from tests.conftest import KNOWN_BUGGY_SERVICE_PY

SAMPLE_REPO = Path(__file__).parent.parent / "sample_repo"
SAMPLE_REPO_PKG_SUBDIR = SAMPLE_REPO / "pkg"


@pytest.mark.parametrize("sample_repo_service", [KNOWN_BUGGY_SERVICE_PY], indirect=True)
def test_mismatched_ingest_root_still_resolves_via_canonical_path(tmp_path, sample_repo_service):
    """pkg/service.py is pinned to a known NameError for this test (see
    conftest.py's sample_repo_service docstring)."""
    service, tracing_setup = sample_repo_service
    repo = GraphRepository(tmp_path / "graph.db")

    # Ingest with the WRONG root: pkg/ itself, not its parent sample_repo.
    analysis_result = PythonAstAnalyzer().analyze(str(SAMPLE_REPO_PKG_SUBDIR))
    write_analysis_result(repo, analysis_result)

    # Confirm the mismatch is real: this entity's id/qualified_name lack
    # the "pkg." prefix the actually-running code's __module__ will have.
    mismatched_entity = repo.get_code_entity("service.py::Calculator.compute")
    assert mismatched_entity is not None
    assert mismatched_entity.qualified_name == "service.Calculator.compute"

    # Run the real instrumented code - sys.path points at the TRUE
    # parent (sample_repo), so its actual __module__ is "pkg.service",
    # not "service" as the mis-rooted ingest produced.
    with pytest.raises(NameError):
        service.run()
    finished_spans = tracing_setup.exporter.get_finished_spans()
    assert len(finished_spans) == 2  # run, compute

    GraphSpanExporter(repo).export(finished_spans)

    # Despite the namespace mismatch, both spans must still link - via
    # each entity's canonical absolute file path, not a string match on
    # a repo-relative id computed from whichever root someone chose.
    compute_spans = repo.spans_for_entity("service.py::Calculator.compute")
    assert len(compute_spans) == 1
    assert compute_spans[0]["name"] == "Calculator.compute"

    run_entity = repo.get_code_entity("service.py::run")
    assert run_entity is not None
    run_spans = repo.spans_for_entity("service.py::run")
    assert len(run_spans) == 1
    assert run_spans[0]["name"] == "run"

    repo.close()


@pytest.mark.parametrize("sample_repo_service", [KNOWN_BUGGY_SERVICE_PY], indirect=True)
def test_correctly_matched_ingest_root_also_still_works(tmp_path, sample_repo_service):
    """Sanity check: the common, correctly-rooted case (ingest sample_repo
    itself) must keep working exactly as before - this rework must not
    have traded one working case for another."""
    service, tracing_setup = sample_repo_service
    repo = GraphRepository(tmp_path / "graph.db")

    analysis_result = PythonAstAnalyzer().analyze(str(SAMPLE_REPO))
    write_analysis_result(repo, analysis_result)

    with pytest.raises(NameError):
        service.run()
    finished_spans = tracing_setup.exporter.get_finished_spans()

    GraphSpanExporter(repo).export(finished_spans)

    compute_spans = repo.spans_for_entity("pkg/service.py::Calculator.compute")
    assert len(compute_spans) == 1
    repo.close()
