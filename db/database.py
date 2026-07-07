"""Database access layer — thin wrapper around sqlite3."""

import sqlite3
from pathlib import Path
from typing import List, Optional

from app.config import settings
from app.core.models import ActionLog, ConversationEntry, MemoryEntry, Preference
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

    # --- settings (Phase 8) ---

    def set_setting(self, key: str, value: str) -> None:
        """Upsert a single settings key. Values are always stored as text."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = datetime('now')",
            (key, value),
        )
        conn.commit()

    def get_setting(self, key: str) -> Optional[str]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def get_all_settings(self) -> dict:
        conn = self._get_conn()
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # --- preferences / personality memory (Phase 8) ---

    def add_preference(
        self,
        title: str,
        value: str,
        category: str = "general_preference",
        source: str = "user",
        is_sensitive: bool = False,
    ) -> int:
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO preferences (title, value, category, source, is_sensitive) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, value, category, source, 1 if is_sensitive else 0),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def _row_to_preference(self, r: sqlite3.Row) -> Preference:
        return Preference(
            id=r["id"],
            title=r["title"],
            value=r["value"],
            category=r["category"],
            source=r["source"],
            is_sensitive=bool(r["is_sensitive"]),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    def get_preferences(
        self, category: Optional[str] = None, limit: int = 100
    ) -> List[Preference]:
        conn = self._get_conn()
        if category:
            rows = conn.execute(
                "SELECT * FROM preferences WHERE category = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM preferences ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_preference(r) for r in rows]

    def search_preferences(self, query: str) -> List[Preference]:
        conn = self._get_conn()
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT * FROM preferences "
            "WHERE title LIKE ? OR value LIKE ? OR category LIKE ? "
            "ORDER BY created_at DESC LIMIT 50",
            (like, like, like),
        ).fetchall()
        return [self._row_to_preference(r) for r in rows]

    def get_preference(self, pref_id: int) -> Optional[Preference]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM preferences WHERE id = ?", (pref_id,)
        ).fetchone()
        return self._row_to_preference(row) if row else None

    def delete_preference(self, pref_id: int) -> bool:
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM preferences WHERE id = ?", (pref_id,))
        conn.commit()
        return cur.rowcount > 0

    def clear_preferences(self) -> int:
        """Delete all preference entries. Returns the number of rows deleted."""
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM preferences")
        conn.commit()
        return cur.rowcount


# Module-level singleton
_db_instance: Optional[Database] = None


def get_db() -> Database:
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance
