"""Database access layer — thin wrapper around sqlite3."""

import sqlite3
from pathlib import Path
from typing import List, Optional

from app.config import settings
from app.core.models import ActionLog, ConversationEntry, MemoryEntry
from app.logging_config import get_logger

logger = get_logger("db.database")


class Database:
    """Thread-safe SQLite wrapper using check_same_thread=False for CLI/API usage."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._path = db_path or settings.db_path
        self._conn: Optional[sqlite3.Connection] = None

    # --- connection management ---

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._path),
                check_same_thread=False,
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # --- memories ---

    def add_memory(self, content: str, tags: Optional[str] = None) -> int:
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO memories (content, tags) VALUES (?, ?)",
            (content, tags),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def search_memory(self, query: str) -> List[MemoryEntry]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, content, tags, created_at FROM memories "
            "WHERE content LIKE ? OR tags LIKE ? "
            "ORDER BY created_at DESC LIMIT 20",
            (f"%{query}%", f"%{query}%"),
        ).fetchall()
        return [
            MemoryEntry(
                id=r["id"],
                content=r["content"],
                tags=r["tags"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def get_all_memories(self, limit: int = 50) -> List[MemoryEntry]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, content, tags, created_at FROM memories "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            MemoryEntry(id=r["id"], content=r["content"], tags=r["tags"], created_at=r["created_at"])
            for r in rows
        ]

    # --- conversations ---

    def add_conversation(self, role: str, content: str) -> int:
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO conversations (role, content) VALUES (?, ?)",
            (role, content),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_recent_conversations(self, limit: int = 20) -> List[ConversationEntry]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, role, content, created_at FROM conversations "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            ConversationEntry(id=r["id"], role=r["role"], content=r["content"], created_at=r["created_at"])
            for r in rows
        ]

    # --- action logs ---

    def log_action(
        self, command: str, tool_name: str, status: str, message: str
    ) -> int:
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO action_logs (command, tool_name, status, message) "
            "VALUES (?, ?, ?, ?)",
            (command, tool_name, status, message),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def clear_logs(self) -> int:
        """Delete all action log entries. Returns the number of rows deleted."""
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM action_logs")
        conn.commit()
        return cur.rowcount

    def get_recent_logs(self, limit: int = 50) -> List[ActionLog]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, command, tool_name, status, message, created_at "
            "FROM action_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            ActionLog(
                id=r["id"],
                command=r["command"],
                tool_name=r["tool_name"],
                status=r["status"],
                message=r["message"],
                created_at=r["created_at"],
            )
            for r in rows
        ]


# Module-level singleton
_db_instance: Optional[Database] = None


def get_db() -> Database:
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance
