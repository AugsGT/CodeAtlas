from pathlib import Path

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.repository import GraphRepository

SAMPLE_REPO = Path(__file__).parent.parent / "sample_repo"


def test_analysis_result_round_trips_through_graph(tmp_path):
    result = PythonAstAnalyzer().analyze(str(SAMPLE_REPO))
    repo = GraphRepository(tmp_path / "graph.db")

    write_analysis_result(repo, result)

    add_entity = repo.get_code_entity("pkg/math_utils.py::add")
    assert add_entity is not None
    assert add_entity.name == "add"

    entities_in_math_utils = {e["id"] for e in repo.entities_in_module("pkg/math_utils.py")}
    assert entities_in_math_utils == {
        "pkg/math_utils.py::add",
        "pkg/math_utils.py::square",
        "pkg/math_utils.py::a_times_a",
    }

    callees = {c["callee_id"] for c in repo.callees_of("pkg/service.py::run")}
    assert callees == {"pkg/service.py::Calculator", "pkg/service.py::Calculator.compute"}

    depends = repo.query(
        "MATCH (a:Module {path: $p})-[:DEPENDS_ON]->(b:Module) RETURN b.path",
        {"p": "pkg/service.py"},
    )
    assert {row[0] for row in depends} == {"pkg/tracing_setup.py", "pkg/math_utils.py"}

    repo.close()
