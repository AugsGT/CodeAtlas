"""Deterministic Issue generation: turns raw facts already sitting in
the graph (a Module's parse_error, a failed/slow RuntimeSpan, an
ERROR/WARN LogEntry, a non-zero exit code, a timed-out execution) into
first-class Issue nodes with a consistent type/severity/detection_method
shape, linked back to whatever they're about via FOUND_IN/AFFECTS/
EVIDENCED_BY_SPAN/EVIDENCED_BY_LOG.

This is the single place issues get created, so alerts, retrieval, and
reasoning all read one consistent representation instead of each having
to separately know the three different raw shapes. Before this existed,
the alert system only ever queried RuntimeSpan/LogEntry - a real bug
found live: a file with nothing but a syntax error (no execution
possible at all) produced zero alerts, even though the exact same fact
was already sitting in Module.parse_error the whole time.

Three independent generators, each owning its own Issue id namespace so
they can clear and regenerate independently without stepping on each
other (see GraphRepository.clear_issues_with_prefix):

  - write_static_issues: one Issue per module that failed to parse, PLUS
    one per pyflakes-derived quality diagnostic (suspicious constructs,
    unresolved references, unused imports - see analysis/quality.py),
    attributed to the containing CodeEntity where its line falls inside
    one. Written once, right after static analysis - neither a parse
    error nor a quality diagnostic changes until the next ingest.
  - sync_runtime_issues: re-derived from whatever RuntimeSpan/LogEntry
    nodes currently exist, every time it's called (spans/logs are
    themselves replaced wholesale on every execution, so this has no
    persistent state of its own to go stale).
  - write_execution_issues: process-level facts (timeout, non-zero exit)
    that never show up as any single function's RuntimeSpan or LogEntry,
    since no traced function has to raise for a script to exit non-zero.
"""

import re
import threading

from .models import Issue

# sync_runtime_issues does a non-transactional clear-then-rebuild (several
# separate Cypher statements, not one atomic operation) - two overlapping
# calls (e.g. the dashboard's own /api/alerts and /api/issues firing close
# together after an execute) can interleave their clears and inserts and
# leave the graph in a genuinely inconsistent state, observed live as
# /api/stats' Issue count disagreeing with /api/alerts' own count. This
# lock only serializes CodeAtlas's own concurrent callers within one
# process - it doesn't make the clear-then-rebuild atomic against an
# unrelated read (like /api/stats' own count query) landing mid-rebuild,
# which remains a narrow, accepted race.
_sync_lock = threading.Lock()

DEFAULT_SLOW_THRESHOLD_MS = 500.0
DEFAULT_LIMIT = 200

_LINE_RE = re.compile(r"line (\d+)")

_SPAN_ERROR_PREFIX = "issue:span-error:"
_SLOW_CALL_PREFIX = "issue:slow-call:"
_LOG_PREFIX = "issue:log:"
_PARSE_ERROR_PREFIX = "issue:parse-error:"
_QUALITY_PREFIX = "issue:quality:"
TIMEOUT_ISSUE_ID = "issue:timeout"
NONZERO_EXIT_ISSUE_ID = "issue:nonzero-exit"


def _parse_error_line(parse_error: str) -> int:
    match = _LINE_RE.search(parse_error)
    return int(match.group(1)) if match else 0


def generate_parse_error_issues(modules) -> list[Issue]:
    """One Issue per Module that failed to parse."""
    issues = []
    for module in modules:
        if not module.parse_error:
            continue
        issues.append(Issue(
            id=f"{_PARSE_ERROR_PREFIX}{module.path}",
            type="SyntaxError",
            severity="critical",
            detection_method="static",
            message=module.parse_error,
            file=module.path,
            line=_parse_error_line(module.parse_error),
        ))
    return issues


def _entity_containing_line(entities_by_module, module_path, line):
    """The narrowest CodeEntity (function/method/class) whose line range
    contains `line`, if any - lets a quality diagnostic be attributed to
    the specific function it's in, not just its containing class. A
    class's own line range spans every one of its methods too, so
    picking just the first containing entity (in AST-visit order, which
    always finds the class before its methods) would attribute a
    diagnostic inside a method to the class instead of the method -
    picking the SMALLEST matching range instead always prefers the most
    specific entity. None for a module-level diagnostic (e.g. an unused
    top-level import, which sits inside no function or class at all).
    """
    best = None
    best_span = None
    for entity in entities_by_module.get(module_path, []):
        if entity.start_line <= line <= entity.end_line:
            span = entity.end_line - entity.start_line
            if best_span is None or span < best_span:
                best, best_span = entity.id, span
    return best


