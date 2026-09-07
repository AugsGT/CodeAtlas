"""A single Kuzu Connection is shared by the whole FastAPI app, and
FastAPI runs sync routes in a threadpool — so two requests (e.g. a
double-submitted dashboard question) can call into GraphRepository from
different threads at the same time. This reproduces exactly that and
checks it doesn't crash or corrupt results, guarding the fix in
repository.py's `_execute` lock.
"""

import threading

from codeatlas.graph.models import CodeEntity
from codeatlas.graph.repository import GraphRepository


def test_concurrent_writes_and_reads_do_not_crash(tmp_path):
    repo = GraphRepository(tmp_path / "graph.db")
    errors = []

    def worker(i):
        try:
            for j in range(20):
                repo.upsert_code_entity(CodeEntity(
                    id=f"mod.py::fn{i}_{j}",
                    qualified_name=f"mod.fn{i}_{j}",
                    name=f"fn{i}_{j}",
                    kind="function",
                    module_path="mod.py",
                    start_line=1,
                    end_line=2,
                ))
                repo.get_code_entity(f"mod.py::fn{i}_{j}")
                repo.query("MATCH (e:CodeEntity) RETURN count(e)")
        except Exception as exc:  # noqa: BLE001 - we want to see any crash, not just some
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    rows = repo.query("MATCH (e:CodeEntity) RETURN count(e)")
    assert rows[0][0] == 8 * 20
    repo.close()
