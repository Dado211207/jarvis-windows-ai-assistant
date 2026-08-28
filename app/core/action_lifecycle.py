"""Persisted action lifecycle audit trail (v0.2).

Separate from app/core/pending_actions.py, which remains the live,
in-memory approval queue app/api/actions.py already uses, unchanged — that
store's own behavior (10-minute expiry, double-execution prevention via
lock, surviving page refresh because it is process-lifetime state) is
untouched by this module.

This module answers a different question: what happened, in order, to
every proposed action — across all risk tiers, not only ones that needed
approval — persisted across restarts, with redacted input so nothing
sensitive lands in the database. It is a write-heavy audit trail, not a
second approval gate; the policy engine (app/core/policy.py) and the
pending-action store still make and hold the real approval decisions.
"""

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.models import (
    TERMINAL_ACTION_STATUSES,
    ActionLifecycleRecord,
    ActionLifecycleStatus,
)
from app.core.redaction import redact_params
from app.logging_config import get_logger

logger = get_logger("action_lifecycle")


class TerminalStateError(Exception):
    """Raised when transition() is asked to move a record that already
    reached a terminal status — prevents a finished action from being
    silently reopened (double-approve / double-execute protection)."""

    def __init__(self, action_id: str, current: ActionLifecycleStatus) -> None:
        self.action_id = action_id
        self.current = current
        super().__init__(
            f"Action {action_id} is already in terminal state "
            f"'{current.value}'; cannot transition further."
        )


def propose(
    tool_name: str,
    params: Dict[str, Any],
    *,
    correlation_id: Optional[str] = None,
    risk: Optional[str] = None,
    policy_action: Optional[str] = None,
    policy_reason: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> ActionLifecycleRecord:
    """Record a new proposed action. If *idempotency_key* was already used
    by an earlier proposal, returns that existing record instead of
    creating a duplicate — a repeated request for the same logical action
    does not create a second audit entry."""
    from db.database import get_db

    db = get_db()

    if idempotency_key is not None:
        existing = db.get_action_lifecycle_record_by_idempotency_key(idempotency_key)
        if existing is not None:
            logger.info(
                "Action proposal reused existing idempotency key (id=%s, tool=%s)",
                existing.id, tool_name,
            )
            return existing

    now = datetime.now(timezone.utc)
    record = ActionLifecycleRecord(
        id=str(uuid.uuid4()),
        correlation_id=correlation_id,
        tool_name=tool_name,
        status=ActionLifecycleStatus.PROPOSED,
        input_summary=redact_params(params),
        risk=risk,
        policy_action=policy_action,
        policy_reason=policy_reason,
        created_at=now,
        updated_at=now,
        idempotency_key=idempotency_key,
    )

    try:
        db.create_action_lifecycle_record(record)
    except sqlite3.IntegrityError:
        # Lost a race against a concurrent proposal using the same key.
        existing = db.get_action_lifecycle_record_by_idempotency_key(idempotency_key) if idempotency_key else None
        if existing is not None:
            return existing
        raise

    logger.info("Action proposed: id=%s tool=%s risk=%s", record.id, tool_name, risk)
    return record


def transition(
    action_id: str, status: ActionLifecycleStatus, **fields: Any
) -> Optional[ActionLifecycleRecord]:
    """Move an action to a new status, optionally updating any other
    column (approved_by, result_summary, error_category, ...).

    Raises TerminalStateError if the record already reached a terminal
    status. Computes duration_ms automatically the first time a record
    reaches a terminal status (created_at -> now), unless the caller
    already supplied duration_ms explicitly. Returns None if the action
    does not exist.
    """
    from db.database import get_db

    db = get_db()
    current = db.get_action_lifecycle_record(action_id)
    if current is None:
        return None
    if current.status in TERMINAL_ACTION_STATUSES:
        raise TerminalStateError(action_id, current.status)

    updates: Dict[str, Any] = dict(fields)
    updates["status"] = status

    if status in TERMINAL_ACTION_STATUSES and "duration_ms" not in updates:
        elapsed_ms = (datetime.now(timezone.utc) - current.created_at).total_seconds() * 1000
        updates["duration_ms"] = round(elapsed_ms, 2)

    updated = db.update_action_lifecycle_record(action_id, **updates)
    logger.info(
        "Action transitioned: id=%s %s -> %s", action_id, current.status.value, status.value
    )
    return updated


def get(action_id: str) -> Optional[ActionLifecycleRecord]:
    from db.database import get_db

    return get_db().get_action_lifecycle_record(action_id)


def count() -> int:
    """Total audit records held, regardless of any display limit."""
    from db.database import get_db

    return get_db().count_action_lifecycle_records()


def list_recent(limit: int = 50) -> List[ActionLifecycleRecord]:
    from db.database import get_db

    return get_db().list_recent_action_lifecycle_records(limit)
