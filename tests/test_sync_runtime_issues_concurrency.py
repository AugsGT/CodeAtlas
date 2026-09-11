"""sync_runtime_issues does a non-transactional clear-then-rebuild
(several separate Cypher statements) - two overlapping calls from
different threads (e.g. the dashboard's own /api/alerts and /api/issues
firing close together right after an execute, which is exactly what
FastAPI's threadpool allows) can interleave their clears and inserts and
leave the graph in a genuinely inconsistent Issue count - observed live
as /api/stats' Issue count disagreeing with /api/alerts' own count.

This verifies the mutual-exclusion guarantee directly (never more than
one thread inside the clear-rebuild body at once) rather than asserting
on a final row count: a final-count assertion can happen to converge to
the same answer whether or not the lock is there (deterministic ids mean
a later full rebuild just overwrites an earlier interleaved one), so it
doesn't reliably fail without the fix - confirmed empirically before
writing this version. Instrumenting entry/exit with a wide artificial
delay does reliably fail without the lock.
"""

import datetime
import threading
import time

from codeatlas.analysis.python_ast import PythonAstAnalyzer
from codeatlas.graph import issues as issues_module
from codeatlas.graph.ingest import write_analysis_result
from codeatlas.graph.issues import sync_runtime_issues
from codeatlas.graph.repository import GraphRepository

NOW = datetime.datetime.now(datetime.timezone.utc)


def test_sync_runtime_issues_never_runs_concurrently(tmp_path, monkeypatch):
    (tmp_path / "mod.py").write_text("def helper():\n    return 1\n")
    repo = GraphRepository(tmp_path / "graph.db")
    try:
        write_analysis_result(repo, PythonAstAnalyzer().analyze(str(tmp_path)))

        state_lock = threading.Lock()
        concurrent_calls = 0
        max_concurrent = 0
        real_locked_body = issues_module._sync_runtime_issues_locked

        def instrumented(*args, **kwargs):
            nonlocal concurrent_calls, max_concurrent
            with state_lock:
                concurrent_calls += 1
                max_concurrent = max(max_concurrent, concurrent_calls)
            try:
                time.sleep(0.05)  # widen the window so real overlap would be observed, not just theoretical
                return real_locked_body(*args, **kwargs)
            finally:
                with state_lock:
                    concurrent_calls -= 1

        monkeypatch.setattr(issues_module, "_sync_runtime_issues_locked", instrumented)

        threads = [threading.Thread(target=lambda: sync_runtime_issues(repo)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert max_concurrent == 1
    finally:
        repo.close()
