"""Bounded subgraph retrieval: intent-specific Cypher queries that pull
a small, relevant slice of the graph as structured evidence, without
any LLM involvement. Phase 6's reasoner consumes Evidence objects; this
module is testable and useful entirely on its own.

Every query is written as `MATCH p = (...) RETURN nodes(p), rels(p)` so
results can be merged into a single Evidence generically: Kuzu returns
full node/relationship property dicts (tagged with `_label`) rather
than a fixed column per node kind, so one merging routine works for
every intent regardless of which node/edge kinds it touches.

Traversal depth is always bounded (a literal, validated int on the
`*1..N` pattern — Kuzu does not support parameterizing variable-length
bounds) so no query can walk the whole graph.

Two things beyond a single intent's own queries are layered on top:

  - Source snippets: a CodeEntity for a function/method carries enough to
    find it (module_path, abs_path, start_line, end_line) but not what it
    actually DOES - graph structure and timing alone can't explain a
    "why" question. `_run` reads the entity's own source text straight
    from disk and attaches it as a `source` property, so the reasoner can
    see the code, not just its shape in the graph.
  - Problem correlation: `recent_errors`/`slow_calls` (no single target)
    and `runtime_behavior` (one target) don't stop at the flagged
    entity's own evidence - they also pull in what it calls (one hop)
    and THAT callee's own runtime evidence, since a caller's slowness or
    failure is very often actually caused by something it calls (the
    project's own worked example: payment.process_payment is slow
    because database.save_transaction, which it calls, is timing out).
    Without this, those two facts show up as two disconnected pieces of
    evidence the small local model can't reliably connect on its own;
    with it, the CALLS edge and both sides' evidence are already in the
    same Evidence object.
"""

import os
from dataclasses import dataclass, field

_SOURCE_SNIPPET_MAX_LINES = 80
_SOURCE_SNIPPET_MAX_CHARS = 4000
_CORRELATION_MAX_ENTITIES = 5
_CORRELATION_MAX_CALLEES = 5


@dataclass
class Evidence:
    intent: str
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)


def _node_key(node):
    return (node["_id"]["table"], node["_id"]["offset"])


def _edge_key(rel):
    return (rel["_id"]["table"], rel["_id"]["offset"])


def _clean_node(node):
    # "label" names the node's graph type (CodeEntity, Module, ...); it's
    # kept separate from the node's own properties because CodeEntity
    # already has an unrelated "kind" property (function/class/method).
    return {
        "label": node["_label"],
        **{k: v for k, v in node.items() if not k.startswith("_") and v is not None},
    }


def _identity(clean_node):
    return clean_node.get("id", clean_node.get("path"))


def _clean_edge(rel, src_node, dst_node):
    return {
        "type": rel["_label"],
        "from": _identity(_clean_node(src_node)),
        "to": _identity(_clean_node(dst_node)),
        **{k: v for k, v in rel.items() if not k.startswith("_") and k not in ("_src", "_dst") and v is not None},
    }


def _read_source_snippet(abs_path, start_line, end_line):
    """The actual text of a function/method, for evidence that explains
    what code DOES rather than just how it's connected. Best-effort: a
    moved/deleted/unreadable file just means no snippet, not an error -
    the rest of the evidence (structure, runtime data) still stands on
    its own."""
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None

    start = max(int(start_line) - 1, 0)
    end = min(int(end_line), len(lines))
    if end <= start:
        return None

    truncated_lines = end - start > _SOURCE_SNIPPET_MAX_LINES
    if truncated_lines:
        end = start + _SOURCE_SNIPPET_MAX_LINES
    snippet = "".join(lines[start:end])

    if len(snippet) > _SOURCE_SNIPPET_MAX_CHARS:
        snippet = snippet[:_SOURCE_SNIPPET_MAX_CHARS] + "\n...(truncated)"
    elif truncated_lines:
        snippet += "\n...(truncated)"
    return snippet


