"""Static code-quality diagnostics (pyflakes-derived: unresolved
references, unused imports, suspicious constructs) are the "suspicious
constructs where analyzers support them" half of static analysis the
spec calls for - PythonAstAnalyzer's own AST walk only ever extracted
structure (modules/entities/calls/depends_on) and whether a file parses
at all, never anything about the CODE's own quality.

Covers three layers: the checker itself (analysis/quality.py), the
analyzer wiring it into AnalysisResult.diagnostics, and turning those
into real, queryable Issue nodes correctly attributed to their
containing function (analysis/python_ast.py + graph/issues.py).
"""

import ast

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.analysis.quality import check_source
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.issues import write_static_issues
from codeatlas.graph.repository import GraphRepository


def test_check_source_finds_undefined_name_and_unused_import():
    source = "import os\n\n\ndef broken():\n    return not_a_real_name\n"
    tree = ast.parse(source, filename="mod.py")

    diagnostics = check_source(tree, "mod.py")

    by_type = {d.type: d for d in diagnostics}
    assert "UnusedImport" in by_type
    assert by_type["UnusedImport"].severity == "low"
    assert "UndefinedName" in by_type
    assert by_type["UndefinedName"].severity == "high"
    assert by_type["UndefinedName"].line == 5


def test_check_source_clean_code_has_no_diagnostics():
    tree = ast.parse("def helper(a, b):\n    return a + b\n", filename="mod.py")

    assert check_source(tree, "mod.py") == []


def test_analyzer_populates_diagnostics_for_a_parsed_module(tmp_path):
    (tmp_path / "mod.py").write_text(
        "import os\n\n\ndef broken():\n    return not_a_real_name\n"
    )

    result = PythonAstAnalyzer().analyze(str(tmp_path))

    module_paths = {mp for mp, _ in result.diagnostics}
    assert module_paths == {"mod.py"}
    types = {d.type for _, d in result.diagnostics}
    assert {"UnusedImport", "UndefinedName"} <= types


def test_analyzer_does_not_run_quality_checks_on_a_file_that_failed_to_parse(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n    pass\n")

    result = PythonAstAnalyzer().analyze(str(tmp_path))

    assert result.diagnostics == []


def test_quality_issue_is_attributed_to_its_containing_entity(tmp_path):
    (tmp_path / "mod.py").write_text(
        "def clean():\n    return 1\n\n\ndef broken():\n    return not_a_real_name\n"
    )
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(tmp_path))
        write_analysis_result(repo, result)

        write_static_issues(repo, result)

        issues = {i["type"]: i for i in repo.list_issues()}
        assert issues["UndefinedName"]["entity_id"] == "mod.py::broken"
        assert issues["UndefinedName"]["module_path"] == "mod.py"
    finally:
        repo.close()


def test_quality_issue_inside_a_method_attributes_to_the_method_not_the_class(tmp_path):
    """A class's own line range spans every one of its methods too - a
    diagnostic on a line inside a method must attribute to that method
    specifically, not just the containing class (the narrower, more
    specific entity), or the evidence loses precision exactly where it
    matters most for "why does this method fail" questions. Real bug
    caught live: an UndefinedName inside Calculator.compute was
    attributed to Calculator (the class) before this fix."""
    (tmp_path / "mod.py").write_text(
        "class Calculator:\n"
        "    def compute(self, a, b):\n"
        "        total = a + c\n"
        "        return total\n"
    )
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(tmp_path))
        write_analysis_result(repo, result)

        write_static_issues(repo, result)

        issue = next(i for i in repo.list_issues() if i["type"] == "UndefinedName")
        assert issue["entity_id"] == "mod.py::Calculator.compute"
    finally:
        repo.close()


def test_quality_issue_with_no_containing_entity_still_gets_found_in_module(tmp_path):
    """A top-level unused import isn't inside any function - it should
    still be recorded and linked to its module, just with no AFFECTS
    edge to a CodeEntity."""
    (tmp_path / "mod.py").write_text("import os\n\n\ndef helper():\n    return 1\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(tmp_path))
        write_analysis_result(repo, result)

        write_static_issues(repo, result)

        issues = repo.list_issues()
        unused_import = next(i for i in issues if i["type"] == "UnusedImport")
        assert unused_import["module_path"] == "mod.py"
        assert unused_import["entity_id"] is None
    finally:
        repo.close()


def test_write_static_issues_combines_parse_errors_and_quality_diagnostics(tmp_path):
    (tmp_path / "ok.py").write_text("import os\n\n\ndef helper():\n    return 1\n")
    (tmp_path / "broken.py").write_text("def broken(:\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(tmp_path))
        write_analysis_result(repo, result)

        issues = write_static_issues(repo, result)

        types = {i.type for i in issues}
        assert "SyntaxError" in types
        assert "UnusedImport" in types
    finally:
        repo.close()