def write_static_issues(repo, result) -> list[Issue]:
    """Generate and persist every static-analysis-derived issue for this
    ingest: parse errors (one per module that failed to parse) and
    quality diagnostics (pyflakes-derived suspicious constructs/
    unresolved references/unused imports - see analysis/quality.py),
    attributed to their containing CodeEntity where their line falls
    inside one. Callers that fully reset the graph (e.g. /api/ingest)
    should clear old issues first via repo.clear_all_issues() - this
    function only ever adds.

    Takes the whole AnalysisResult (not just `.modules`) since quality
    diagnostics need `.entities`' line ranges to attribute themselves to
    a specific function rather than just the file.
    """
    issues = generate_parse_error_issues(result.modules)
    for issue in issues:
        repo.upsert_issue(issue)
        repo.add_issue_found_in(issue.id, issue.file)

    entities_by_module = {}
    for entity in result.entities:
        entities_by_module.setdefault(entity.module_path, []).append(entity)

    for index, (module_path, diagnostic) in enumerate(result.diagnostics):
        issue = Issue(
            id=f"{_QUALITY_PREFIX}{index}",
            type=diagnostic.type,
            severity=diagnostic.severity,
            detection_method="static",
            message=diagnostic.message,
            file=module_path,
            line=diagnostic.line,
        )
        repo.upsert_issue(issue)
        repo.add_issue_found_in(issue.id, module_path)
        entity_id = _entity_containing_line(entities_by_module, module_path, diagnostic.line)
        if entity_id:
            repo.add_issue_affects(issue.id, entity_id)
        issues.append(issue)

    return issues


def sync_runtime_issues(repo, slow_threshold_ms=DEFAULT_SLOW_THRESHOLD_MS, limit=DEFAULT_LIMIT) -> list[Issue]:
    """Re-derive every RuntimeSpan/LogEntry-based Issue from current
    graph state. Idempotent and safe to call on every read (e.g. before
    every /api/alerts response): clears its own id namespace first and
    rebuilds fresh, since spans/logs themselves get replaced wholesale on
    every execution (see GraphRepository.clear_runtime_telemetry) - there
    is no incremental state to maintain here.

    Deliberately does not distinguish "runtime" (CodeAtlas's own
    sandboxed execution) from "telemetry" (externally supplied OTLP) -
    RuntimeSpan/LogEntry carry no field recording which path produced
    them, so both are tagged detection_method="runtime" here. Splitting
    that out would need a real origin field on RuntimeSpan itself, which
    is a separate change from unifying issue representation.
    """
    with _sync_lock:
        return _sync_runtime_issues_locked(repo, slow_threshold_ms, limit)


