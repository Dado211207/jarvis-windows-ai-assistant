"""Automated proof that db/migrations.py's create_tables() is a safe,
additive upgrade — the actual scenario a real user's existing data
directory goes through when they update to v0.2. This was previously only
checked by an ad-hoc manual script during development; it belongs in the
suite so it can't silently regress.
"""

import sqlite3

from db.migrations import create_tables

_PRE_V02_SCHEMA = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    tags TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE action_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success', 'failure', 'blocked')),
    message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def test_migration_preserves_existing_data_and_adds_new_table(tmp_path):
    db_path = tmp_path / "legacy.db"

    conn = sqlite3.connect(str(db_path))
    conn.executescript(_PRE_V02_SCHEMA)
    conn.execute("INSERT INTO memories (content, tags) VALUES ('pre-existing memory', 'important')")
    conn.execute(
        "INSERT INTO action_logs (command, tool_name, status, message) VALUES (?, ?, ?, ?)",
        ("old cmd", "old_tool", "success", "ok"),
    )
    conn.commit()
    conn.close()

    create_tables(db_path=db_path)  # the real, current migration

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        mem = conn.execute("SELECT * FROM memories").fetchall()
        logs = conn.execute("SELECT * FROM action_logs").fetchall()
        assert len(mem) == 1 and mem[0]["content"] == "pre-existing memory"
        assert len(logs) == 1 and logs[0]["tool_name"] == "old_tool"

        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "action_lifecycle" in tables

        conn.execute(
            "INSERT INTO action_lifecycle (id, tool_name, status, input_summary) "
            "VALUES ('abc-1', 'open_app', 'proposed', '{}')"
        )
        conn.commit()
        row = conn.execute("SELECT * FROM action_lifecycle WHERE id = ?", ("abc-1",)).fetchone()
        assert row is not None and row["status"] == "proposed"
    finally:
        conn.close()


def test_migration_is_idempotent_when_run_twice(tmp_path):
    """Every app startup calls create_tables() again against the same
    file — it must never error or duplicate schema objects."""
    db_path = tmp_path / "fresh.db"

    create_tables(db_path=db_path)
    create_tables(db_path=db_path)  # must not raise

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()
    assert {"memories", "conversations", "action_logs", "action_lifecycle"} <= tables


def test_fresh_database_gets_all_four_tables(tmp_path):
    db_path = tmp_path / "brand_new.db"
    create_tables(db_path=db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()
    # Subset, not equality: AUTOINCREMENT columns make SQLite add its own
    # internal sqlite_sequence table, which is real but not one of ours.
    assert {"memories", "conversations", "action_logs", "action_lifecycle"} <= tables
