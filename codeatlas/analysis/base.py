from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..graph.models import CodeEntity, Module


@dataclass
class CallEdge:
    caller_id: str  # ID of the function or method that calls another
    callee_id: str  # ID of the function or method being called
    call_line: int  # Line number where the call is made


@dataclass
class Diagnostic:
    """A single static-analysis finding beyond structure/parse-failure -
    a suspicious construct, unresolved reference, unused import, or
    similar - independent of which underlying tool produced it (Python's
    analyzer uses pyflakes; a future language's analyzer could plug in
    something else and still produce these). Kept generic here rather
    than in a Python-specific module since any StaticAnalyzer
    implementation might want to emit this shape.
    """
    type: str  # Name of the check from the underlying tool, e.g. "UndefinedName"
    severity: str  # Severity level: "high", "medium", or "low"
    message: str  # Description of the issue
    line: int  # Line number where the issue occurs
    col: int = 0  # Column number where the issue occurs


@dataclass
class AnalysisResult:
    modules: list[Module] = field(default_factory=list)  # List of modules found in the repository
    entities: list[CodeEntity] = field(default_factory=list)  # List of code entities (functions, classes, etc.)
    contains: list[tuple[str, str]] = field(default_factory=list)  # List of tuples indicating which module contains which entity
    calls: list[CallEdge] = field(default_factory=list)  # List of call edges between functions or methods
    depends_on: list[tuple[str, str]] = field(default_factory=list)  # List of dependencies between modules
    diagnostics: list[tuple[str, Diagnostic]] = field(default_factory=list)  # List of diagnostics for each module


class StaticAnalyzer(ABC):
    @abstractmethod
    def analyze(self, repo_root: str) -> AnalysisResult:
        """Analyze all source files under the given repository root and return the graph data (modules, entities, and their relationships) found."""