"""Schema migrations — idempotent table creation for JARVIS SQLite database."""

import sqlite3
from pathlib import Path
from typing import Optional

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

-- v0.2: persisted action lifecycle audit trail. Additive only — does not
-- touch memories/conversations/action_logs or app/core/pending_actions.py's
-- existing in-memory approval queue, which remains the live gate used by
-- app/api/actions.py exactly as before.
CREATE TABLE IF NOT EXISTS action_lifecycle (
    id                   TEXT PRIMARY KEY,
    correlation_id       TEXT,
    tool_name            TEXT NOT NULL,
    status               TEXT NOT NULL CHECK(status IN (
                              'proposed', 'pending_approval', 'approved', 'executing',
                              'succeeded', 'failed', 'cancelled', 'expired', 'blocked'
                          )),
    input_summary        TEXT NOT NULL DEFAULT '{}',
    risk                 TEXT,
    policy_action        TEXT,
    policy_reason        TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
    approved_by          TEXT,
    approval_source      TEXT,
    result_summary       TEXT,
    verification_result  TEXT,
    error_category       TEXT,
    duration_ms          REAL,
    idempotency_key      TEXT
);

CREATE INDEX IF NOT EXISTS idx_memories_created   ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_action_logs_status ON action_logs(status);
CREATE INDEX IF NOT EXISTS idx_action_lifecycle_status      ON action_lifecycle(status);
CREATE INDEX IF NOT EXISTS idx_action_lifecycle_correlation ON action_lifecycle(correlation_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_action_lifecycle_idempotency
    ON action_lifecycle(idempotency_key) WHERE idempotency_key IS NOT NULL;
"""


def create_tables(db_path: Optional[Path] = None) -> None:
    """Create all required tables if they do not exist.

    *db_path* defaults to settings.db_path; tests may pass a temp path for
    isolation without touching the real database.
    """
    resolved_path: Path = db_path or settings.db_path
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(resolved_path))
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        logger.info("Database schema verified at %s", resolved_path)
    finally:
        conn.close()
