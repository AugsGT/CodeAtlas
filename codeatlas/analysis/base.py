"""Interface every static-analysis backend implements.

Only one backend (PythonAstAnalyzer) exists today, but the interface is
kept separate from it so a second language's analyzer can be added
later without touching callers of analyze().
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..graph.models import CodeEntity, Module


@dataclass
class CallEdge:
    caller_id: str
    callee_id: str
    call_line: int


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
    type: str  # the underlying tool's own check name, e.g. "UndefinedName"
    severity: str  # "high" | "medium" | "low"
    message: str
    line: int
    col: int = 0


@dataclass
class AnalysisResult:
    modules: list[Module] = field(default_factory=list)
    entities: list[CodeEntity] = field(default_factory=list)
    contains: list[tuple[str, str]] = field(default_factory=list)  # (module_path, entity_id)
    calls: list[CallEdge] = field(default_factory=list)
    depends_on: list[tuple[str, str]] = field(default_factory=list)  # (from_path, to_path)
    diagnostics: list[tuple[str, Diagnostic]] = field(default_factory=list)  # (module_path, Diagnostic)


class StaticAnalyzer(ABC):
    @abstractmethod
    def analyze(self, repo_root: str) -> AnalysisResult:
        """Analyze all source files under repo_root and return the graph
        data (modules, entities, and their relationships) found."""
