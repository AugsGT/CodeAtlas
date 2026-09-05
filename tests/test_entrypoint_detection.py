"""Entry-point/dependency detection must be repository-agnostic: it
should work on a real, independently-authored repo (codeatlas_identity_test_repo)
without any special-casing, correctly report "not found" when no safe
entry point exists (sample_repo, by design, has none), and refuse to
guess when detection is ambiguous rather than picking arbitrarily.
"""

from pathlib import Path

from codeatlas.execution.entrypoint import detect_dependencies, detect_entrypoint

IDENTITY_TEST_REPO = Path(__file__).parent.parent / "codeatlas_identity_test_repo"
SAMPLE_REPO = Path(__file__).parent.parent / "sample_repo"


def test_detects_top_level_main_guarded_script():
    result = detect_entrypoint(str(IDENTITY_TEST_REPO))
    assert result.found
    assert result.script_path == "run_workload.py"
    assert result.module is None
    assert result.callable_spec is None


def test_detects_requirements_txt():
    manifest = detect_dependencies(str(IDENTITY_TEST_REPO))
    assert manifest.kind == "requirements_txt"
    assert manifest.path == "requirements.txt"


def test_sample_repo_entrypoint_is_its_run_workload_script():
    """sample_repo/pkg/service.py itself has no `__main__` guard - it's
    invoked via sample_repo/run_workload.py, the same top-level-script
    convention codeatlas_identity_test_repo uses, so /api/execute can run
    it the same way as any other repo instead of needing a dedicated
    mechanism."""
    result = detect_entrypoint(str(SAMPLE_REPO))
    assert result.found
    assert result.script_path == "run_workload.py"


def test_no_dependency_manifest_reports_none(tmp_path):
    (tmp_path / "main.py").write_text("if __name__ == '__main__':\n    pass\n")
    manifest = detect_dependencies(str(tmp_path))
    assert manifest.kind == "none"


def test_console_script_entrypoint_detected(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1"\n'
        '[project.scripts]\nmytool = "mypkg.cli:main"\n'
    )
    result = detect_entrypoint(str(tmp_path))
    assert result.found
    assert result.callable_spec == ("mypkg.cli", "main")


def test_ambiguous_console_scripts_not_guessed(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1"\n'
        '[project.scripts]\na = "pkg.a:main"\nb = "pkg.b:main"\n'
    )
    result = detect_entrypoint(str(tmp_path))
    assert not result.found
    assert "console script" in result.reason


def test_main_dunder_package_detected(tmp_path):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text("print('hi')\n")
    result = detect_entrypoint(str(tmp_path))
    assert result.found
    assert result.module == "mypkg"


def test_ambiguous_main_guarded_scripts_not_guessed(tmp_path):
    (tmp_path / "a.py").write_text("if __name__ == '__main__':\n    pass\n")
    (tmp_path / "b.py").write_text("if __name__ == '__main__':\n    pass\n")
    result = detect_entrypoint(str(tmp_path))
    assert not result.found
    assert "multiple top-level scripts" in result.reason


def test_reports_a_syntax_error_as_the_reason_when_the_only_candidate_cant_parse(tmp_path):
    """Real user confusion, found live: a file can have a genuine
    `if __name__ == '__main__':` guard and still be correctly rejected as
    an entry point if it also has a syntax error, since there's no way to
    check for (or run) a guard in a file that doesn't parse. The old
    generic "no entry point found" reason gave no hint why - this
    reproduces the exact case (a single top-level script with both) and
    checks the reason names the actual syntax error."""
    (tmp_path / "tt.py").write_text(
        "def broken(:\n    pass\n\n\nif __name__ == '__main__':\n    broken()\n"
    )

    result = detect_entrypoint(str(tmp_path))

    assert not result.found
    assert "tt.py" in result.reason
    assert "SyntaxError" in result.reason


def test_setup_py_and_conftest_are_not_treated_as_entrypoints(tmp_path):
    (tmp_path / "setup.py").write_text("if __name__ == '__main__':\n    pass\n")
    (tmp_path / "conftest.py").write_text("if __name__ == '__main__':\n    pass\n")
    result = detect_entrypoint(str(tmp_path))
    assert not result.found
