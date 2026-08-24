"""Database access layer — thin wrapper around sqlite3."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from app.config import settings
from app.core.models import (
    ActionLifecycleRecord,
    ActionLog,
    ConversationEntry,
    MemoryEntry,
)
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
        """Insert one memory row.

        **The only place a memory row is ever created**, which is why the
        secret check is repeated here. `app/core/memory.py` checks first
        and returns a readable refusal; this raises. A caller that forgot
        to check gets an exception rather than quietly writing a
        credential to disk — the guarantee belongs at the insert, not at
        each of the callers that might one day exist.

        Raises SecretRejected, which carries the *kind* of secret and
        never the value.
        """
        from app.core.secret_guard import SecretRejected, find_secret

        label = find_secret(content) or find_secret(tags or "")
        if label:
            raise SecretRejected(label)

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

    def delete_memory(self, memory_id: int) -> bool:
        """Delete one memory. Returns whether a row was actually removed,
        so a caller can tell "deleted" from "was not there" rather than
        reporting success for an id that never existed."""
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        return cur.rowcount > 0

    def clear_memories(self) -> int:
        """Delete every stored memory. Returns the number removed."""
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM memories")
        conn.commit()
        return cur.rowcount

    def count_rows(self, table: str) -> int:
        """Row count for one of the tables JARVIS stores user data in.

        The table name is checked against a fixed set rather than
        interpolated blindly — a count endpoint is not a reason to open a
        path from a request to arbitrary SQL.
        """
        allowed = {"memories", "conversations", "action_logs", "action_lifecycle"}
        if table not in allowed:
            raise ValueError(f"Unknown table: {table!r}")
        conn = self._get_conn()
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"]) if row else 0

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

    def clear_conversations(self) -> int:
        """Delete every stored conversation turn. Returns rows deleted.

        Scoped to this table alone — action_logs and action_lifecycle are
        untouched, so clearing a chat never doubles as erasing the audit
        trail (see app/core/conversation.py::reset)."""
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM conversations")
        conn.commit()
        return cur.rowcount

    # --- action logs ---

    def log_action(
        self, command: str, tool_name: str, status: str, message: str
    ) -> int:
        # This is the single write boundary for the legacy action log.
        # Callers are numerous and historically inconsistent, so redact
        # here rather than relying on every route to remember.
        from app.core.redaction import redact_message

        safe_command = redact_message(str(command))
        safe_message = redact_message(str(message))
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO action_logs (command, tool_name, status, message) "
            "VALUES (?, ?, ?, ?)",
            (safe_command, tool_name, status, safe_message),
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

    # --- action lifecycle (v0.2 audit trail) ---
    # Thin and mechanical like the sections above: no redaction, no policy
    # decisions, no idempotency fallback logic here. That belongs to
    # app/core/action_lifecycle.py, which is the only caller of these
    # methods. A duplicate idempotency_key raises sqlite3.IntegrityError
    # (enforced by the partial unique index in db/migrations.py) — the
    # caller decides what to do with that, this layer just reports it.

    def create_action_lifecycle_record(self, record: ActionLifecycleRecord) -> None:
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO action_lifecycle (
                id, correlation_id, tool_name, status, input_summary,
                risk, policy_action, policy_reason, created_at, updated_at,
                approved_by, approval_source, result_summary,
                verification_result, error_category, duration_ms, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.correlation_id,
                record.tool_name,
                record.status.value,
                json.dumps(record.input_summary),
                record.risk,
                record.policy_action,
                record.policy_reason,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
                record.approved_by,
                record.approval_source,
                record.result_summary,
                record.verification_result,
                record.error_category,
                record.duration_ms,
                record.idempotency_key,
            ),
        )
        conn.commit()

    def get_action_lifecycle_record(self, action_id: str) -> Optional[ActionLifecycleRecord]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM action_lifecycle WHERE id = ?", (action_id,)
        ).fetchone()
        return self._row_to_action_record(row) if row else None

    def get_action_lifecycle_record_by_idempotency_key(
        self, idempotency_key: str
    ) -> Optional[ActionLifecycleRecord]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM action_lifecycle WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return self._row_to_action_record(row) if row else None

    def update_action_lifecycle_record(
        self, action_id: str, **fields: Any
    ) -> Optional[ActionLifecycleRecord]:
        """Update arbitrary columns on an existing record and stamp
        updated_at. Returns the refreshed record, or None if *action_id*
        does not exist. A no-op (no fields) still refreshes updated_at."""
        conn = self._get_conn()
        fields = dict(fields)
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()

        values = []
        for value in fields.values():
            values.append(value.value if hasattr(value, "value") else value)

        set_clause = ", ".join(f"{column} = ?" for column in fields)
        cur = conn.execute(
            f"UPDATE action_lifecycle SET {set_clause} WHERE id = ?",
            (*values, action_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_action_lifecycle_record(action_id)

    def count_action_lifecycle_records(self) -> int:
        """How many audit records exist in total, so a capped list can say
        "showing 50 of 214" instead of implying it showed everything."""
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) AS n FROM action_lifecycle").fetchone()
        return int(row["n"]) if row else 0

    def list_recent_action_lifecycle_records(self, limit: int = 50) -> List[ActionLifecycleRecord]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM action_lifecycle ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_action_record(r) for r in rows]

    @staticmethod
    def _row_to_action_record(row: sqlite3.Row) -> ActionLifecycleRecord:
        data = dict(row)
        data["input_summary"] = json.loads(data["input_summary"]) if data["input_summary"] else {}
        return ActionLifecycleRecord(**data)


# Module-level singleton
_db_instance: Optional[Database] = None


def get_db() -> Database:
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance
