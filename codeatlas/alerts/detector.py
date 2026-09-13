"""Proactive problem detection: reads the unified Issue graph (see
graph/issues.py) and turns it into a flat, severity-ordered list the
dashboard can show without the developer having to ask a question
first.

Before the Issue node existed, this queried RuntimeSpan/LogEntry
directly - which meant a repository with nothing but a static syntax
error (no execution possible at all) produced zero alerts, since
nothing here ever looked at Module.parse_error. Reading from Issue
instead fixes that structurally: static and runtime problems are the
same kind of node here, so there's no longer a code path that only
checks one of the two.
"""

from dataclasses import dataclass

from ..graph.issues import sync_runtime_issues

DEFAULT_SLOW_THRESHOLD_MS = 500.0
DEFAULT_LIMIT = 20

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
# The dashboard's existing alert styling only knows two levels - map the
# Issue graph's finer severity scale onto it rather than changing the
# display contract for this change.
_ALERT_SEVERITY = {"critical": "error", "high": "error", "medium": "warning", "low": "warning", "info": "warning"}


@dataclass
class Alert:
    severity: str  # "error" | "warning" - see _ALERT_SEVERITY
    message: str
    entity_id: str | None
    evidence_id: str  # the RuntimeSpan/LogEntry/Module node id this issue is grounded in
    issue_id: str = ""
    type: str = ""
    detection_method: str = ""
    file: str = ""
    line: int = 0


def detect_alerts(repo, slow_threshold_ms=DEFAULT_SLOW_THRESHOLD_MS, limit=DEFAULT_LIMIT) -> list[Alert]:
    sync_runtime_issues(repo, slow_threshold_ms=slow_threshold_ms, limit=limit)
    issues = repo.list_issues()
    issues.sort(key=lambda i: _SEVERITY_ORDER.get(i["severity"], 99))
    # Bounded, not just sorted: static quality checks (see
    # analysis/quality.py) can produce many low-severity findings on a
    # large repo - without this, the proactive panel could be flooded
    # with unused-import notices ahead of a developer even asking a
    # question, which defeats the point of a short, actionable list.
    issues = issues[:limit]

    alerts = []
    for issue in issues:
        evidence_id = issue["span_id"] or issue["log_id"] or issue["module_path"] or issue["id"]
        alerts.append(Alert(
            severity=_ALERT_SEVERITY.get(issue["severity"], "warning"),
            message=issue["message"],
            entity_id=issue["entity_id"],
            evidence_id=evidence_id,
            issue_id=issue["id"],
            type=issue["type"],
            detection_method=issue["detection_method"],
            file=issue["file"] or issue["module_path"] or "",
            line=issue["line"],
        ))
    return alerts
