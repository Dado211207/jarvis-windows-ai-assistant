"""Schema migrations — idempotent table creation for JARVIS SQLite database."""

import sqlite3
from pathlib import Path

from app.config import settings
from app.logging_config import get_logger

logger = get_logger("db.migrations")

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT    NOT NULL,
    tags       TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS action_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    command    TEXT NOT NULL,
    tool_name  TEXT NOT NULL,
    status     TEXT NOT NULL CHECK(status IN ('success', 'failure', 'blocked')),
    message    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_memories_created   ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_action_logs_status ON action_logs(status);
"""


def create_tables() -> None:
    """Create all required tables if they do not exist."""
    db_path: Path = settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        logger.info("Database schema verified at %s", db_path)
    finally:
        conn.close()
