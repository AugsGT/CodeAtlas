import datetime

from codeatlas.graph.models import CodeEntity, LogEntry, Metric, Module, RuntimeSpan
from codeatlas.graph.repository import GraphRepository


def make_repo(tmp_path):
    return GraphRepository(tmp_path / "graph.db")


def test_schema_creates_without_error(tmp_path):
    repo = make_repo(tmp_path)
    repo.close()


def test_module_upsert_and_get(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_module(Module(path="pkg/math_utils.py", name="math_utils", language="python"))

    result = repo.get_module("pkg/math_utils.py")

    assert result == Module(path="pkg/math_utils.py", name="math_utils", language="python")
    repo.close()


def test_module_upsert_is_idempotent_and_updates(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_module(Module(path="pkg/a.py", name="a", language="python"))
    repo.upsert_module(Module(path="pkg/a.py", name="a_renamed", language="python"))

    result = repo.get_module("pkg/a.py")

    assert result.name == "a_renamed"
    repo.close()


def test_get_module_missing_returns_none(tmp_path):
    repo = make_repo(tmp_path)
    assert repo.get_module("does/not/exist.py") is None
    repo.close()


def make_entity(**overrides):
    defaults = dict(
        id="pkg/math_utils.py::add",
        qualified_name="pkg.math_utils.add",
        name="add",
        kind="function",
        module_path="pkg/math_utils.py",
        start_line=1,
        end_line=2,
    )
    defaults.update(overrides)
    return CodeEntity(**defaults)


def test_code_entity_upsert_and_get(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_code_entity(make_entity())

    result = repo.get_code_entity("pkg/math_utils.py::add")

    assert result == make_entity()
    repo.close()


def test_contains_edge_links_module_to_entity(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_module(Module(path="pkg/math_utils.py", name="math_utils", language="python"))
    repo.upsert_code_entity(make_entity())

    repo.add_contains("pkg/math_utils.py", "pkg/math_utils.py::add")

    entities = repo.entities_in_module("pkg/math_utils.py")
    assert entities == [{"id": "pkg/math_utils.py::add", "name": "add", "kind": "function"}]
    repo.close()


def test_calls_edge_tracks_caller_and_callee(tmp_path):
    repo = make_repo(tmp_path)
    for name in ("square", "a_times_a"):
        repo.upsert_code_entity(make_entity(
            id=f"pkg/math_utils.py::{name}",
            qualified_name=f"pkg.math_utils.{name}",
            name=name,
        ))

    repo.add_calls("pkg/math_utils.py::square", "pkg/math_utils.py::a_times_a", call_line=6)

    callees = repo.callees_of("pkg/math_utils.py::square")
    callers = repo.callers_of("pkg/math_utils.py::a_times_a")
    assert callees == [{"callee_id": "pkg/math_utils.py::a_times_a", "call_line": 6}]
    assert callers == [{"caller_id": "pkg/math_utils.py::square", "call_line": 6}]
    repo.close()


def test_depends_on_edge_links_modules(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_module(Module(path="pkg/service.py", name="service", language="python"))
    repo.upsert_module(Module(path="pkg/math_utils.py", name="math_utils", language="python"))

    repo.add_depends_on("pkg/service.py", "pkg/math_utils.py")

    rows = repo.query(
        "MATCH (a:Module)-[:DEPENDS_ON]->(b:Module) RETURN a.path, b.path"
    )
    assert rows == [["pkg/service.py", "pkg/math_utils.py"]]
    repo.close()


def test_clear_runtime_telemetry_removes_spans_metrics_and_logs_only(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_code_entity(make_entity())
    repo.upsert_module(Module(path="pkg/math_utils.py", name="math_utils", language="python"))
    now = datetime.datetime.now(datetime.timezone.utc)

    repo.upsert_runtime_span(RuntimeSpan(
        id="span1", trace_id="t1", name="add", start_time=now, end_time=now,
        duration_ms=1.0, status="OK",
    ))
    repo.add_produces("pkg/math_utils.py::add", "span1")
    repo.upsert_metric(Metric(id="metric1", name="calls", value=1.0, unit="", timestamp=now))
    repo.add_records("pkg/math_utils.py::add", "metric1")
    repo.upsert_log_entry(LogEntry(id="log1", message="hi", level="INFO", timestamp=now))
    repo.add_emits("span1", "log1")

    repo.clear_runtime_telemetry()

    assert repo.get_runtime_span("span1") is None
    assert repo.get_metric("metric1") is None
    assert repo.get_log_entry("log1") is None
    # Static data is untouched.
    assert repo.get_code_entity("pkg/math_utils.py::add") is not None
    assert repo.get_module("pkg/math_utils.py") is not None
    repo.close()


def test_clear_static_graph_removes_modules_and_entities_only(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_code_entity(make_entity())
    repo.upsert_module(Module(path="pkg/math_utils.py", name="math_utils", language="python"))
    repo.add_contains("pkg/math_utils.py", "pkg/math_utils.py::add")
    now = datetime.datetime.now(datetime.timezone.utc)
    repo.upsert_runtime_span(RuntimeSpan(
        id="span1", trace_id="t1", name="add", start_time=now, end_time=now,
        duration_ms=1.0, status="OK",
    ))
    repo.add_produces("pkg/math_utils.py::add", "span1")

    repo.clear_static_graph()

    assert repo.get_code_entity("pkg/math_utils.py::add") is None
    assert repo.get_module("pkg/math_utils.py") is None
    # Runtime telemetry is untouched by this call alone - callers that want
    # a full reset (e.g. /api/ingest) call clear_runtime_telemetry() too.
    assert repo.get_runtime_span("span1") is not None
    repo.close()
