"""A persistent database created before Module.parse_error existed must
keep working once the code is upgraded, not crash the next time
GraphRepository opens it - schema changes need a migration path, not
just a new CREATE TABLE definition that only helps brand-new databases.
"""

import kuzu

from codeatlas.graph.models import Module
from codeatlas.graph.repository import GraphRepository


def test_opening_a_pre_existing_database_adds_missing_columns(tmp_path):
    db_path = tmp_path / "graph.db"

    # Simulate a database created by an older version of the schema, with
    # no parse_error column on Module at all.
    db = kuzu.Database(str(db_path))
    conn = kuzu.Connection(db)
    conn.execute("CREATE NODE TABLE Module(path STRING, name STRING, language STRING, PRIMARY KEY (path))")
    conn.execute("CREATE (m:Module {path: 'old.py', name: 'old.py', language: 'python'})")
    conn.close()
    db.close()

    # Opening it through GraphRepository (as the real app does on startup)
    # must migrate the table in place, not error out.
    repo = GraphRepository(str(db_path))
    try:
        pre_existing = repo.get_module("old.py")
        assert pre_existing is not None
        assert pre_existing.parse_error == ""  # migrated column defaults to empty, not None/crash

        repo.upsert_module(Module(path="new.py", name="new.py", language="python", parse_error="SyntaxError: x"))
        assert repo.get_module("new.py").parse_error == "SyntaxError: x"
    finally:
        repo.close()


def test_opening_a_database_missing_module_abs_path_adds_it(tmp_path):
    """Same migration story, for the abs_path column added when the
    dashboard's file viewer needed a durable way to read a module's real
    source without the caller resupplying repo_root."""
    db_path = tmp_path / "graph.db"

    db = kuzu.Database(str(db_path))
    conn = kuzu.Connection(db)
    conn.execute(
        "CREATE NODE TABLE Module(path STRING, name STRING, language STRING, "
        "parse_error STRING, PRIMARY KEY (path))"
    )
    conn.execute("CREATE (m:Module {path: 'old.py', name: 'old.py', language: 'python', parse_error: ''})")
    conn.close()
    db.close()

    repo = GraphRepository(str(db_path))
    try:
        pre_existing = repo.get_module("old.py")
        assert pre_existing is not None
        assert pre_existing.abs_path == ""

        repo.upsert_module(Module(path="new.py", name="new.py", language="python", abs_path="/abs/new.py"))
        assert repo.get_module("new.py").abs_path == "/abs/new.py"
    finally:
        repo.close()


def test_running_schema_creation_twice_is_a_no_op(tmp_path):
    """Opening the SAME already-current-schema database a second time
    (e.g. a server restart) must not error just because the column - and
    the migration that adds it - both already exist."""
    db_path = tmp_path / "graph.db"

    repo1 = GraphRepository(str(db_path))
    repo1.close()

    repo2 = GraphRepository(str(db_path))  # re-opening must not raise
    repo2.close()
