# This file contains functions to resolve OpenTelemetry code identities to CodeEntity ids.
# It helps in linking runtime data with static analysis results without requiring an exact "repo root".

from .models import LogEntry, Metric, RuntimeSpan
from .paths import canonical_path_key
from .repository import GraphRepository


def resolve_code_identity(repo: GraphRepository, code_filepath: str, code_function: str,
                           code_namespace: str = "", code_lineno: int = 0) -> str | None:
    """Find the CodeEntity that produced a given OpenTelemetry identity.
    
    Args:
        repo (GraphRepository): The repository to search in.
        code_filepath (str): The file path of the code.
        code_function (str): The function name of the code.
        code_namespace (str, optional): The namespace of the code. Defaults to "".
        code_lineno (int, optional): The line number of the code. Defaults to 0.

    Returns:
        str | None: The id of the CodeEntity if found, otherwise None.
    """
    # Check if file path and function name are provided
    if not code_filepath or not code_function:
        return None

    candidates = []
    
    # Try to find by qualified name (namespace + function)
    if code_namespace:
        qualified_name = f"{code_namespace}.{code_function}"
        candidates = repo.find_code_entities_by_qualified_name(qualified_name)

    # If no candidates found or more than one, try by path and qualified name
    if len(candidates) != 1:
        abs_path_key = canonical_path_key(code_filepath)
        path_candidates = repo.find_code_entities_by_path_and_qualname(abs_path_key, code_function)
        
        # Only use the path-based fallback when namespace-based lookup is empty or agrees with it
        if not candidates:
            candidates = path_candidates
        elif len(candidates) > 1:
            agreeing = [c for c in candidates if c in path_candidates]
            if agreeing:
                candidates = agreeing

    # If exactly one candidate, return its id
    if len(candidates) == 1:
        return candidates[0].id
    
    # If multiple candidates and line number is provided, narrow down by line number
    if len(candidates) > 1 and code_lineno:
        narrowed = [c for c in candidates if c.start_line <= code_lineno <= c.end_line]
        if len(narrowed) == 1:
            return narrowed[0].id

    # Return None if no unique match found
    return None


def resolve_and_link_span(repo: GraphRepository, span: RuntimeSpan) -> str | None:
    """Store a runtime span and link it to a CodeEntity if possible.
    
    Args:
        repo (GraphRepository): The repository to store the span in.
        span (RuntimeSpan): The runtime span to store.

    Returns:
        str | None: The id of the linked CodeEntity if found, otherwise None.
    """
    # Store the span
    repo.upsert_runtime_span(span)

    # Try to resolve the span's identity
    entity_id = resolve_code_identity(
        repo, span.code_filepath, span.code_function, span.code_namespace, span.code_lineno
    )
    
    # If resolved, link it with a PRODUCES edge
    if entity_id is not None:
        repo.add_produces(entity_id, span.id)
    
    return entity_id


def resolve_and_link_metric(repo: GraphRepository, metric: Metric) -> str | None:
    """Store a metric data point and link it to a CodeEntity if possible.
    
    Args:
        repo (GraphRepository): The repository to store the metric in.
        metric (Metric): The metric data point to store.

    Returns:
        str | None: The id of the linked CodeEntity if found, otherwise None.
    """
    # Store the metric
    repo.upsert_metric(metric)

    # Try to resolve the metric's identity
    entity_id = resolve_code_identity(
        repo, metric.code_filepath, metric.code_function, metric.code_namespace, metric.code_lineno
    )
    
    # If resolved, link it with a RECORDS edge
    if entity_id is not None:
        repo.add_records(entity_id, metric.id)
    
    return entity_id


def resolve_and_link_log(repo: GraphRepository, log: LogEntry) -> str | None:
    """Store a log entry and link it to a CodeEntity or RuntimeSpan.
    
    Args:
        repo (GraphRepository): The repository to store the log in.
        log (LogEntry): The log entry to store.

    Returns:
        str | None: The id of the linked entity if found, otherwise None.
    """
    # Store the log
    repo.upsert_log_entry(log)

    # Try to link with a RuntimeSpan via trace/span id
    if log.span_id and repo.get_runtime_span(log.span_id) is not None:
        repo.add_emits(log.span_id, log.id)
        return log.span_id

    # If no span found, try to resolve the log's identity
    entity_id = resolve_code_identity(
        repo, log.code_filepath, log.code_function, log.code_namespace, log.code_lineno
    )
    
    # If resolved, link it with a LOGS edge
    if entity_id is not None:
        repo.add_logs(entity_id, log.id)
    
    return entity_id