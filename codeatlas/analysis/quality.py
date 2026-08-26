# This file contains functions to analyze the quality of Python code using pyflakes.
# Pyflakes checks for suspicious constructs and unresolved references in Python code.

import ast

from pyflakes.checker import Checker

from .base import Diagnostic

# Buckets chosen by likely runtime/correctness impact, not pyflakes' own
# undifferentiated treatment of every check. For example, 'raise NotImplemented'
# (RaiseNotImplemented) and 'assert (a, b)' (AssertTuple, always truthy)
# are real bugs waiting to happen, so they're considered high severity.
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
    """Determine the severity level of a diagnostic type."""
    if type_name in _HIGH_SEVERITY_TYPES:
        return "high"
    if type_name in _LOW_SEVERITY_TYPES:
        return "low"
    return "medium"

def check_source(tree: ast.AST, filename: str) -> list[Diagnostic]:
    """Run pyflakes against an already-parsed AST. Returns a list of diagnostics.
    
    Args:
        tree (ast.AST): The parsed abstract syntax tree of the Python code.
        filename (str): The name of the file being checked.

    Returns:
        list[Diagnostic]: A list of diagnostic objects containing information about issues found in the code.
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