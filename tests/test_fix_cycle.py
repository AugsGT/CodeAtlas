"""The fix-validation cycle (spec section 14): apply a proposed
improved_code snippet to a disposable copy of the repository, re-run it
for real, and report whether the target entity's previously-observed
failure is actually gone. Uses real subprocess execution (the same
fallback /api/execute uses when Docker isn't available), not a mock -
the entire point of this feature is that it re-runs the code for real
rather than trusting the LLM's own claim that a fix "should" work.
"""

import datetime

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.graph.identity import resolve_and_link_span
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.models import CodeEntity, RuntimeSpan
from codeatlas.graph.repository import GraphRepository
import os

from codeatlas.validation.fix_cycle import apply_fix_to_repository, apply_patch_to_temp_workspace, validate_fix

NOW = datetime.datetime.now(datetime.timezone.utc)

DIVIDE_MAIN_PY = (
    "def divide(a, b):\n"
    "    return a / b\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    divide(10, 0)\n"
)

FIXED_DIVIDE = (
    "def divide(a, b):\n"
    "    if b == 0:\n"
    "        return 0\n"
    "    return a / b\n"
)

STILL_BROKEN_DIVIDE = "def divide(a, b):\n    return a / b\n"


def _seed_repo_with_before_error(tmp_path):
    """The repo root (containing only the code to be copied/executed)
    must be a directory of its own, separate from where the Kuzu db
    lives - apply_patch_to_temp_workspace copies the entire repo root
    into the workspace, and a graph.db sitting inside it would get
    dragged along (and risk a Windows file-lock conflict with the
    Connection still holding it open)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "main.py").write_text(DIVIDE_MAIN_PY)

    repo = GraphRepository(tmp_path / "graph.db")
    result = PythonAstAnalyzer().analyze(str(repo_root))
    write_analysis_result(repo, result)

    span = RuntimeSpan(
        id="s-before", trace_id="t1", name="divide", start_time=NOW, end_time=NOW,
        duration_ms=1.0, status="ERROR", error_message="ZeroDivisionError: division by zero",
        code_filepath=str(repo_root / "main.py"), code_function="divide",
    )
    resolve_and_link_span(repo, span)
    return repo, repo_root


def test_apply_patch_replaces_entity_lines_and_reindents(tmp_path):
    # repo_root must be its own directory, separate from graph.db - see
    # _seed_repo_with_before_error's docstring: copytree would otherwise
    # try to copy the live Kuzu db file, which can hit a Windows file
    # lock conflict while the Connection still holds it open.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "mod.py").write_text(
        "class Calculator:\n"
        "    def compute(self, a, b):\n"
        "        return a + c\n"
    )
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(repo_root))
        write_analysis_result(repo, result)
        entity = repo.get_code_entity("mod.py::Calculator.compute")

        workspace = apply_patch_to_temp_workspace(
            str(repo_root), entity, "def compute(self, a, b):\n    return a + b\n"
        )

        patched = (workspace + "/mod.py")
        with open(patched, encoding="utf-8") as f:
            content = f.read()
        assert "return a + b" in content
        assert "return a + c" not in content
        # Re-indented to match the original method's own indentation (4 spaces, nested in a class).
        assert "    def compute(self, a, b):" in content
        assert "        return a + b" in content

        # The original file on disk must be untouched.
        with open(repo_root / "mod.py", encoding="utf-8") as f:
            original_content = f.read()
        assert "return a + c" in original_content
    finally:
        repo.close()


def test_apply_patch_raises_for_unknown_module_path(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "mod.py").write_text("def helper():\n    return 1\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        fake_entity = CodeEntity(
            id="missing.py::helper", qualified_name="missing.helper", name="helper",
            kind="function", module_path="missing.py", start_line=1, end_line=1,
        )
        try:
            apply_patch_to_temp_workspace(str(repo_root), fake_entity, "def helper():\n    return 2\n")
            assert False, "expected ValueError"
        except ValueError as exc:
            assert "not found" in str(exc)
    finally:
        repo.close()


def test_validate_fix_reports_no_improved_code_provided(tmp_path):
    repo, repo_root = _seed_repo_with_before_error(tmp_path)
    try:
        result = validate_fix(repo, str(repo_root), "main.py::divide", "")

        assert result.attempted is False
        assert result.resolved is None
        assert "no improved_code" in result.reason
    finally:
        repo.close()


def test_validate_fix_reports_unknown_entity(tmp_path):
    repo, repo_root = _seed_repo_with_before_error(tmp_path)
    try:
        result = validate_fix(repo, str(repo_root), "main.py::does_not_exist", "def x(): pass")

        assert result.attempted is False
        assert "unknown entity" in result.reason
    finally:
        repo.close()


def test_validate_fix_confirms_a_real_fix_resolves_the_failure(tmp_path):
    repo, repo_root = _seed_repo_with_before_error(tmp_path)
    try:
        result = validate_fix(repo, str(repo_root), "main.py::divide", FIXED_DIVIDE)

        assert result.attempted is True
        assert result.before_status == "ERROR"
        assert result.after_status == "OK"
        assert result.resolved is True
        assert "successfully" in result.explanation
    finally:
        repo.close()


def test_validate_fix_detects_a_fix_that_does_not_resolve_the_failure(tmp_path):
    repo, repo_root = _seed_repo_with_before_error(tmp_path)
    try:
        result = validate_fix(repo, str(repo_root), "main.py::divide", STILL_BROKEN_DIVIDE)

        assert result.attempted is True
        assert result.before_status == "ERROR"
        assert result.after_status == "ERROR"
        assert result.resolved is False
        assert "Still failing" in result.explanation
    finally:
        repo.close()


def test_validate_fix_does_not_modify_the_original_repository(tmp_path):
    repo, repo_root = _seed_repo_with_before_error(tmp_path)
    try:
        validate_fix(repo, str(repo_root), "main.py::divide", FIXED_DIVIDE)

        with open(repo_root / "main.py", encoding="utf-8") as f:
            content = f.read()
        assert content == DIVIDE_MAIN_PY
    finally:
        repo.close()


def test_apply_fix_writes_the_real_file_and_backs_up_the_original(tmp_path):
    repo, repo_root = _seed_repo_with_before_error(tmp_path)
    try:
        result = apply_fix_to_repository(repo, str(repo_root), "main.py::divide", FIXED_DIVIDE)

        assert result.applied is True
        assert result.file == "main.py"
        assert os.path.isfile(result.backup_path)

        with open(repo_root / "main.py", encoding="utf-8") as f:
            new_content = f.read()
        assert "if b == 0" in new_content
        assert new_content != DIVIDE_MAIN_PY

        with open(result.backup_path, encoding="utf-8") as f:
            backup_content = f.read()
        assert backup_content == DIVIDE_MAIN_PY
    finally:
        repo.close()


def test_apply_fix_reports_no_improved_code_provided(tmp_path):
    repo, repo_root = _seed_repo_with_before_error(tmp_path)
    try:
        result = apply_fix_to_repository(repo, str(repo_root), "main.py::divide", "")

        assert result.applied is False
        assert "no improved_code" in result.reason
        with open(repo_root / "main.py", encoding="utf-8") as f:
            assert f.read() == DIVIDE_MAIN_PY
    finally:
        repo.close()


def test_apply_fix_reports_unknown_entity(tmp_path):
    repo, repo_root = _seed_repo_with_before_error(tmp_path)
    try:
        result = apply_fix_to_repository(repo, str(repo_root), "main.py::does_not_exist", "def x(): pass")

        assert result.applied is False
        assert "unknown entity" in result.reason
    finally:
        repo.close()


def test_apply_fix_does_not_touch_the_file_when_the_entity_line_range_is_out_of_bounds(tmp_path, monkeypatch):
    repo, repo_root = _seed_repo_with_before_error(tmp_path)
    try:
        stale_entity = repo.get_code_entity("main.py::divide")
        stale_entity.start_line = 999
        stale_entity.end_line = 1000
        # Force the out-of-bounds path deterministically, simulating the
        # file having changed since the entity's line range was recorded.
        monkeypatch.setattr(repo, "get_code_entity", lambda entity_id: stale_entity)

        result = apply_fix_to_repository(repo, str(repo_root), "main.py::divide", FIXED_DIVIDE)

        assert result.applied is False
        assert "out of bounds" in result.reason
        with open(repo_root / "main.py", encoding="utf-8") as f:
            assert f.read() == DIVIDE_MAIN_PY
    finally:
        repo.close()
