# Persist a static-analysis AnalysisResult into the graph.

from ..analysis.base import AnalysisResult
from .repository import GraphRepository


def write_analysis_result(repo: GraphRepository, result: AnalysisResult) -> None:
    # Iterate through each module in the analysis result and upsert it into the repository.
    for module in result.modules:
        repo.upsert_module(module)
    
    # Iterate through each entity in the analysis result and upsert it into the repository.
    for entity in result.entities:
        repo.upsert_code_entity(entity)
    
    # Iterate through each (module path, entity ID) pair in the analysis result and add the relationship to the repository.
    for module_path, entity_id in result.contains:
        repo.add_contains(module_path, entity_id)
    
    # Iterate through each call in the analysis result and add the call relationship to the repository.
    for call in result.calls:
        repo.add_calls(call.caller_id, call.callee_id, call.call_line)
    
    # Iterate through each (from path, to path) pair in the analysis result and add the dependency relationship to the repository.
    for from_path, to_path in result.depends_on:
        repo.add_depends_on(from_path, to_path)