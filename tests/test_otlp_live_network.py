"""The one test that proves the ARBITRARY-repository workflow end to
end over a REAL network socket, not just in-process protobuf decoding:
runs an actual uvicorn server in a background thread, then uses
make_otlp_tracer() + traced() (exactly what an arbitrary separately-run
repository's own code would do) to send a real span over real HTTP to
it, and confirms it lands in the graph correctly linked.
"""

import threading
import time

import pytest
import uvicorn

from codeatlas.api.app import create_app
from codeatlas.graph.models import CodeEntity
from codeatlas.graph.paths import canonical_path_key
from codeatlas.telemetry.tracing import make_otlp_tracer, traced

# Module-level, not nested in the test function: traced() records
# func.__qualname__ as the identity, and a function nested inside a
# test function would have a "test_x.<locals>.traced_add" qualname
# instead of the plain "traced_add" this test seeds an entity for.
def traced_add(a, b):
    return a + b


@pytest.fixture
def running_server(tmp_path):
    app = create_app(str(tmp_path / "graph.db"))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn server did not start in time"

    port = server.servers[0].sockets[0].getsockname()[1]
    yield app, port

    server.should_exit = True
    thread.join(timeout=10)


def test_arbitrary_repo_workflow_over_real_network(running_server):
    app, port = running_server
    endpoint = f"http://127.0.0.1:{port}/v1/traces"

    # Step: the (arbitrary, separately-running) app's own file location -
    # in a real scenario this would be that repo's actual source file.
    filepath = canonical_path_key(__file__)  # this test file stands in for "the repo's own code"

    # Steps 1-2 (from the user's requested workflow): ingest would happen
    # via /api/ingest against the real repo; here we seed the equivalent
    # CodeEntity directly since this test isn't about static analysis.
    app.state.repo.upsert_code_entity(CodeEntity(
        id="test_module.py::traced_add",
        qualified_name="tests.test_otlp_live_network.traced_add",
        name="traced_add",
        kind="function",
        module_path="test_module.py",
        start_line=1,
        end_line=2,
        abs_path=filepath,
        local_qualname="traced_add",
    ))

    # Step 3: run the repo's own workload, in its own process (this test
    # process stands in for that - the key point is it's a real tracer
    # exporting over real HTTP, not GraphSpanExporter writing in-process).
    tracer = make_otlp_tracer(service_name="arbitrary-repo", endpoint=endpoint)
    traced_version = traced(tracer)(traced_add)

    result = traced_version(2, 3)
    assert result == 5

    # Step 4-5: SimpleSpanProcessor exports synchronously within the
    # traced call above, but the receiver's HTTP handling is still async
    # on the server side, so poll briefly rather than assuming it landed
    # in the exact same instant.
    deadline = time.time() + 10
    spans = []
    while time.time() < deadline:
        spans = app.state.repo.spans_for_entity("test_module.py::traced_add")
        if spans:
            break
        time.sleep(0.1)

    assert len(spans) == 1
    assert spans[0]["name"] == "traced_add"

    # Step 6: query the resulting unified graph.
    span_obj = app.state.repo.get_runtime_span(spans[0]["id"])
    assert span_obj.status == "OK"
    assert span_obj.return_value == "5"