def _sync_runtime_issues_locked(repo, slow_threshold_ms, limit) -> list[Issue]:
    repo.clear_issues_with_prefix(_SPAN_ERROR_PREFIX)
    repo.clear_issues_with_prefix(_SLOW_CALL_PREFIX)
    repo.clear_issues_with_prefix(_LOG_PREFIX)

    issues = []

    for span_id, name, error_message, entity_id, module_path, start_line in repo.query(
        "MATCH (e:CodeEntity)-[:PRODUCES]->(s:RuntimeSpan) WHERE s.status = 'ERROR' "
        "RETURN s.id, s.name, s.error_message, e.id, e.module_path, e.start_line LIMIT $limit",
        {"limit": limit},
    ):
        issue = Issue(
            id=f"{_SPAN_ERROR_PREFIX}{span_id}",
            type="UncaughtException",
            severity="high",
            detection_method="runtime",
            message=f"{name} failed: {error_message or 'unknown error'}",
            # The affected entity's own file/start line - not the exact
            # failing line (spans don't currently record one), but enough
            # for the dashboard's code viewer to place the issue inline
            # against the right function rather than nowhere at all.
            file=module_path or "",
            line=start_line or 0,
        )
        repo.upsert_issue(issue)
        repo.add_issue_affects(issue.id, entity_id)
        repo.add_issue_evidenced_by_span(issue.id, span_id)
        issues.append(issue)

    for span_id, name, duration_ms, entity_id, module_path, start_line in repo.query(
        "MATCH (e:CodeEntity)-[:PRODUCES]->(s:RuntimeSpan) WHERE s.duration_ms > $threshold "
        "RETURN s.id, s.name, s.duration_ms, e.id, e.module_path, e.start_line ORDER BY s.duration_ms DESC LIMIT $limit",
        {"threshold": float(slow_threshold_ms), "limit": limit},
    ):
        issue = Issue(
            id=f"{_SLOW_CALL_PREFIX}{span_id}",
            type="SlowCall",
            severity="medium",
            detection_method="runtime",
            message=f"{name} took {duration_ms:.1f}ms (over the {slow_threshold_ms:.0f}ms threshold)",
            file=module_path or "",
            line=start_line or 0,
        )
        repo.upsert_issue(issue)
        repo.add_issue_affects(issue.id, entity_id)
        repo.add_issue_evidenced_by_span(issue.id, span_id)
        issues.append(issue)

    # ERROR/WARN log entries not already covered by a span-error issue
    # above - a log emitted during an otherwise-successful span, or a
    # process-level crash log with no owning span at all (no entity to
    # attribute file/line to, in that last case - left empty rather than
    # guessed).
    for log_id, message, level in repo.query(
        "MATCH (l:LogEntry) WHERE l.level IN ['ERROR', 'WARN'] RETURN l.id, l.message, l.level LIMIT $limit",
        {"limit": limit},
    ):
        entity_rows = repo.query(
            "MATCH (e:CodeEntity)-[:PRODUCES]->(:RuntimeSpan)-[:EMITS]->(l:LogEntry {id: $id}) "
            "RETURN e.id, e.module_path, e.start_line LIMIT 1",
            {"id": log_id},
        )
        entity_id, module_path, start_line = entity_rows[0] if entity_rows else (None, "", 0)

        issue = Issue(
            id=f"{_LOG_PREFIX}{log_id}",
            type="LoggedError" if level == "ERROR" else "LoggedWarning",
            severity="high" if level == "ERROR" else "medium",
            detection_method="runtime",
            message=message[:300],
            file=module_path or "",
            line=start_line or 0,
        )
        repo.upsert_issue(issue)
        repo.add_issue_evidenced_by_log(issue.id, log_id)
        if entity_id:
            repo.add_issue_affects(issue.id, entity_id)
        issues.append(issue)

    return issues


def write_execution_issues(repo, timed_out: bool, exit_code, has_crash: bool) -> list[Issue]:
    """Process-level facts that never show up in any per-function
    RuntimeSpan/LogEntry scan at all - a script can exit non-zero (e.g.
    sys.exit(1)) or run past its timeout with no traced function ever
    raising. Owns TIMEOUT_ISSUE_ID/NONZERO_EXIT_ISSUE_ID exclusively:
    always clears both before deciding whether to recreate either, so a
    fixed timeout/exit code from a later run doesn't leave a stale issue
    behind.
    """
    repo.delete_issue(TIMEOUT_ISSUE_ID)
    repo.delete_issue(NONZERO_EXIT_ISSUE_ID)

    if timed_out:
        issue = Issue(
            id=TIMEOUT_ISSUE_ID,
            type="Timeout",
            severity="high",
            detection_method="runtime",
            message="Execution did not complete within the configured timeout and was killed.",
        )
        repo.upsert_issue(issue)
        return [issue]

    if exit_code not in (0, None) and not has_crash:
        # A crash (uncaught exception escaping the entry point) already
        # produces its own, more specific LoggedError issue via
        # sync_runtime_issues (from the crash log written by
        # execution/pipeline.py); only report the bare exit code when
        # there's no more specific explanation for it (e.g. sys.exit(1)
        # with no exception at all).
        issue = Issue(
            id=NONZERO_EXIT_ISSUE_ID,
            type="NonZeroExit",
            severity="high",
            detection_method="runtime",
            message=f"Process exited with a non-zero status code ({exit_code}).",
        )
        repo.upsert_issue(issue)
        return [issue]

    return []
