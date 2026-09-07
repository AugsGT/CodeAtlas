from codeatlas.graph.models import CodeEntity, Module
from codeatlas.graph.repository import GraphRepository
from codeatlas.retrieval.resolve import resolve_target


def make_repo(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    repo.upsert_module(Module(path="pkg/math_utils.py", name="math_utils", language="python"))
    repo.upsert_code_entity(CodeEntity(
        id="pkg/math_utils.py::add",
        qualified_name="pkg.math_utils.add",
        name="add",
        kind="function",
        module_path="pkg/math_utils.py",
        start_line=1,
        end_line=2,
    ))
    return repo


def test_resolves_exact_entity_id(tmp_path):
    repo = make_repo(tmp_path)
    assert resolve_target(repo, "pkg/math_utils.py::add") == ("entity", "pkg/math_utils.py::add")
    repo.close()


def test_resolves_exact_module_path(tmp_path):
    repo = make_repo(tmp_path)
    assert resolve_target(repo, "pkg/math_utils.py") == ("module", "pkg/math_utils.py")
    repo.close()


def test_resolves_by_simple_name(tmp_path):
    repo = make_repo(tmp_path)
    assert resolve_target(repo, "add") == ("entity", "pkg/math_utils.py::add")
    repo.close()


def test_resolves_by_qualified_name(tmp_path):
    repo = make_repo(tmp_path)
    assert resolve_target(repo, "pkg.math_utils.add") == ("entity", "pkg/math_utils.py::add")
    repo.close()


def test_empty_name_returns_none(tmp_path):
    repo = make_repo(tmp_path)
    assert resolve_target(repo, "") == (None, None)
    repo.close()


def test_unknown_name_returns_none(tmp_path):
    repo = make_repo(tmp_path)
    assert resolve_target(repo, "does_not_exist") == (None, None)
    repo.close()


def test_exact_name_match_wins_over_ambiguous_substring(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_code_entity(CodeEntity(
        id="pkg/math_utils.py::add_all",
        qualified_name="pkg.math_utils.add_all",
        name="add_all",
        kind="function",
        module_path="pkg/math_utils.py",
        start_line=3,
        end_line=4,
    ))
    # "add" exactly matches one entity's name, even though it's also a
    # substring of "add_all" - the exact match takes priority.
    assert resolve_target(repo, "add") == ("entity", "pkg/math_utils.py::add")
    # "add_a" matches no name exactly but is an unambiguous substring of add_all.
    assert resolve_target(repo, "add_a") == ("entity", "pkg/math_utils.py::add_all")
    repo.close()
