import datetime

from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult

from ..graph.identity import resolve_and_link_log
from ..graph.models import LogEntry


# This function converts a readable log record into a LogEntry object.
def to_log_entry(readable_log_record) -> LogEntry:
    # Extract the log record and its attributes.
    record = readable_log_record.log_record
    attrs = dict(record.attributes or {})
    
    # Get the timestamp of the log entry, using observed timestamp if available.
    timestamp_nanos = record.timestamp or record.observed_timestamp
    
    # Create a new LogEntry object with various properties.
    return LogEntry(
        id=_make_log_id(record),
        message=str(record.body) if record.body is not None else "",
        level=record.severity_text or "",
        timestamp=_to_datetime(timestamp_nanos) if timestamp_nanos else datetime.datetime.now(datetime.timezone.utc),
        trace_id=format(record.trace_id, "032x") if record.trace_id else "",
        span_id=format(record.span_id, "016x") if record.span_id else "",
        code_filepath=attrs.get("code.filepath", ""),
        code_namespace=attrs.get("code.namespace", ""),
        code_function=attrs.get("code.function", ""),
        code_lineno=attrs.get("code.lineno", 0),
    )


# Helper function to create a unique ID for a log entry.
def _make_log_id(record):
    # Use the timestamp or observed timestamp, defaulting to 0 if not available.
    ts = record.timestamp or record.observed_timestamp or 0
    # Format the trace ID, span ID, and timestamp into a single string.
    return f"{record.trace_id:032x}-{record.span_id:016x}-{ts}"


# Helper function to convert an epoch timestamp in nanoseconds to a datetime object.
def _to_datetime(epoch_nanos):
    # Convert the nanoseconds to seconds and create a UTC datetime object.
    return datetime.datetime.fromtimestamp(epoch_nanos / 1e9, tz=datetime.timezone.utc)


# This class exports log records directly into the graph by resolving each log entry to a RuntimeSpan or CodeEntity.
class GraphLogExporter(LogRecordExporter):
    """LogRecordExporter that writes exported log records directly into
    the graph, resolving each to a RuntimeSpan or CodeEntity (no "repo
    root" needed — see identity.py).
    """

    # Initialize the exporter with a repository object.
    def __init__(self, repo):
        self.repo = repo

    # Export a batch of log records by converting them to LogEntry objects and linking them to the graph.
    def export(self, batch):
        for readable_log_record in batch:
            log_entry = to_log_entry(readable_log_record)
            resolve_and_link_log(self.repo, log_entry)
        return LogRecordExportResult.SUCCESS

    # Perform any necessary cleanup when shutting down the exporter.
    def shutdown(self):
        pass

    # Forcefully flush any buffered logs within a specified timeout.
    def force_flush(self, timeout_millis=30_000):
        return True