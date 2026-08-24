# CodeAtlas

Graph-based code and runtime intelligence tool. Combines static code
analysis with runtime telemetry in a Kuzu graph database, then answers
questions about the codebase using an LLM (Ollama) grounded in
retrieved graph evidence and validated against that evidence.

Built in 9 phases; see `codeatlas/` subpackages for the pipeline stages
(graph, analysis, telemetry, retrieval, reasoning, validation, api).

## Development process

Built through iterative pair-programming with [Claude Code](https://claude.com/claude-code)
(Anthropic's AI coding agent). My role: set the direction and made the
architectural calls (Docker sandbox with a subprocess fallback over
bare execution, Kuzu over a general-purpose graph database,
deterministic citation validation instead of a second LLM call, a
local Ollama model over a hosted API), specified requirements and
constraints at each stage, used the running dashboard myself to find
real bugs the test suite missed (several are documented inline in
commit history and code comments), and reviewed and directed every
fix. Claude Code wrote most of the implementation and tests under that
direction, iterating against my live feedback rather than a single
one-shot spec.

## Setup

Windows Command Prompt (cmd.exe) needs backslashes; bash/PowerShell accept forward slashes too.

```bat
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

## Test

```bat
.venv\Scripts\python -m pytest tests/ -v
```

## Status

**Phase 1 (Foundation) — done.** Project structure, Kuzu-backed graph
repository (`codeatlas/graph/`), schema for `Module`/`CodeEntity` nodes
and `CONTAINS`/`CALLS`/`DEPENDS_ON` edges, and a small sample repo
(`sample_repo/`) for exercising the static analyzer in Phase 3.

**Phase 2 (Identity Resolution) — done.** `CodeEntity`/`RuntimeSpan`
dataclasses (`codeatlas/graph/models.py`), span->entity normalization
(`codeatlas/graph/identity.py`) using OpenTelemetry's `code.*` semantic
convention attributes, `PRODUCES` edge, and a minimal traced-decorator
instrumentation (`codeatlas/telemetry/tracing.py`) validated against
real spans emitted by the actually-instrumented `sample_repo`.

**Phase 3 (Static Analysis) — done.** Python-only analyzer built on the
stdlib `ast` module (`codeatlas/analysis/python_ast.py`), behind a
replaceable `StaticAnalyzer` interface (`codeatlas/analysis/base.py`).
Resolves same-module calls, imported-function calls, `self.method()`
calls, and same-function local-variable method calls
(`var = Class(); var.method()`); intra-repo `DEPENDS_ON` from imports.
Documented resolution limits: no cross-repo/external-package call
resolution, no relative imports, no control-flow-aware type inference.

**Phase 4 (Runtime Telemetry) — done.** `Metric`/`LogEntry` graph nodes
and `RECORDS`/`LOGS`/`EMITS` edges. `GraphSpanExporter`,
`GraphMetricExporter`, `GraphLogExporter` (`codeatlas/telemetry/`) plug
into standard OTel SDK processors/readers and write straight into the
graph in-process (no separate collector needed, matching Kuzu's
embedded model). Logs correlate to their producing `RuntimeSpan` via
trace/span id first, falling back to `code.*`-attribute resolution.

**Phase 5 (Retrieval) — done.** `SubgraphRetriever`
(`codeatlas/retrieval/subgraph.py`): six intent-specific, depth-bounded
Cypher queries (`entity_overview`, `callers`, `callees`,
`runtime_behavior`, `module_dependencies`, `recent_errors`) built on
`MATCH p = (...) RETURN nodes(p), rels(p)`, merged into a generic
`Evidence(nodes, edges)` structure. No LLM involved; tested entirely
against a real populated graph.

**Phase 6 (Reasoning) — done.** `OllamaClient` (stdlib `urllib`, no
extra HTTP dependency) talking to a local Ollama server;
`IntentClassifier` maps a question to one of the six retrieval intents
plus a raw target string; `retrieval/resolve.py` deterministically
resolves that string to a real graph id (the LLM proposes, the graph
disposes); `GroundedReasoner` answers using only retrieved evidence and
is prompted to cite exact node ids. `ReasoningPipeline` wires all four
stages together. Defaults to `qwen2.5-coder:1.5b` for speed; tests skip
gracefully if no local Ollama server is reachable.

**Phase 7 (Validation) — done.** `codeatlas/validation/validator.py`:
deterministic citation-matching (regex-extract every `[id]` in the
answer, check it against the evidence's real node ids) flags fabricated
citations and warns when an answer cites nothing despite having
evidence. No secondary LLM semantic validator was added — citation
matching already catches the specific failure mode (the model naming
something it wasn't given), so a second model call to re-check the
first didn't add anything a deterministic check couldn't already do.

**Phase 8 (API + Dashboard) — done.** `codeatlas/api/app.py`: FastAPI
backend (`/api/health`, `/api/stats`, `/api/ingest`, `/api/ask`)
wrapping the full pipeline, factory-built per instance for test
isolation. `dashboard/`: a Vite + React single-page app (ingest panel,
question box, answer + evidence + validation display). Verified live in
a real browser against both dev servers: ingesting `sample_repo` and
asking "Who calls the add function?" correctly classified intent
`callers`, resolved the target, displayed the grounded answer with a
"Grounded" validation badge, and rendered the exact call-chain evidence.

**Phase 9 (Integration Testing) — done.**
`tests/test_full_pipeline_integration.py` runs the entire chain — source
-> static analysis -> graph -> real OTel telemetry -> identity
resolution -> retrieval -> Ollama reasoning -> validation -> API — both
directly and through the HTTP API, using the actually-instrumented
`sample_repo`. Combined with the Phase 8 dashboard walkthrough, every
stage in the pipeline diagram is now exercised end to end.

**Post-launch hardening (from real dashboard use):**
- `GroundedReasoner` now refuses deterministically ("I don't have enough
  evidence...") instead of calling the LLM at all when evidence is
  empty — a small model doesn't reliably follow "say so plainly"
  instructions with nothing to ground on, so this is a hard short-circuit,
  not a prompt tweak.
- Follow-up questions: `ReasoningPipeline.ask(question, history)` — the
  classifier sees recent turns to resolve references ("it", "that
  function"), with a deterministic carry-forward fallback in
  `pipeline.py` if the model's own target extraction misses. The API
  and dashboard now carry a bounded conversation history (last 5 turns).
- `recent_errors` now also surfaces RuntimeSpans with `status == "ERROR"`
  (an uncaught exception during a traced call, which OpenTelemetry marks
  automatically even with no log line), plus a new `slow_calls` intent
  for "what's taking too long" questions. `RuntimeSpan.error_message`
  captures the exception detail.
- `/api/run-sample-workload`: executes sample_repo's instrumented
  `Calculator.compute()` and exports whatever spans it produces
  (deliberately scoped to this repo's own bundled sample — running
  arbitrary ingested code from an API would be a real risk). This is
  what actually populates runtime evidence for the dashboard to reason
  over; `/api/ingest` alone only does static analysis. Re-executes
  `pkg/service.py` from its current bytes on every call rather than
  using `importlib.reload()` — that trusts a cached `.pyc` whenever the
  file's mtime+size look unchanged, and two quick edits (editing the
  file, then immediately clicking the button again) can land within the
  filesystem's mtime granularity and silently keep running the old
  version. See `test_run_sample_workload_picks_up_file_edits_without_restart`.
- **Real bug found via live dashboard use, not tests:** `GraphRepository`
  shared one Kuzu `Connection` across the whole FastAPI app, and FastAPI
  runs sync routes in a threadpool — a double-submitted question (two
  concurrent requests) crashed the connection. Fixed with a
  `threading.Lock` around all database access (`_execute`); see
  `tests/test_repository_concurrency.py` for the reproduction.

**Identity-resolution architecture rework (`codeatlas/graph/identity.py`,
`codeatlas/graph/paths.py`):** the original resolver required the
caller to pass a `repo_root` matching exactly what static analysis used
at ingest time, and computed entity ids purely by string-relativizing a
path against it. Ingesting a subdirectory instead of the true repo root
(a real dashboard mistake, not hypothetical) silently broke every
runtime-to-static link with no error. Resolution no longer takes a
`repo_root` at all — `resolve_code_identity()` matches on whichever
signal the graph can confirm, most reliable first:

1. **qualified name** (`code.namespace` + `code.function`, Python's own
   import-derived identity) — works whenever the analysis root happened
   to match the real `sys.path` root, independent of any path string.
2. **canonical absolute file path** (`graph/paths.py::canonical_path_key`,
   normalizing separators/case and resolving symlinks when the path
   exists) **+ local qualified name** — the fallback that saves the
   exact mis-rooted-ingest case, since two different path spellings for
   the same file reduce to the same key regardless of what root anyone
   chose.
3. **source line number** — breaks a tie between multiple otherwise-equal
   candidates; never rejects an already-unique match.

If signals disagree or remain ambiguous after all three, resolution
returns `None` rather than guessing — an incorrect link is worse than
no link. `CodeEntity` gained two columns (`abs_path`, `local_qualname`)
populated automatically by the static analyzer; `GraphSpanExporter`,
`GraphMetricExporter`, and `GraphLogExporter` no longer take a
`repo_root` constructor argument. See `tests/test_identity.py` for the
matching-tier unit tests (including deliberately cross-platform
Windows-backslash-vs-POSIX and mixed-case path pairs) and
`tests/test_identity_resolution_robustness.py`, which reproduces the
exact real bug end-to-end against the real `sample_repo` (ingest
`sample_repo/pkg` while the code actually runs with `sample_repo` on
`sys.path`) and proves it now resolves correctly with no user
intervention.

**Stale-telemetry and return-value fixes (from continued real dashboard use):**
- `/api/run-sample-workload` now calls `GraphRepository.clear_runtime_telemetry()`
  before each run: a `RuntimeSpan`'s id is a fresh random OTel span id
  every execution, so without clearing, spans from every past click
  piled up forever — including an error from a bug already fixed,
  still showing up as "current" evidence. This endpoint's whole purpose
  is "what does the code do right now," so old runs are discarded, not
  kept as history.
- `RuntimeSpan` gained a `return_value` field (`traced()` captures
  `repr()` of the return value on success, truncated at 500 chars,
  tolerant of a `__repr__` that itself raises). Motivation: a bug that
  produces the wrong result without raising anything — e.g. a missing
  `return` statement — is invisible to status/error_message alone,
  since OpenTelemetry doesn't distinguish "ran fine" from "ran fine but
  the answer is garbage." Along the way, discovered that OTel does
  *not* default a successful span's status to `OK` on its own (it stays
  `UNSET`), so `traced()` now sets it explicitly for clearer evidence.
- Discovered that several tests were quietly assuming the *exact*
  current content of `sample_repo/pkg/service.py` — but the user
  actively and continuously edits that file as a live sandbox. Any test
  needing a specific known scenario now pins its own content via
  `tests/conftest.py`'s `sample_repo_service`/`pinned_service_py_content`
  fixtures (`KNOWN_BUGGY_SERVICE_PY`, `KNOWN_WORKING_SERVICE_PY`,
  `KNOWN_SILENT_BUG_SERVICE_PY`) and restores whatever was actually on
  disk afterward, rather than depending on the live file's current state.

**`recent_activity` intent + honest intent reporting (from the "why is
there no output" report going in circles):** a vague question naming no
specific function (exactly "why is there no output") classified as
`runtime_behavior`, failed to resolve a target, and silently fell back
to `recent_errors` — which correctly found nothing, since nothing in
the silent-bug scenario actually raises. Two fixes:
- New `recent_activity` intent (`codeatlas/retrieval/subgraph.py`):
  surveys the most recent RuntimeSpans regardless of status — a strict
  superset of `recent_errors` (every error is also recent activity), so
  it's now the fallback whenever a target can't be resolved, instead of
  `recent_errors` specifically.
- `/api/ask`'s `"intent"` field now reports `result.evidence.intent`
  (what was actually queried) instead of `result.classification.intent`
  (the classifier's raw, possibly-overridden-by-fallback guess) — the
  original code was silently showing the wrong thing whenever fallback
  kicked in. The raw guess is still available as `"classified_intent"`.

`tests/test_silent_bug_evidence.py::test_ask_vague_why_no_output_question_gets_real_evidence`
reproduces the literal reported question end-to-end and confirms it now
returns real evidence instead of a repeated "no evidence" refusal.

123 backend tests passing (`pytest tests/ -v`, 1 skipped for a
symlink-permission edge case); reasoning/API tests need a local Ollama
server (`ollama serve`) and skip cleanly if unreachable.

## Runtime telemetry for an ARBITRARY repository (not just sample_repo)

`POST /api/execute {"repo_root": "..."}` detects a safe entry point in
any already-ingested Python repository and runs it automatically — no
manual tracing decorators, no source changes. It runs inside the Docker
sandbox when available (network disabled, resource limits, read-only
mount) and falls back to a restricted subprocess otherwise, always
reporting which one actually ran. See `codeatlas/execution/`.

For a repository whose execution you'd rather fully control yourself
(your own process, your own sandboxing), telemetry can also reach
CodeAtlas over the network using the standard **OTLP** protocol — the
same mechanism any real observability backend (Jaeger, an OTel
collector, Datadog) uses. CodeAtlas never executes your code in this
path; it only listens and decodes.

1. **Ingest** the repo as usual: `POST /api/ingest {"repo_root": "..."}`.
2. **Instrument** your repo's code with CodeAtlas's `traced` decorator
   (reused as a plain library import — it just calls the standard
   OpenTelemetry API) and point it at a tracer built with
   `make_otlp_tracer()`:
   ```python
   from codeatlas.telemetry.tracing import make_otlp_tracer, traced

   tracer = make_otlp_tracer(endpoint="http://localhost:8000/v1/traces")

   @traced(tracer)
   def my_function(...):
       ...
   ```
3. **Run your application yourself**, however you normally would — your
   own process, your own machine, your own sandboxing choices. CodeAtlas
   has no involvement in executing it.
4. Each traced call POSTs a real OTLP payload to `http://localhost:8000/v1/traces`
   (also `/v1/metrics`, `/v1/logs` for `make_otlp_meter`/logger
   equivalents, if added the same way).
5. `codeatlas/telemetry/otlp_receiver.py` decodes the OTLP protobuf and
   calls the same `resolve_and_link_span/metric/log()` used everywhere
   else — the exact identity resolution described above (qualified
   name, then canonical path — no `repo_root` needed) links each span to
   its `CodeEntity`.
6. Query the unified graph via `/api/ask` as normal.

No authentication on the receiver — it's meant for localhost / a
trusted network, the same posture as a local Jaeger/collector dev
setup; don't expose it publicly as-is.

Tested end-to-end for real, not just decoding logic in isolation:
`tests/test_otlp_receiver.py` proves interop using the actual standard
`opentelemetry-exporter-otlp-proto-http` exporters (the exact ones any
arbitrary instrumented repo would use); `tests/test_otlp_live_network.py`
runs a real `uvicorn` server on a real socket and sends a real span over
real HTTP end to end.

## Dashboard

```bat
cd dashboard
npm install
npm run dev
```

Dashboard runs at http://localhost:5173. In another terminal, from the
project root (`C:\Storage\CodeAtlas`), start the API it talks to:

```bat
.venv\Scripts\python -m uvicorn codeatlas.api.app:app --port 8000
```

Then: Ingest `C:\Storage\CodeAtlas\sample_repo`, click "Execute
repository" to run it automatically and generate real telemetry (no
manual instrumentation needed — if `sample_repo/pkg/service.py` calls
something undefined, that produces an ERROR-status span on purpose —
ask "What functions are failing?" to see it surfaced as evidence, or
check the Alerts panel), then ask questions and follow-ups.
