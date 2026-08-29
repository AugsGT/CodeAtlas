"""Thin repository layer over the Kuzu graph database.

Wraps a Kuzu connection with typed upsert methods for the node/edge
kinds defined in schema.py, plus a generic query() escape hatch for
Cypher the higher-level pipeline stages (retrieval, etc.) will need.

A single Kuzu Connection is not safe to call from multiple threads at
once (the FastAPI layer runs each sync route in its own threadpool
thread, so two requests hitting this repository concurrently — e.g. a
double-submitted question — is a real scenario, not a hypothetical
one). All access goes through `_execute`, serialized by one lock per
repository instance.
"""

import threading

import kuzu

from .models import CodeEntity, Issue, LogEntry, Metric, Module, RuntimeSpan
from .schema import create_schema


class GraphRepository:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.db = kuzu.Database(self.db_path)
        self.conn = kuzu.Connection(self.db)
        self._lock = threading.Lock()
        create_schema(self.conn)

    def close(self):
        self.conn.close()
        self.db.close()

    def _execute(self, cypher, parameters=None):
        with self._lock:
            return self.conn.execute(cypher, parameters or {})

    def query(self, cypher, parameters=None):
        """Run arbitrary Cypher and return all result rows as a list of lists."""
        result = self._execute(cypher, parameters)
        rows = []
        while result.has_next():
            rows.append(result.get_next())
        return rows

    def clear_static_graph(self):
        """Delete every Module and CodeEntity node (and the CONTAINS/CALLS/
        DEPENDS_ON edges between them) - leaves RuntimeSpan/Metric/LogEntry
        untouched (callers that also want those gone call
        clear_runtime_telemetry() too; /api/ingest calls both, since
        replacing the static graph without also clearing runtime evidence
        would leave orphaned spans pointing at CodeEntities that no longer
        exist).

        Without this, ingesting repository B after repository A leaves A's
        entities in the graph forever - `recent_errors`/`slow_calls` and
        similar whole-graph surveys have no per-repo filter, so a question
        about B could silently be answered using A's leftover evidence. A
        real, observed bug: asking about a file that was never ingested
        returned an unrelated error from a completely different,
        previously-ingested repository, narrated as if it belonged to the
        named file.
        """
        for label in ("Module", "CodeEntity"):
            self._execute(f"MATCH (n:{label}) DETACH DELETE n")

    def has_any_runtime_span(self):
        """Whether any execution has ever been recorded at all - used to
        distinguish "the code ran and found nothing wrong" from "the code
        has never been run," which need very different answers to a
        question like "what are the issues in this repo" (see
        reasoning/pipeline.py's handling of empty recent_errors/slow_calls
        evidence)."""
        rows = self.query("MATCH (s:RuntimeSpan) RETURN count(s) LIMIT 1")
        return bool(rows and rows[0][0] > 0)

    def clear_runtime_telemetry(self):
        """Delete every RuntimeSpan, Metric, and LogEntry node (and the
        edges connecting them) - leaves Module/CodeEntity/CALLS/DEPENDS_ON
        untouched. Runtime telemetry has no notion of "current" vs "stale"
        on its own (each ingest just adds more nodes), so callers that
        want evidence to reflect only the latest observed execution -
        rather than an ever-growing mix of every past run - call this
        before writing new telemetry.
        """
        for label in ("RuntimeSpan", "Metric", "LogEntry"):
            self._execute(f"MATCH (n:{label}) DETACH DELETE n")

    def clear_all_issues(self):
        """Delete every Issue node. Called on /api/ingest (a full
        graph reset) - static issues (parse errors) get regenerated
        right after by write_static_issues, and runtime issues get
        regenerated the next time anything reads them (see
        graph/issues.py::sync_runtime_issues)."""
        self._execute("MATCH (i:Issue) DETACH DELETE i")

    def clear_issues_with_prefix(self, prefix):
        """Delete only the Issue nodes whose id starts with `prefix` -
        lets independent issue producers (parse-error issues at ingest
        time, span/log-derived issues resynced on every alerts read,
        process-level timeout/exit-code issues written once per
        execution) each own and clear only their own id namespace
        without stepping on each other. See graph/issues.py."""
        self._execute("MATCH (i:Issue) WHERE i.id STARTS WITH $prefix DETACH DELETE i", {"prefix": prefix})

    def delete_issue(self, issue_id):
        self._execute("MATCH (i:Issue {id: $id}) DETACH DELETE i", {"id": issue_id})

    # --- Module ---

    def upsert_module(self, module: Module):
        self._execute(
            """
            MERGE (m:Module {path: $path})
            SET m.name = $name, m.language = $language, m.parse_error = $parse_error,
                m.abs_path = $abs_path
            """,
            {
                "path": module.path,
                "name": module.name,
                "language": module.language,
                "parse_error": module.parse_error,
                "abs_path": module.abs_path,
            },
        )

    def get_module(self, path):
        rows = self.query(
            "MATCH (m:Module {path: $path}) RETURN m.path, m.name, m.language, m.parse_error, m.abs_path",
            {"path": path},
        )
        if not rows:
            return None
        p, name, language, parse_error, abs_path = rows[0]
        return Module(path=p, name=name, language=language, parse_error=parse_error or "", abs_path=abs_path or "")

    def list_modules(self):
        rows = self.query(
            "MATCH (m:Module) RETURN m.path, m.name, m.language, m.parse_error, m.abs_path ORDER BY m.path"
        )
        return [
            Module(path=p, name=name, language=language, parse_error=parse_error or "", abs_path=abs_path or "")
            for (p, name, language, parse_error, abs_path) in rows
        ]

    # --- CodeEntity ---

    def upsert_code_entity(self, entity: CodeEntity):
        self._execute(
            """
            MERGE (e:CodeEntity {id: $id})
            SET e.qualified_name = $qualified_name,
                e.name = $name,
                e.kind = $kind,
                e.module_path = $module_path,
                e.start_line = $start_line,
                e.end_line = $end_line,
                e.abs_path = $abs_path,
                e.local_qualname = $local_qualname
            """,
            {
                "id": entity.id,
                "qualified_name": entity.qualified_name,
                "name": entity.name,
                "kind": entity.kind,
                "module_path": entity.module_path,
                "start_line": entity.start_line,
                "end_line": entity.end_line,
                "abs_path": entity.abs_path,
                "local_qualname": entity.local_qualname,
            },
        )

    def get_code_entity(self, id):
        rows = self.query(
            """
            MATCH (e:CodeEntity {id: $id})
            RETURN e.id, e.qualified_name, e.name, e.kind, e.module_path, e.start_line, e.end_line,
                   e.abs_path, e.local_qualname
            """,
            {"id": id},
        )
        if not rows:
            return None
        return self._row_to_code_entity(rows[0])

    def _row_to_code_entity(self, row):
        (id_, qualified_name, name, kind, module_path, start_line, end_line,
         abs_path, local_qualname) = row
        return CodeEntity(
            id=id_,
            qualified_name=qualified_name,
            name=name,
            kind=kind,
            module_path=module_path,
            start_line=start_line,
            end_line=end_line,
            abs_path=abs_path or "",
            local_qualname=local_qualname or "",
        )

    def find_code_entities_by_qualified_name(self, qualified_name):
        """All CodeEntity nodes with this exact qualified name (module
        namespace + local qualname combined) — usually unique, but a graph
        holding more than one ingested repo could have collisions, which is
        exactly why this returns a list for the caller to treat as
        ambiguous rather than silently picking one."""
        rows = self.query(
            """
            MATCH (e:CodeEntity {qualified_name: $qualified_name})
            RETURN e.id, e.qualified_name, e.name, e.kind, e.module_path, e.start_line, e.end_line,
                   e.abs_path, e.local_qualname
            """,
            {"qualified_name": qualified_name},
        )
        return [self._row_to_code_entity(row) for row in rows]

    def find_code_entities_by_path_and_qualname(self, abs_path_key, local_qualname):
        """All CodeEntity nodes whose canonical file path and local
        qualified name match — the fallback signal that works even when
        the static analyzer's module namespace doesn't line up with the
        runtime's (e.g. the repo was ingested from a different directory
        than the one actually on sys.path when the code executed)."""
        rows = self.query(
            """
            MATCH (e:CodeEntity {abs_path: $abs_path, local_qualname: $local_qualname})
            RETURN e.id, e.qualified_name, e.name, e.kind, e.module_path, e.start_line, e.end_line,
                   e.abs_path, e.local_qualname
            """,
            {"abs_path": abs_path_key, "local_qualname": local_qualname},
        )
        return [self._row_to_code_entity(row) for row in rows]

    # --- RuntimeSpan ---

    def upsert_runtime_span(self, span: RuntimeSpan):
        self._execute(
            """
            MERGE (s:RuntimeSpan {id: $id})
            SET s.trace_id = $trace_id,
                s.name = $name,
                s.start_time = $start_time,
                s.end_time = $end_time,
                s.duration_ms = $duration_ms,
                s.status = $status,
                s.error_message = $error_message,
                s.return_value = $return_value,
                s.code_filepath = $code_filepath,
                s.code_namespace = $code_namespace,
                s.code_function = $code_function,
                s.code_lineno = $code_lineno
            """,
            {
                "id": span.id,
                "trace_id": span.trace_id,
                "name": span.name,
                "start_time": span.start_time,
                "end_time": span.end_time,
                "duration_ms": span.duration_ms,
                "status": span.status,
                "error_message": span.error_message,
                "return_value": span.return_value,
                "code_filepath": span.code_filepath,
                "code_namespace": span.code_namespace,
                "code_function": span.code_function,
                "code_lineno": span.code_lineno,
            },
        )

    def get_runtime_span(self, id):
        rows = self.query(
            """
            MATCH (s:RuntimeSpan {id: $id})
            RETURN s.id, s.trace_id, s.name, s.start_time, s.end_time, s.duration_ms,
                   s.status, s.error_message, s.return_value, s.code_filepath, s.code_namespace,
                   s.code_function, s.code_lineno
            """,
            {"id": id},
        )
        if not rows:
            return None
        (id_, trace_id, name, start_time, end_time, duration_ms, status, error_message,
         return_value, code_filepath, code_namespace, code_function, code_lineno) = rows[0]
        return RuntimeSpan(
            id=id_,
            trace_id=trace_id,
            name=name,
            start_time=start_time,
            end_time=end_time,
            duration_ms=duration_ms,
            status=status,
            error_message=error_message,
            return_value=return_value or "",
            code_filepath=code_filepath,
            code_namespace=code_namespace,
            code_function=code_function,
            code_lineno=code_lineno,
        )

    # --- Metric ---

    def upsert_metric(self, metric: Metric):
        self._execute(
            """
            MERGE (m:Metric {id: $id})
            SET m.name = $name,
                m.value = $value,
                m.unit = $unit,
                m.timestamp = $timestamp,
                m.code_filepath = $code_filepath,
                m.code_namespace = $code_namespace,
                m.code_function = $code_function,
                m.code_lineno = $code_lineno
            """,
            {
                "id": metric.id,
                "name": metric.name,
                "value": metric.value,
                "unit": metric.unit,
                "timestamp": metric.timestamp,
                "code_filepath": metric.code_filepath,
                "code_namespace": metric.code_namespace,
                "code_function": metric.code_function,
                "code_lineno": metric.code_lineno,
            },
        )

    def get_metric(self, id):
        rows = self.query(
            """
            MATCH (m:Metric {id: $id})
            RETURN m.id, m.name, m.value, m.unit, m.timestamp,
                   m.code_filepath, m.code_namespace, m.code_function, m.code_lineno
            """,
            {"id": id},
        )
        if not rows:
            return None
        (id_, name, value, unit, timestamp,
         code_filepath, code_namespace, code_function, code_lineno) = rows[0]
        return Metric(
            id=id_, name=name, value=value, unit=unit, timestamp=timestamp,
            code_filepath=code_filepath, code_namespace=code_namespace,
            code_function=code_function, code_lineno=code_lineno,
        )

    # --- LogEntry ---

    def upsert_log_entry(self, log: LogEntry):
        self._execute(
            """
            MERGE (l:LogEntry {id: $id})
            SET l.message = $message,
                l.level = $level,
                l.timestamp = $timestamp,
                l.trace_id = $trace_id,
                l.span_id = $span_id,
                l.code_filepath = $code_filepath,
                l.code_namespace = $code_namespace,
                l.code_function = $code_function,
                l.code_lineno = $code_lineno
            """,
            {
                "id": log.id,
                "message": log.message,
                "level": log.level,
                "timestamp": log.timestamp,
                "trace_id": log.trace_id,
                "span_id": log.span_id,
                "code_filepath": log.code_filepath,
                "code_namespace": log.code_namespace,
                "code_function": log.code_function,
                "code_lineno": log.code_lineno,
            },
        )

    def get_log_entry(self, id):
        rows = self.query(
            """
            MATCH (l:LogEntry {id: $id})
            RETURN l.id, l.message, l.level, l.timestamp, l.trace_id, l.span_id,
                   l.code_filepath, l.code_namespace, l.code_function, l.code_lineno
            """,
            {"id": id},
        )
        if not rows:
            return None
        (id_, message, level, timestamp, trace_id, span_id,
         code_filepath, code_namespace, code_function, code_lineno) = rows[0]
        return LogEntry(
            id=id_, message=message, level=level, timestamp=timestamp,
            trace_id=trace_id, span_id=span_id,
            code_filepath=code_filepath, code_namespace=code_namespace,
            code_function=code_function, code_lineno=code_lineno,
        )

    # --- Issue ---

    def upsert_issue(self, issue: Issue):
        self._execute(
            """
            MERGE (i:Issue {id: $id})
            SET i.type = $type,
                i.severity = $severity,
                i.detection_method = $detection_method,
                i.message = $message,
                i.file = $file,
                i.line = $line,
                i.status = $status
            """,
            {
                "id": issue.id,
                "type": issue.type,
                "severity": issue.severity,
                "detection_method": issue.detection_method,
                "message": issue.message,
                "file": issue.file,
                "line": issue.line,
                "status": issue.status,
            },
        )

    def get_issue(self, id):
        rows = self.query(
            "MATCH (i:Issue {id: $id}) "
            "RETURN i.id, i.type, i.severity, i.detection_method, i.message, i.file, i.line, i.status",
            {"id": id},
        )
        if not rows:
            return None
        id_, type_, severity, detection_method, message, file, line, status = rows[0]
        return Issue(
            id=id_, type=type_, severity=severity, detection_method=detection_method,
            message=message, file=file or "", line=line or 0, status=status or "open",
        )

    def list_issues(self):
        """Every Issue currently in the graph, with whatever it's linked
        to (a CodeEntity via AFFECTS, a Module via FOUND_IN, a
        RuntimeSpan/LogEntry via EVIDENCED_BY_*) resolved alongside it -
        the single read path alerts and evidence retrieval both build on.
        """
        rows = self.query(
            """
            MATCH (i:Issue)
            OPTIONAL MATCH (i)-[:AFFECTS]->(e:CodeEntity)
            OPTIONAL MATCH (i)-[:FOUND_IN]->(m:Module)
            OPTIONAL MATCH (i)-[:EVIDENCED_BY_SPAN]->(s:RuntimeSpan)
            OPTIONAL MATCH (i)-[:EVIDENCED_BY_LOG]->(l:LogEntry)
            RETURN i.id, i.type, i.severity, i.detection_method, i.message, i.file, i.line, i.status,
                   e.id, m.path, s.id, l.id
            """
        )
        return [
            {
                "id": id_, "type": type_, "severity": severity, "detection_method": detection_method,
                "message": message, "file": file or "", "line": line or 0, "status": status or "open",
                "entity_id": entity_id, "module_path": module_path, "span_id": span_id, "log_id": log_id,
            }
            for (id_, type_, severity, detection_method, message, file, line, status,
                 entity_id, module_path, span_id, log_id) in rows
        ]

    def add_issue_found_in(self, issue_id, module_path):
        self._execute(
            """
            MATCH (i:Issue {id: $issue_id}), (m:Module {path: $module_path})
            MERGE (i)-[:FOUND_IN]->(m)
            """,
            {"issue_id": issue_id, "module_path": module_path},
        )

    def add_issue_affects(self, issue_id, entity_id):
        self._execute(
            """
            MATCH (i:Issue {id: $issue_id}), (e:CodeEntity {id: $entity_id})
            MERGE (i)-[:AFFECTS]->(e)
            """,
            {"issue_id": issue_id, "entity_id": entity_id},
        )

    def add_issue_evidenced_by_span(self, issue_id, span_id):
        self._execute(
            """
            MATCH (i:Issue {id: $issue_id}), (s:RuntimeSpan {id: $span_id})
            MERGE (i)-[:EVIDENCED_BY_SPAN]->(s)
            """,
            {"issue_id": issue_id, "span_id": span_id},
        )

    def add_issue_evidenced_by_log(self, issue_id, log_id):
        self._execute(
            """
            MATCH (i:Issue {id: $issue_id}), (l:LogEntry {id: $log_id})
            MERGE (i)-[:EVIDENCED_BY_LOG]->(l)
            """,
            {"issue_id": issue_id, "log_id": log_id},
        )

    # --- Edges ---

    def add_contains(self, module_path, entity_id):
        self._execute(
            """
            MATCH (m:Module {path: $module_path}), (e:CodeEntity {id: $entity_id})
            MERGE (m)-[:CONTAINS]->(e)
            """,
            {"module_path": module_path, "entity_id": entity_id},
        )

    def add_calls(self, caller_id, callee_id, call_line=None):
        self._execute(
            """
            MATCH (a:CodeEntity {id: $caller_id}), (b:CodeEntity {id: $callee_id})
            MERGE (a)-[r:CALLS]->(b)
            SET r.call_line = $call_line
            """,
            {"caller_id": caller_id, "callee_id": callee_id, "call_line": call_line},
        )

    def add_depends_on(self, from_path, to_path):
        self._execute(
            """
            MATCH (a:Module {path: $from_path}), (b:Module {path: $to_path})
            MERGE (a)-[:DEPENDS_ON]->(b)
            """,
            {"from_path": from_path, "to_path": to_path},
        )

    def add_produces(self, entity_id, span_id):
        self._execute(
            """
            MATCH (e:CodeEntity {id: $entity_id}), (s:RuntimeSpan {id: $span_id})
            MERGE (e)-[:PRODUCES]->(s)
            """,
            {"entity_id": entity_id, "span_id": span_id},
        )

    def add_records(self, entity_id, metric_id):
        self._execute(
            """
            MATCH (e:CodeEntity {id: $entity_id}), (m:Metric {id: $metric_id})
            MERGE (e)-[:RECORDS]->(m)
            """,
            {"entity_id": entity_id, "metric_id": metric_id},
        )

    def add_logs(self, entity_id, log_id):
        self._execute(
            """
            MATCH (e:CodeEntity {id: $entity_id}), (l:LogEntry {id: $log_id})
            MERGE (e)-[:LOGS]->(l)
            """,
            {"entity_id": entity_id, "log_id": log_id},
        )

    def add_emits(self, span_id, log_id):
        self._execute(
            """
            MATCH (s:RuntimeSpan {id: $span_id}), (l:LogEntry {id: $log_id})
            MERGE (s)-[:EMITS]->(l)
            """,
            {"span_id": span_id, "log_id": log_id},
        )

    def entities_in_module(self, module_path):
        rows = self.query(
            """
            MATCH (m:Module {path: $module_path})-[:CONTAINS]->(e:CodeEntity)
            RETURN e.id, e.name, e.kind
            """,
            {"module_path": module_path},
        )
        return [{"id": r[0], "name": r[1], "kind": r[2]} for r in rows]

    def callers_of(self, entity_id):
        rows = self.query(
            """
            MATCH (a:CodeEntity)-[r:CALLS]->(b:CodeEntity {id: $entity_id})
            RETURN a.id, r.call_line
            """,
            {"entity_id": entity_id},
        )
        return [{"caller_id": r[0], "call_line": r[1]} for r in rows]

    def callees_of(self, entity_id):
        rows = self.query(
            """
            MATCH (a:CodeEntity {id: $entity_id})-[r:CALLS]->(b:CodeEntity)
            RETURN b.id, r.call_line
            """,
            {"entity_id": entity_id},
        )
        return [{"callee_id": r[0], "call_line": r[1]} for r in rows]

    def spans_for_entity(self, entity_id):
        rows = self.query(
            """
            MATCH (e:CodeEntity {id: $entity_id})-[:PRODUCES]->(s:RuntimeSpan)
            RETURN s.id, s.name, s.duration_ms
            """,
            {"entity_id": entity_id},
        )
        return [{"id": r[0], "name": r[1], "duration_ms": r[2]} for r in rows]

    def metrics_for_entity(self, entity_id):
        rows = self.query(
            """
            MATCH (e:CodeEntity {id: $entity_id})-[:RECORDS]->(m:Metric)
            RETURN m.id, m.name, m.value
            """,
            {"entity_id": entity_id},
        )
        return [{"id": r[0], "name": r[1], "value": r[2]} for r in rows]

    def logs_for_span(self, span_id):
        rows = self.query(
            """
            MATCH (s:RuntimeSpan {id: $span_id})-[:EMITS]->(l:LogEntry)
            RETURN l.id, l.message, l.level
            """,
            {"span_id": span_id},
        )
        return [{"id": r[0], "message": r[1], "level": r[2]} for r in rows]

    def logs_for_entity(self, entity_id):
        rows = self.query(
            """
            MATCH (e:CodeEntity {id: $entity_id})-[:LOGS]->(l:LogEntry)
            RETURN l.id, l.message, l.level
            """,
            {"entity_id": entity_id},
        )
        return [{"id": r[0], "message": r[1], "level": r[2]} for r in rows]
