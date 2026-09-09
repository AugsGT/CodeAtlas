"""module_dependencies must surface a Module even when it has no CONTAINS
or DEPENDS_ON edges at all - the case for a file that failed to parse
(no entities could be extracted, no imports could be resolved), where
the Module.parse_error property is the one thing actually worth telling
the developer.
"""

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.repository import GraphRepository
from codeatlas.retrieval.subgraph import SubgraphRetriever


def test_module_dependencies_surfaces_parse_error_with_no_edges(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n    pass\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(tmp_path))
        write_analysis_result(repo, result)

        evidence = SubgraphRetriever(repo).retrieve("module_dependencies", module_path="broken.py")

        module_node = next(n for n in evidence.nodes if n["label"] == "Module")
        assert module_node["path"] == "broken.py"
        assert "SyntaxError" in module_node["parse_error"]
    finally:
        repo.close()


def test_module_dependencies_still_works_for_a_normal_module(tmp_path):
    (tmp_path / "clean.py").write_text("def helper():\n    return 1\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        result = PythonAstAnalyzer().analyze(str(tmp_path))
        write_analysis_result(repo, result)

        evidence = SubgraphRetriever(repo).retrieve("module_dependencies", module_path="clean.py")

        module_node = next(n for n in evidence.nodes if n["label"] == "Module")
        assert module_node.get("parse_error", "") == ""
    finally:
        repo.close()
