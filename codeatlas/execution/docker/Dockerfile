# Sandbox image for automatic execution of an arbitrary ingested Python
# repository. Built with the CodeAtlas project root as the build context
# (see sandbox.py's IMAGE_NAME/build invocation) so it can install the
# codeatlas package itself for the tracer/bootstrap.
#
# This image runs the TARGET repository, never anything CodeAtlas
# considers trusted - see execution/sandbox.py for the resource limits,
# network isolation, and read-only mounts applied at `docker run` time.
# The image alone is not the security boundary; those run-time flags are.
FROM python:3.12-slim

# opentelemetry-instrumentation-* packages are the "OpenTelemetry
# instrumentation where appropriate" layer: if the traced repository
# uses one of these libraries, its instrumentor auto-enables via the
# opentelemetry_instrumentor entry point group (see bootstrap.py's
# _enable_otel_auto_instrumentation) for higher-level HTTP/DB spans on
# top of the universal settrace-based function tracing. Absent here,
# tracing still works via settrace alone.
COPY pyproject.toml /opt/codeatlas/pyproject.toml
COPY codeatlas /opt/codeatlas/codeatlas
RUN pip install --no-cache-dir /opt/codeatlas \
    opentelemetry-instrumentation \
    opentelemetry-instrumentation-requests \
    opentelemetry-instrumentation-urllib3 \
    opentelemetry-instrumentation-sqlite3 \
    opentelemetry-instrumentation-logging

WORKDIR /workspace
ENTRYPOINT ["python", "-m", "codeatlas.execution.bootstrap"]
