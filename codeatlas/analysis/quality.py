"""Static code-quality diagnostics via pyflakes - the "suspicious
constructs" and "unresolved references" half of static analysis that
PythonAstAnalyzer's own AST walk never attempted (it only extracts
structure: modules/entities/calls/depends_on, plus whether a file parses
at all).

Reuses pyflakes rather than reimplementing its checks, per the project's
own static-analysis language (spec: "Where static analyzers provide
diagnostics, ingest them as issues"). pyflakes needs a full, valid AST,
so this only ever runs against files that already parsed successfully -
a file that doesn't parse at all already gets its own SyntaxError issue
elsewhere (see graph/issues.py::generate_parse_error_issues) and has no tree
to check further.
"""

import ast

from pyflakes.checker import Checker

from .base import Diagnostic

# Buckets chosen by likely runtime/correctness impact, not pyflakes' own
# undifferentiated treatment of every check - e.g. `raise NotImplemented`
# (RaiseNotImplemented) or `assert (a, b)` (AssertTuple, always truthy)
# are real bugs waiting to happen, not style nits, so they're HIGH here
# even though pyflakes reports them the same way as an unused import.
# Anything not explicitly listed defaults to "medium".
_HIGH_SEVERITY_TYPES = frozenset({
    "UndefinedName", "UndefinedLocal", "UndefinedExport", "DuplicateArgument",
    "AssertTuple", "IfTuple", "RaiseNotImplemented",
    "FutureFeatureNotDefined", "LateFutureImport",
    "ForwardAnnotationSyntaxError", "DoctestSyntaxError",
    "ReturnOutsideFunction", "YieldOutsideFunction", "ContinueOutsideLoop", "BreakOutsideLoop",
    "TwoStarredExpressions", "TooManyExpressionsInStarredAssignment",
    "PercentFormatExpectedMapping", "PercentFormatExpectedSequence",
    "PercentFormatExtraNamedArguments", "PercentFormatInvalidFormat",
    "PercentFormatMissingArgument", "PercentFormatMixedPositionalAndNamed",
    "PercentFormatPositionalCountMismatch", "PercentFormatStarRequiresSequence",
    "PercentFormatUnsupportedFormatCharacter",
    "StringDotFormatExtraNamedArguments", "StringDotFormatExtraPositionalArguments",
    "StringDotFormatInvalidFormat", "StringDotFormatMissingArgument",
    "StringDotFormatMixingAutomatic",
})
_LOW_SEVERITY_TYPES = frozenset({
    "UnusedImport", "UnusedVariable", "UnusedAnnotation", "InvalidPrintSyntax",
    "FStringMissingPlaceholders", "TStringMissingPlaceholders", "UnusedIndirectAssignment",
})


def _severity_for(type_name: str) -> str:
    if type_name in _HIGH_SEVERITY_TYPES:
        return "high"
    if type_name in _LOW_SEVERITY_TYPES:
        return "low"
    return "medium"


def check_source(tree: ast.AST, filename: str) -> list[Diagnostic]:
    """Run pyflakes against an already-parsed AST (the caller has
    already parsed the file once for its own structural pass - reusing
    that tree avoids parsing twice). Returns [] if pyflakes itself can't
    process the tree, rather than raising - a quality-check failure
    shouldn't block ingest of the structural facts that already
    succeeded.
    """
    try:
        checker = Checker(tree, filename=filename)
    except Exception:  # noqa: BLE001 - pyflakes-internal failure, not our contract to enumerate
        return []

    diagnostics = []
    for msg in checker.messages:
        type_name = type(msg).__name__
        diagnostics.append(Diagnostic(
            type=type_name,
            severity=_severity_for(type_name),
            message=msg.message % msg.message_args,
            line=msg.lineno,
            col=getattr(msg, "col", 0),
        ))
    return diagnostics
