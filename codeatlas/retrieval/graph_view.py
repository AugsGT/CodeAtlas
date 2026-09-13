"""Whole-graph view for the dashboard's graph visualization panel (spec
section 17, deferred until now as a separate, larger feature).

Unlike every other retrieval method here, this isn't bounded to one
question's evidence - it returns the full current structural graph
(every Module/CodeEntity and the edges between them) for the dashboard
to render directly. Safe to leave unbounded: CodeAtlas only ever holds
one ingested repository's data at a time (a fresh /api/ingest replaces
the static graph rather than merging onto it - see
GraphRepository.clear_static_graph), so this is naturally sized to one
repository, not the whole database's lifetime history.

Issue nodes are deliberately not rendered as their own graph nodes (that
would roughly double node count on a repo with many low-severity quality
diagnostics, for little visual benefit at this scale) - instead, the
Module/CodeEntity they're FOUND_IN/AFFECTS is annotated with the most
severe linked issue, so the graph shows STRUCTURE with problems
highlighted on top of it, matching how the rest of the dashboard treats
issues as a property of the thing they're about rather than a separate
thing to look at.
"""

_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _more_severe(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a if _SEVERITY_RANK.get(a, 99) <= _SEVERITY_RANK.get(b, 99) else b


def build_repo_graph(repo) -> dict:
    nodes = {}
    edges = []

    for path, name, parse_error in repo.query("MATCH (m:Module) RETURN m.path, m.name, m.parse_error"):
        node_id = f"module:{path}"
        nodes[node_id] = {
            "id": node_id, "kind": "Module", "label": name or path, "path": path,
            "severity": "critical" if parse_error else None,
        }

    for entity_id, name, kind, module_path, local_qualname in repo.query(
        "MATCH (e:CodeEntity) RETURN e.id, e.name, e.kind, e.module_path, e.local_qualname"
    ):
        node_id = f"entity:{entity_id}"
        nodes[node_id] = {
            "id": node_id, "kind": "CodeEntity", "entity_kind": kind,
            "label": local_qualname or name, "module_path": module_path, "severity": None,
        }

    for module_path, entity_id in repo.query("MATCH (m:Module)-[:CONTAINS]->(e:CodeEntity) RETURN m.path, e.id"):
        edges.append({"source": f"module:{module_path}", "target": f"entity:{entity_id}", "type": "CONTAINS"})

    for caller_id, callee_id in repo.query("MATCH (a:CodeEntity)-[:CALLS]->(b:CodeEntity) RETURN a.id, b.id"):
        edges.append({"source": f"entity:{caller_id}", "target": f"entity:{callee_id}", "type": "CALLS"})

    for from_path, to_path in repo.query("MATCH (a:Module)-[:DEPENDS_ON]->(b:Module) RETURN a.path, b.path"):
        edges.append({"source": f"module:{from_path}", "target": f"module:{to_path}", "type": "DEPENDS_ON"})

    for module_path, severity in repo.query("MATCH (i:Issue)-[:FOUND_IN]->(m:Module) RETURN m.path, i.severity"):
        node = nodes.get(f"module:{module_path}")
        if node:
            node["severity"] = _more_severe(node["severity"], severity)

    for entity_id, severity in repo.query("MATCH (i:Issue)-[:AFFECTS]->(e:CodeEntity) RETURN e.id, i.severity"):
        node = nodes.get(f"entity:{entity_id}")
        if node:
            node["severity"] = _more_severe(node["severity"], severity)

    return {"nodes": list(nodes.values()), "edges": edges}
