"""Kuzu graph schema for CodeAtlas.

The schema is applied incrementally, one build phase at a time, rather
than declared all at once.

Phase 1 defined the static-analysis side of the graph: Module and
CodeEntity nodes, and the CONTAINS, CALLS, and DEPENDS_ON relationships
between them.

Phase 2 adds the runtime-identity side: RuntimeSpan nodes and the
PRODUCES relationship, which links a CodeEntity to the spans emitted
by its executions.

Phase 4 adds Metric and LogEntry nodes: RECORDS links a CodeEntity to
a metric data point recorded from it, LOGS links a CodeEntity to a log
entry resolved via its own code.* attributes, and EMITS links a
RuntimeSpan to a log entry emitted during that span (the standard
OpenTelemetry trace/log correlation, preferred over LOGS when both are
available).

CodeEntity's abs_path/local_qualname columns (added after initial
launch) let runtime identity resolution match a span/metric/log back to
its CodeEntity without needing to know which directory was treated as
the analysis "repo root" — see graph/identity.py.

Issue is the unified representation of "something is wrong", added
after real dashboard use showed that scattering issue facts across
Module.parse_error/RuntimeSpan.status/LogEntry.level (each read by a
different consumer) meant a static-only problem (a syntax error) was
structurally invisible to the alert system, which only ever queried
runtime evidence. FOUND_IN/AFFECTS/EVIDENCED_BY_SPAN/EVIDENCED_BY_LOG
connect an Issue back to whatever it's about — see graph/issues.py.
"""

NODE_TABLES = {
    "Module": """
        CREATE NODE TABLE IF NOT EXISTS Module(
            path STRING,
            name STRING,
            language STRING,
            parse_error STRING,
            abs_path STRING,
            PRIMARY KEY (path)
        )
    """,
    "CodeEntity": """
        CREATE NODE TABLE IF NOT EXISTS CodeEntity(
            id STRING,
            qualified_name STRING,
            name STRING,
            kind STRING,
            module_path STRING,
            start_line INT64,
            end_line INT64,
            abs_path STRING,
            local_qualname STRING,
            PRIMARY KEY (id)
        )
    """,
    "RuntimeSpan": """
        CREATE NODE TABLE IF NOT EXISTS RuntimeSpan(
            id STRING,
            trace_id STRING,
            name STRING,
            start_time TIMESTAMP,
            end_time TIMESTAMP,
            duration_ms DOUBLE,
            status STRING,
            error_message STRING,
            return_value STRING,
            code_filepath STRING,
            code_namespace STRING,
            code_function STRING,
            code_lineno INT64,
            PRIMARY KEY (id)
        )
    """,
    "Metric": """
        CREATE NODE TABLE IF NOT EXISTS Metric(
            id STRING,
            name STRING,
            value DOUBLE,
            unit STRING,
            timestamp TIMESTAMP,
            code_filepath STRING,
            code_namespace STRING,
            code_function STRING,
            code_lineno INT64,
            PRIMARY KEY (id)
        )
    """,
    "LogEntry": """
        CREATE NODE TABLE IF NOT EXISTS LogEntry(
            id STRING,
            message STRING,
            level STRING,
            timestamp TIMESTAMP,
            trace_id STRING,
            span_id STRING,
            code_filepath STRING,
            code_namespace STRING,
            code_function STRING,
            code_lineno INT64,
            PRIMARY KEY (id)
        )
    """,
    "Issue": """
        CREATE NODE TABLE IF NOT EXISTS Issue(
            id STRING,
            type STRING,
            severity STRING,
            detection_method STRING,
            message STRING,
            file STRING,
            line INT64,
            status STRING,
            PRIMARY KEY (id)
        )
    """,
}

REL_TABLES = {
    "CONTAINS": """
        CREATE REL TABLE IF NOT EXISTS CONTAINS(
            FROM Module TO CodeEntity
        )
    """,
    "CALLS": """
        CREATE REL TABLE IF NOT EXISTS CALLS(
            FROM CodeEntity TO CodeEntity,
            call_line INT64
        )
    """,
    "DEPENDS_ON": """
        CREATE REL TABLE IF NOT EXISTS DEPENDS_ON(
            FROM Module TO Module
        )
    """,
    "PRODUCES": """
        CREATE REL TABLE IF NOT EXISTS PRODUCES(
            FROM CodeEntity TO RuntimeSpan
        )
    """,
    "RECORDS": """
        CREATE REL TABLE IF NOT EXISTS RECORDS(
            FROM CodeEntity TO Metric
        )
    """,
    "LOGS": """
        CREATE REL TABLE IF NOT EXISTS LOGS(
            FROM CodeEntity TO LogEntry
        )
    """,
    "EMITS": """
        CREATE REL TABLE IF NOT EXISTS EMITS(
            FROM RuntimeSpan TO LogEntry
        )
    """,
    "FOUND_IN": """
        CREATE REL TABLE IF NOT EXISTS FOUND_IN(
            FROM Issue TO Module
        )
    """,
    "AFFECTS": """
        CREATE REL TABLE IF NOT EXISTS AFFECTS(
            FROM Issue TO CodeEntity
        )
    """,
    "EVIDENCED_BY_SPAN": """
        CREATE REL TABLE IF NOT EXISTS EVIDENCED_BY_SPAN(
            FROM Issue TO RuntimeSpan
        )
    """,
    "EVIDENCED_BY_LOG": """
        CREATE REL TABLE IF NOT EXISTS EVIDENCED_BY_LOG(
            FROM Issue TO LogEntry
        )
    """,
}


def _existing_columns(conn, table):
    result = conn.execute(f'CALL TABLE_INFO("{table}") RETURN *')
    columns = set()
    while result.has_next():
        columns.add(result.get_next()[1])
    return columns


def _ensure_column(conn, table, column, column_type, default_literal):
    """Add a column to an already-existing table from a previous schema
    version, via Kuzu's ALTER TABLE (idempotent: a no-op if the column is
    already there, whether from a fresh CREATE TABLE or a prior run of
    this same migration). A brand-new persistent database file predates
    schema changes just as much as a long-running one does - this isn't
    just for the developer's own machine.
    """
    if column not in _existing_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD {column} {column_type} DEFAULT {default_literal}")


def create_schema(conn):
    """Create all node and relationship tables if they don't already exist.

    Node tables must be created before relationship tables that reference
    them, and dict insertion order above already satisfies that.
    """
    for ddl in NODE_TABLES.values():
        conn.execute(ddl)
    for ddl in REL_TABLES.values():
        conn.execute(ddl)

    # Migrations for columns added after a table's initial CREATE TABLE
    # definition - needed so a persistent database created before this
    # column existed keeps working without the user deleting their data.
    _ensure_column(conn, "Module", "parse_error", "STRING", "''")
    _ensure_column(conn, "Module", "abs_path", "STRING", "''")
