# In-memory representations of graph node kinds.

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Module:
    # Represents a module in the codebase.
    path: str  # Path to the module file.
    name: str  # Name of the module.
    language: str  # Programming language used in the module.
    parse_error: str = ""  # Error message if parsing fails, empty otherwise.
    abs_path: str = ""  # Canonical absolute path of the module.


@dataclass
class CodeEntity:
    # Represents a code entity like a function or class.
    id: str  # Unique identifier for the entity.
    qualified_name: str  # Full name including module path.
    name: str  # Name of the entity.
    kind: str  # Type of entity (e.g., function, class).
    module_path: str  # Path to the module containing this entity.
    start_line: int  # Starting line number in the file.
    end_line: int  # Ending line number in the file.
    abs_path: str = ""  # Canonical absolute path of the entity's source file.
    local_qualname: str = ""  # Qualified name without module prefix.


@dataclass
class RuntimeSpan:
    # Represents a span of execution captured during runtime tracing.
    id: str  # Unique identifier for the span.
    trace_id: str  # Identifier for the entire trace.
    name: str  # Name of the operation or function.
    start_time: datetime  # Start time of the span.
    end_time: datetime  # End time of the span.
    duration_ms: float  # Duration of the span in milliseconds.
    status: str  # Status of the span (e.g., OK, ERROR).
    error_message: str = ""  # Error message if the span failed.
    return_value: str = ""  # Return value of the operation.
    code_filepath: str = ""  # Path to the file where the operation occurred.
    code_namespace: str = ""  # Namespace of the operation.
    code_function: str = ""  # Name of the function or method.
    code_lineno: int = 0  # Line number in the file.


@dataclass
class Metric:
    # Represents a metric collected during runtime tracing.
    id: str  # Unique identifier for the metric.
    name: str  # Name of the metric.
    value: float  # Value of the metric.
    unit: str  # Unit of measurement for the metric.
    timestamp: datetime  # Timestamp when the metric was recorded.
    code_filepath: str = ""  # Path to the file where the metric occurred.
    code_namespace: str = ""  # Namespace of the metric.
    code_function: str = ""  # Name of the function or method.
    code_lineno: int = 0  # Line number in the file.


@dataclass
class Issue:
    # Represents an issue found during analysis.
    id: str  # Unique identifier for the issue.
    type: str  # Type of issue (e.g., SyntaxError, UncaughtException).
    severity: str  # Severity level of the issue.
    detection_method: str  # Method used to detect the issue.
    message: str  # Description of the issue.
    file: str = ""  # File where the issue occurred.
    line: int = 0  # Line number in the file.
    status: str = "open"  # Status of the issue (e.g., open, resolved).


@dataclass
class LogEntry:
    # Represents a log entry captured during runtime tracing.
    id: str  # Unique identifier for the log entry.
    message: str  # Message of the log entry.
    level: str  # Level of severity (e.g., INFO, ERROR).
    timestamp: datetime  # Timestamp when the log entry was recorded.
    trace_id: str = ""  # Identifier for the entire trace.
    span_id: str = ""  # Identifier for the span within the trace.
    code_filepath: str = ""  # Path to the file where the log occurred.
    code_namespace: str = ""  # Namespace of the log.
    code_function: str = ""  # Name of the function or method.
    code_lineno: int = 0  # Line number in the file.