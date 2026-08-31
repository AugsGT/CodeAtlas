# This file resolves a free-text name to an actual CodeEntity ID or Module path.
# It separates the resolution logic from the LLM (Language Model), ensuring that the graph decides whether the name refers to something real.

def resolve_target(repo, name: str):
    """Determines if a given name corresponds to a CodeEntity or Module in the repository.
    
    Args:
        repo: The repository object containing code entities and modules.
        name: The name to be resolved as either an entity or module.
        
    Returns:
        A tuple (kind, id) where kind is "entity" or "module", or (None, None) if no match is found.
    """
    # Check if the name is empty
    if not name:
        return None, None

    # Try to find a CodeEntity by exact name
    entity = repo.get_code_entity(name)
    if entity is not None:
        return "entity", name

    # Try to find a Module by exact name
    module = repo.get_module(name)
    if module is not None:
        return "module", name

    # Query for CodeEntities with an exact match on name or qualified_name
    rows = repo.query(
        "MATCH (e:CodeEntity) WHERE e.name = $name OR e.qualified_name = $name RETURN DISTINCT e.id",
        {"name": name},
    )
    if len(rows) == 1:
        return "entity", rows[0][0]

    # Query for CodeEntities where the name contains the given name
    rows = repo.query(
        "MATCH (e:CodeEntity) WHERE e.name CONTAINS $name OR e.qualified_name CONTAINS $name RETURN DISTINCT e.id",
        {"name": name},
    )
    if len(rows) == 1:
        return "entity", rows[0][0]

    # Query for Modules where the path contains the given name or the module name matches exactly
    rows = repo.query(
        "MATCH (m:Module) WHERE m.path CONTAINS $name OR m.name = $name RETURN DISTINCT m.path",
        {"name": name},
    )
    if len(rows) == 1:
        return "module", rows[0][0]

    # If no matches are found, return None for both kind and id
    return None, None