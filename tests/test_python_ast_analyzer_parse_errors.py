"""A file that fails to parse must still show up in the graph as a
Module carrying its parse error, not silently vanish - a real user asked
"what is happening in tt.py" about a file with a genuine syntax error
(an unclosed `[` bracket) and got no useful answer, because the analyzer
just skipped it with no record anywhere that it had even tried.
"""

from codeatlas.analysis.python_ast import PythonAstAnalyzer


def _write_repo(root):
    (root / "good.py").write_text("def helper():\n    return 1\n")
    (root / "broken.py").write_text("def broken(:\n    pass\n")  # invalid syntax
    return root


def test_unparseable_file_recorded_as_module_with_parse_error(tmp_path):
    repo_root = _write_repo(tmp_path)

    result = PythonAstAnalyzer().analyze(str(repo_root))

    modules_by_path = {m.path: m for m in result.modules}
    assert "good.py" in modules_by_path
    assert modules_by_path["good.py"].parse_error == ""

    assert "broken.py" in modules_by_path
    assert "SyntaxError" in modules_by_path["broken.py"].parse_error


def test_abs_path_is_recorded_even_for_a_module_that_failed_to_parse(tmp_path):
    """A module's abs_path (used by the dashboard's file viewer to read
    its real source - see api/app.py's /api/file) must be populated
    whether or not the file actually parsed: a syntax error doesn't mean
    the file itself is unreadable, and it's exactly the kind of file a
    developer most wants to open and look at."""
    repo_root = _write_repo(tmp_path)

    result = PythonAstAnalyzer().analyze(str(repo_root))

    modules_by_path = {m.path: m for m in result.modules}
    assert modules_by_path["good.py"].abs_path
    assert modules_by_path["broken.py"].abs_path
    assert modules_by_path["broken.py"].abs_path.endswith("broken.py")


def test_unparseable_file_contributes_no_entities_or_calls(tmp_path):
    repo_root = _write_repo(tmp_path)

    result = PythonAstAnalyzer().analyze(str(repo_root))

    assert all(e.module_path != "broken.py" for e in result.entities)
    # The rest of the repo is still analyzed normally.
    assert any(e.id == "good.py::helper" for e in result.entities)


def test_unreadable_file_is_silently_skipped_not_recorded(tmp_path, monkeypatch):
    """Unlike a syntax error (a code problem worth reporting), a file
    CodeAtlas simply couldn't read (permissions, a race with deletion) is
    an environment issue, not something to surface as a Module."""
    repo_root = _write_repo(tmp_path)

    real_open = open

    def failing_open(path, *args, **kwargs):
        if str(path).endswith("good.py"):
            raise OSError("simulated read failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_open)
    result = PythonAstAnalyzer().analyze(str(repo_root))

    assert all(m.path != "good.py" for m in result.modules)