def _attach_source_snippets(nodes):
    for node in nodes:
        if node.get("label") != "CodeEntity" or node.get("kind") not in ("function", "method"):
            continue
        if not node.get("abs_path") or not node.get("start_line") or not node.get("end_line"):
            continue
        snippet = _read_source_snippet(node["abs_path"], node["start_line"], node["end_line"])
        if snippet:
            node["source"] = snippet


def _merge_evidence(intent, *evidences):
    nodes = {}
    edges = {}
    for evidence in evidences:
        for node in evidence.nodes:
            key = (node.get("label"), node.get("id") or node.get("path"))
            existing = nodes.get(key)
            # Prefer a copy that already has a source snippet attached
            # over a bare one seen via a later, less-detailed query path.
            if existing is None or ("source" not in existing and "source" in node):
                nodes[key] = node
        for edge in evidence.edges:
            key = (edge.get("type"), edge.get("from"), edge.get("to"))
            edges.setdefault(key, edge)
    return Evidence(intent=intent, nodes=list(nodes.values()), edges=list(edges.values()))


class SubgraphRetriever:
    def __init__(self, repo):
        self.repo = repo
        self._intents = {
            "entity_overview": self.entity_overview,
            "callers": self.callers,
            "callees": self.callees,
            "runtime_behavior": self.runtime_behavior,
            "module_dependencies": self.module_dependencies,
            "diagnosis": self.diagnosis,
            "recent_errors": self.recent_errors,
            "slow_calls": self.slow_calls,
            "recent_activity": self.recent_activity,
        }

    def retrieve(self, intent: str, **kwargs) -> Evidence:
        handler = self._intents.get(intent)
        if handler is None:
            raise ValueError(f"Unknown retrieval intent {intent!r}. Known intents: {sorted(self._intents)}")
        return handler(**kwargs)

    def _run(self, intent, queries) -> Evidence:
        nodes = {}
        edges = {}
        for cypher, params in queries:
            for node_list, rel_list in self.repo.query(cypher, params):
                for node in node_list:
                    nodes[_node_key(node)] = _clean_node(node)
                for i, rel in enumerate(rel_list):
                    edges[_edge_key(rel)] = _clean_edge(rel, node_list[i], node_list[i + 1])
        _attach_source_snippets(nodes.values())
        return Evidence(intent=intent, nodes=list(nodes.values()), edges=list(edges.values()))

    def entity_overview(self, entity_id, call_depth=2, limit=20) -> Evidence:
        """What is this entity, what module is it in, what calls it and
        what does it call, and what runtime evidence exists for it."""
        depth = int(call_depth)
        queries = [
            ("MATCH p = (m:Module)-[:CONTAINS]->(e:CodeEntity {id: $id}) RETURN nodes(p), rels(p)",
             {"id": entity_id}),
            (f"MATCH p = (a:CodeEntity)-[:CALLS*1..{depth}]->(e:CodeEntity {{id: $id}}) RETURN nodes(p), rels(p)",
             {"id": entity_id}),
            (f"MATCH p = (e:CodeEntity {{id: $id}})-[:CALLS*1..{depth}]->(b:CodeEntity) RETURN nodes(p), rels(p)",
             {"id": entity_id}),
            ("MATCH p = (e:CodeEntity {id: $id})-[:PRODUCES]->(s:RuntimeSpan) RETURN nodes(p), rels(p) LIMIT $limit",
             {"id": entity_id, "limit": limit}),
            ("MATCH p = (i:Issue)-[:AFFECTS]->(e:CodeEntity {id: $id}) RETURN nodes(p), rels(p)",
             {"id": entity_id}),
        ]
        return self._run("entity_overview", queries)

    def callers(self, entity_id, max_depth=3) -> Evidence:
        """Who (transitively, up to max_depth call hops) calls this entity."""
        depth = int(max_depth)
        query = (
            f"MATCH p = (a:CodeEntity)-[:CALLS*1..{depth}]->(e:CodeEntity {{id: $id}}) RETURN nodes(p), rels(p)",
            {"id": entity_id},
        )
        return self._run("callers", [query])

    def callees(self, entity_id, max_depth=3) -> Evidence:
        """What this entity (transitively, up to max_depth call hops) calls."""
        depth = int(max_depth)
        query = (
            f"MATCH p = (e:CodeEntity {{id: $id}})-[:CALLS*1..{depth}]->(b:CodeEntity) RETURN nodes(p), rels(p)",
            {"id": entity_id},
        )
        return self._run("callees", [query])

    def _own_runtime_evidence(self, entity_id, limit) -> Evidence:
        """Runtime spans, metrics, and logs for exactly this entity (no
        call-graph expansion) - the reusable core both the public
        runtime_behavior() and the problem-correlation helpers below build
        on, so a callee's evidence is fetched the same way its caller's is."""
        queries = [
            ("MATCH p = (e:CodeEntity {id: $id})-[:PRODUCES]->(s:RuntimeSpan) RETURN nodes(p), rels(p) LIMIT $limit",
             {"id": entity_id, "limit": limit}),
            ("MATCH p = (e:CodeEntity {id: $id})-[:PRODUCES]->(s:RuntimeSpan)-[:EMITS]->(l:LogEntry) "
             "RETURN nodes(p), rels(p) LIMIT $limit",
             {"id": entity_id, "limit": limit}),
            ("MATCH p = (e:CodeEntity {id: $id})-[:RECORDS]->(m:Metric) RETURN nodes(p), rels(p) LIMIT $limit",
             {"id": entity_id, "limit": limit}),
            ("MATCH p = (e:CodeEntity {id: $id})-[:LOGS]->(l:LogEntry) RETURN nodes(p), rels(p) LIMIT $limit",
             {"id": entity_id, "limit": limit}),
            ("MATCH p = (i:Issue)-[:AFFECTS]->(e:CodeEntity {id: $id}) RETURN nodes(p), rels(p)",
             {"id": entity_id}),
        ]
        return self._run("runtime_behavior", queries)

    def runtime_behavior(self, entity_id, limit=20, expand_callees=True) -> Evidence:
        """Runtime spans, metrics, and logs associated with this entity,
        including logs correlated via its spans - PLUS, by default, the
        same for whatever it directly calls. A caller's own timing/status
        alone often can't explain "why": if payment.process_payment is
        slow because database.save_transaction (which it calls) is timing
        out, that only becomes visible once both sides' evidence and the
        CALLS edge between them are in the same picture."""
        evidences = [self._own_runtime_evidence(entity_id, limit)]
        if expand_callees:
            callees_evidence = self.callees(entity_id, max_depth=1)
            evidences.append(callees_evidence)
            callee_ids = sorted(
                {n["id"] for n in callees_evidence.nodes if n.get("label") == "CodeEntity"}
            )[:_CORRELATION_MAX_CALLEES]
            evidences.extend(self._own_runtime_evidence(callee_id, limit) for callee_id in callee_ids)
        return _merge_evidence("runtime_behavior", *evidences)

    def module_dependencies(self, module_path, max_hops=2) -> Evidence:
        """What this module (transitively, up to max_hops) depends on, and
        what CodeEntities it contains - plus the Module node itself
        unconditionally, not just when it happens to have dependency or
        CONTAINS edges. A module that failed to parse (see
        Module.parse_error) has neither - without this, asking about it by
        name would come back with zero evidence instead of surfacing the
        one fact that actually matters: it doesn't even parse."""
        hops = int(max_hops)
        queries = [
            ("MATCH (m:Module {path: $path}) RETURN [m], []", {"path": module_path}),
            (f"MATCH p = (m:Module {{path: $path}})-[:DEPENDS_ON*1..{hops}]->(d:Module) RETURN nodes(p), rels(p)",
             {"path": module_path}),
            ("MATCH p = (m:Module {path: $path})-[:CONTAINS]->(e:CodeEntity) RETURN nodes(p), rels(p)",
             {"path": module_path}),
            ("MATCH p = (i:Issue)-[:FOUND_IN]->(m:Module {path: $path}) RETURN nodes(p), rels(p)",
             {"path": module_path}),
        ]
        return self._run("module_dependencies", queries)

    def diagnosis(self, kind, target_id, depth=2, limit=20) -> Evidence:
        """"What's wrong with X" for one specific, named target - a file
        (kind="module") or a function/method/class (kind="entity").
        Unlike module_dependencies (structural only) or runtime_behavior
        (assumes a resolvable CodeEntity already exists), this is the
        target-scoped counterpart to recent_errors/slow_calls: it composes
        the SAME existing retrieval methods (no new Cypher primitives)
        rather than re-implementing them, bounded by `depth`/`limit` so it
        never approaches "retrieve the whole repository."

        For a module: the Module node itself (always present, including
        its parse_error if the file failed to parse at all - the one case
        where there may be no CodeEntities to investigate further), the
        entities it contains, and - for a bounded number of those entities -
        their own runtime evidence (with callee expansion) and callers.

        For an entity: its overview (containing module, immediate
        callers/callees) plus its own runtime evidence with callee
        expansion - i.e. entity_overview and runtime_behavior merged, since
        a diagnosis question needs both the structural and runtime picture
        together, not one or the other.
        """
        if kind == "module":
            return self._diagnose_module(target_id, depth=depth, limit=limit)
        return self._diagnose_entity(target_id, depth=depth, limit=limit)

    def _diagnose_module(self, module_path, depth, limit) -> Evidence:
        base = self._run("diagnosis", [
            ("MATCH (m:Module {path: $path}) RETURN [m], []", {"path": module_path}),
            ("MATCH p = (m:Module {path: $path})-[:CONTAINS]->(e:CodeEntity) RETURN nodes(p), rels(p)",
             {"path": module_path}),
            ("MATCH p = (i:Issue)-[:FOUND_IN]->(m:Module {path: $path}) RETURN nodes(p), rels(p)",
             {"path": module_path}),
        ])
        entity_ids = sorted(
            {n["id"] for n in base.nodes if n.get("label") == "CodeEntity"}
        )[:_CORRELATION_MAX_ENTITIES]

        evidences = [base]
        for entity_id in entity_ids:
            evidences.append(self.runtime_behavior(entity_id, limit=limit, expand_callees=True))
            evidences.append(self.callers(entity_id, max_depth=1))
        # A module with no entities at all (e.g. one that failed to parse)
        # has nothing further to correlate - `base` (Module + parse_error,
        # if any) is the complete, honest picture: there's no static AST
        # to have found functions in, so there's nothing to investigate
        # beyond the parse failure itself.
        return _merge_evidence("diagnosis", *evidences)

    def _diagnose_entity(self, entity_id, depth, limit) -> Evidence:
        evidences = [
            self.entity_overview(entity_id, call_depth=depth, limit=limit),
            self.runtime_behavior(entity_id, limit=limit, expand_callees=True),
        ]
        return _merge_evidence("diagnosis", *evidences)

    def recent_errors(self, limit=20) -> Evidence:
        """Recent problems from every angle the graph can surface them:
        ERROR/WARN log entries (correlated back to their CodeEntity via
        span or directly), RuntimeSpans that failed outright (an
        uncaught exception during a traced call, which OpenTelemetry
        marks as an ERROR-status span even with no log line at all), and
        every critical/high-severity Issue regardless of whether it came
        from static analysis or execution - this is what lets a vague,
        no-target "why does this fail" question surface a syntax error
        even when the repo has never been executed at all (a real gap:
        the queries above only ever look at runtime evidence, so a
        static-only problem was previously invisible to this intent) -
        PLUS, for each flagged entity, what it calls and who calls it (see
        _correlate_problem_entities), since a failure is very often caused
        by something the failing function itself called."""
        levels = ["ERROR", "WARN"]
        queries = [
            ("MATCH p = (e:CodeEntity)-[:PRODUCES]->(s:RuntimeSpan)-[:EMITS]->(l:LogEntry) "
             "WHERE l.level IN $levels RETURN nodes(p), rels(p) ORDER BY l.timestamp DESC LIMIT $limit",
             {"levels": levels, "limit": limit}),
            ("MATCH p = (e:CodeEntity)-[:LOGS]->(l:LogEntry) "
             "WHERE l.level IN $levels RETURN nodes(p), rels(p) ORDER BY l.timestamp DESC LIMIT $limit",
             {"levels": levels, "limit": limit}),
            ("MATCH p = (e:CodeEntity)-[:PRODUCES]->(s:RuntimeSpan) "
             "WHERE s.status = 'ERROR' RETURN nodes(p), rels(p) ORDER BY s.start_time DESC LIMIT $limit",
             {"limit": limit}),
            ("MATCH (i:Issue) WHERE i.severity IN ['critical', 'high'] "
             "RETURN [i], [] LIMIT $limit",
             {"limit": limit}),
        ]
        primary = self._run("recent_errors", queries)
        return self._correlate_problem_entities(primary, limit)

    def slow_calls(self, threshold_ms=100, limit=20) -> Evidence:
        """RuntimeSpans that took longer than threshold_ms, correlated back
        to the CodeEntity that produced them, slowest first - PLUS, for
        each flagged entity, what it calls and who calls it (see
        _correlate_problem_entities), since the actual bottleneck is often
        one call deeper than the slow entity itself."""
        query = (
            "MATCH p = (e:CodeEntity)-[:PRODUCES]->(s:RuntimeSpan) "
            "WHERE s.duration_ms > $threshold_ms "
            "RETURN nodes(p), rels(p) ORDER BY s.duration_ms DESC LIMIT $limit",
            {"threshold_ms": float(threshold_ms), "limit": limit},
        )
        primary = self._run("slow_calls", [query])
        return self._correlate_problem_entities(primary, limit)

    def _correlate_problem_entities(self, primary: Evidence, limit) -> Evidence:
        """For each CodeEntity a problem-detection query flagged, pull in
        its immediate callees (with THEIR own runtime evidence - the
        likely root cause) and callers (who's affected) too, so a chain of
        related problems shows up as one connected picture with the CALLS
        edges already in it, instead of several separate facts the small
        local model would otherwise have to guess how to connect.
        Bounded to the first few flagged entities so this can't blow up
        evidence size on a graph with many simultaneous problems."""
        entity_ids = sorted(
            {n["id"] for n in primary.nodes if n.get("label") == "CodeEntity"}
        )[:_CORRELATION_MAX_ENTITIES]
        if not entity_ids:
            return primary

        evidences = [primary]
        for entity_id in entity_ids:
            evidences.append(self.runtime_behavior(entity_id, limit=limit, expand_callees=True))
            evidences.append(self.callers(entity_id, max_depth=1))
        return _merge_evidence(primary.intent, *evidences)

    def recent_activity(self, limit=20) -> Evidence:
        """The most recent RuntimeSpans regardless of outcome, correlated
        back to their CodeEntity - for vague "what happened", "why is
        there no output", "what did it return" questions that name no
        specific function. recent_errors only surfaces failures; this
        surfaces everything, since a call that "succeeded" (no exception)
        can still have produced the wrong - or no - result.

        Also includes every Issue in the graph regardless of severity -
        the broadest survey intent should be able to say "here's what's
        wrong" even on a repository that's never been executed at all
        (zero RuntimeSpans) but does have a static problem sitting in
        evidence. A real, live-reported gap: "what happened" on a
        syntax-error-only repo answered "I don't have enough evidence"
        despite the exact same syntax error already being visible in the
        Alerts panel - this intent just never looked at Issue nodes."""
        queries = [
            ("MATCH p = (e:CodeEntity)-[:PRODUCES]->(s:RuntimeSpan) "
             "RETURN nodes(p), rels(p) ORDER BY s.start_time DESC LIMIT $limit",
             {"limit": limit}),
            ("MATCH (i:Issue) RETURN [i], [] LIMIT $limit", {"limit": limit}),
        ]
        return self._run("recent_activity", queries)
