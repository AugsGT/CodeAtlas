"""Unit tests for the multi-signal identity resolver (graph/identity.py).

These intentionally exercise the exact architectural weakness the
dashboard's sample_repo bug exposed: resolution must not depend on the
caller supplying a "repo root" that happens to match what static
analysis used. Instead it matches on qualified name and/or canonical
file path, both computed independently by each side.
"""

import datetime

from codeatlas.graph.identity import resolve_and_link_span, resolve_code_identity
from codeatlas.graph.models import CodeEntity, RuntimeSpan
from codeatlas.graph.repository import GraphRepository


def make_span(**overrides):
    now = datetime.datetime.now(datetime.timezone.utc)
    defaults = dict(
        id="span1",
        trace_id="trace1",
        name="add",
        start_time=now,
        end_time=now,
        duration_ms=1.0,
        status="OK",
        code_filepath="C:\\repo\\pkg\\math_utils.py",
        code_namespace="pkg.math_utils",
        code_function="add",
        code_lineno=1,
    )
    defaults.update(overrides)
    return RuntimeSpan(**defaults)


def make_entity(**overrides):
    defaults = dict(
        id="pkg/math_utils.py::add",
        qualified_name="pkg.math_utils.add",
        name="add",
        kind="function",
        module_path="pkg/math_utils.py",
        start_line=1,
        end_line=2,
        abs_path="c:/repo/pkg/math_utils.py",
        local_qualname="add",
    )
    defaults.update(overrides)
    return CodeEntity(**defaults)


def make_repo(tmp_path):
    return GraphRepository(tmp_path / "graph.db")


def test_resolves_via_qualified_name_when_namespace_matches(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_code_entity(make_entity())

    entity_id = resolve_code_identity(
        repo, "C:\\repo\\pkg\\math_utils.py", "add", code_namespace="pkg.math_utils"
    )

    assert entity_id == "pkg/math_utils.py::add"
    repo.close()


def test_resolves_via_path_fallback_when_namespace_does_not_match(tmp_path):
    """This is the exact shape of the sample_repo bug: the static entity's
    qualified_name was computed with a namespace that doesn't match the
    runtime's module namespace (e.g. ingested from a different directory
    than the one on sys.path at execution time), but the underlying file
    is the same, so canonical-path + local-qualname still finds it."""
    repo = make_repo(tmp_path)
    repo.upsert_code_entity(make_entity(
        qualified_name="math_utils.add",  # namespace missing the "pkg." prefix
    ))

    entity_id = resolve_code_identity(
        repo, "C:\\repo\\pkg\\math_utils.py", "add", code_namespace="pkg.math_utils"
    )

    assert entity_id == "pkg/math_utils.py::add"
    repo.close()


def test_resolves_when_no_namespace_given(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_code_entity(make_entity())

    entity_id = resolve_code_identity(repo, "C:\\repo\\pkg\\math_utils.py", "add")

    assert entity_id == "pkg/math_utils.py::add"
    repo.close()


def test_returns_none_without_code_attributes(tmp_path):
    repo = make_repo(tmp_path)
    assert resolve_code_identity(repo, "", "") is None
    repo.close()


def test_returns_none_when_nothing_matches(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_code_entity(make_entity())

    entity_id = resolve_code_identity(repo, "C:\\repo\\pkg\\other.py", "does_not_exist")

    assert entity_id is None
    repo.close()


def test_matches_method_qualname(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_code_entity(make_entity(
        id="pkg/service.py::Calculator.compute",
        qualified_name="pkg.service.Calculator.compute",
        name="compute",
        kind="method",
        module_path="pkg/service.py",
        abs_path="c:/repo/pkg/service.py",
        local_qualname="Calculator.compute",
    ))

    entity_id = resolve_code_identity(
        repo, "C:\\repo\\pkg\\service.py", "Calculator.compute", code_namespace="pkg.service"
    )

    assert entity_id == "pkg/service.py::Calculator.compute"
    repo.close()


def test_cross_platform_path_formats_match(tmp_path):
    """A Windows-style path (backslashes, uppercase drive) recorded by a
    span must match an entity whose canonical path was computed from a
    POSIX-style path — this is what canonical_path_key() exists for."""
    repo = make_repo(tmp_path)
    repo.upsert_code_entity(make_entity(abs_path="c:/repo/pkg/math_utils.py"))

    entity_id = resolve_code_identity(repo, "C:\\Repo\\PKG\\Math_Utils.py", "add")

    assert entity_id == "pkg/math_utils.py::add"
    repo.close()


def test_ambiguous_qualified_name_falls_back_to_path_agreement(tmp_path):
    """Two entities share a qualified name (e.g. two ingested copies of
    the same repo) - only the one whose path also agrees should resolve."""
    repo = make_repo(tmp_path)
    repo.upsert_code_entity(make_entity(id="copy-a::add", abs_path="c:/repo-a/pkg/math_utils.py"))
    repo.upsert_code_entity(make_entity(id="copy-b::add", abs_path="c:/repo-b/pkg/math_utils.py"))

    entity_id = resolve_code_identity(
        repo, "C:\\repo-b\\pkg\\math_utils.py", "add", code_namespace="pkg.math_utils"
    )

    assert entity_id == "copy-b::add"
    repo.close()


def test_fully_ambiguous_match_returns_none_rather_than_guessing(tmp_path):
    """Same qualified name, same path, genuinely indistinguishable (e.g.
    the exact same file ingested twice under two different entity ids) -
    must not guess."""
    repo = make_repo(tmp_path)
    repo.upsert_code_entity(make_entity(id="entity-1"))
    repo.upsert_code_entity(make_entity(id="entity-2"))

    entity_id = resolve_code_identity(
        repo, "C:\\repo\\pkg\\math_utils.py", "add", code_namespace="pkg.math_utils"
    )

    assert entity_id is None
    repo.close()


def test_line_number_breaks_tie_between_ambiguous_path_matches(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_code_entity(make_entity(id="entity-1", start_line=1, end_line=2))
    repo.upsert_code_entity(make_entity(id="entity-2", start_line=10, end_line=12))

    entity_id = resolve_code_identity(
        repo, "C:\\repo\\pkg\\math_utils.py", "add", code_namespace="pkg.math_utils", code_lineno=11
    )

    assert entity_id == "entity-2"
    repo.close()


def test_resolve_and_link_creates_produces_edge_for_known_entity(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_code_entity(make_entity())
    span = make_span()

    entity_id = resolve_and_link_span(repo, span)

    assert entity_id == "pkg/math_utils.py::add"
    assert repo.get_runtime_span("span1") is not None
    assert repo.spans_for_entity("pkg/math_utils.py::add") == [
        {"id": "span1", "name": "add", "duration_ms": 1.0}
    ]
    repo.close()


def test_resolve_and_link_stores_unresolved_span_without_edge(tmp_path):
    repo = make_repo(tmp_path)
    span = make_span(code_function="does_not_exist")

    entity_id = resolve_and_link_span(repo, span)

    assert entity_id is None
    assert repo.get_runtime_span("span1") is not None
    repo.close()
