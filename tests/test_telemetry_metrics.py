from opentelemetry.sdk.metrics import MeterProvider

from codeatlas.graph.models import CodeEntity
from codeatlas.graph.paths import canonical_path_key
from codeatlas.graph.repository import GraphRepository
from codeatlas.telemetry.metrics import GraphMetricExporter, to_metrics
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader


def test_to_metrics_converts_sum_data_points():
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("test")
    counter = meter.create_counter("calls_total")
    counter.add(1, {"code.filepath": "/repo/pkg/math_utils.py", "code.function": "add"})
    counter.add(2, {"code.filepath": "/repo/pkg/math_utils.py", "code.function": "add"})

    metrics = to_metrics(reader.get_metrics_data())

    assert len(metrics) == 1
    assert metrics[0].name == "calls_total"
    assert metrics[0].value == 3
    assert metrics[0].code_filepath == "/repo/pkg/math_utils.py"
    assert metrics[0].code_function == "add"


def test_to_metrics_converts_histogram_sum():
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("test")
    hist = meter.create_histogram("duration_ms")
    hist.record(5.0, {"code.function": "run"})
    hist.record(7.0, {"code.function": "run"})

    metrics = to_metrics(reader.get_metrics_data())

    assert len(metrics) == 1
    assert metrics[0].value == 12.0  # sum of recorded values


def test_graph_metric_exporter_links_metric_to_known_entity(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    repo.upsert_code_entity(CodeEntity(
        id="pkg/math_utils.py::add",
        qualified_name="pkg.math_utils.add",
        name="add",
        kind="function",
        module_path="pkg/math_utils.py",
        start_line=1,
        end_line=2,
        abs_path=canonical_path_key("/repo/pkg/math_utils.py"),
        local_qualname="add",
    ))

    exporter = GraphMetricExporter(repo)
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=60_000)
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("test")
    counter = meter.create_counter("calls_total")
    counter.add(1, {"code.filepath": "/repo/pkg/math_utils.py", "code.function": "add"})

    provider.force_flush()

    metrics = repo.metrics_for_entity("pkg/math_utils.py::add")
    provider.shutdown()
    assert len(metrics) == 1
    assert metrics[0]["name"] == "calls_total"
    assert metrics[0]["value"] == 1
    repo.close()


def test_graph_metric_exporter_stores_unresolved_metric(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    exporter = GraphMetricExporter(repo)
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=60_000)
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("test")
    counter = meter.create_counter("unlinked_total")
    counter.add(1)

    provider.force_flush()

    rows = repo.query("MATCH (m:Metric {name: 'unlinked_total'}) RETURN m.id")
    provider.shutdown()
    assert len(rows) == 1
    repo.close()
