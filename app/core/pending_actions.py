"""In-memory pending action store — Phase 5 Action Approval System.

Pending actions reset on app restart. This is intentional: a pending approval
that survives a restart would be stale context. Users must re-issue the command.
"""

import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

EXPIRY_MINUTES = 10


class PendingAction(BaseModel):
    id: str
    command: str
    tool_name: str
    action_name: str
    description: str
    risk_level: str        # "low" | "medium" | "high"
    parameters: Dict[str, Any]
    status: str            # pending / confirmed / cancelled / expired / executed / failed
    created_at: datetime
    expires_at: Optional[datetime] = None
    result: Optional[Any] = None
    error: Optional[str] = None


class PendingActionStore:
    """Thread-safe in-memory store for pending approval actions."""

    def __init__(self) -> None:
        self._actions: Dict[str, PendingAction] = {}
        self._lock = threading.Lock()

    def create(
        self,
        command: str,
        tool_name: str,
        action_name: str,
        description: str,
        risk_level: str,
        parameters: Dict[str, Any],
        id: Optional[str] = None,
    ) -> PendingAction:
        now = datetime.utcnow()
        action = PendingAction(
            id=id or str(uuid.uuid4()),
            command=command,
            tool_name=tool_name,
            action_name=action_name,
            description=description,
            risk_level=risk_level,
            parameters=parameters,
            status="pending",
            created_at=now,
            expires_at=now + timedelta(minutes=EXPIRY_MINUTES),
        )
        with self._lock:
            self._actions[action.id] = action
        return action

    def get(self, action_id: str) -> Optional[PendingAction]:
        with self._lock:
            action = self._actions.get(action_id)
            if action is None:
                return None
            if action.status == "pending" and action.expires_at and datetime.utcnow() > action.expires_at:
                action.status = "expired"
            return action

    def list_pending(self) -> List[PendingAction]:
        now = datetime.utcnow()
        result = []
        with self._lock:
            for action in self._actions.values():
                if action.status == "pending":
                    if action.expires_at and now > action.expires_at:
                        action.status = "expired"
                    else:
                        result.append(action)
        return result

    def list_all(self) -> List[PendingAction]:
        with self._lock:
            return list(self._actions.values())

    def confirm(self, action_id: str) -> Optional[PendingAction]:
        """Transition status from pending → confirmed. Returns the action or None."""
        with self._lock:
            action = self._actions.get(action_id)
            if action is None:
                return None
            if action.status == "pending" and action.expires_at and datetime.utcnow() > action.expires_at:
                action.status = "expired"
                return None
            if action.status != "pending":
                return None
            action.status = "confirmed"
            return action

    def cancel(self, action_id: str) -> Optional[PendingAction]:
        """Transition status from pending → cancelled. Returns the action or None."""
        with self._lock:
            action = self._actions.get(action_id)
            if action is None:
                return None
            if action.status == "pending" and action.expires_at and datetime.utcnow() > action.expires_at:
                action.status = "expired"
                return None
            if action.status != "pending":
                return None
            action.status = "cancelled"
            return action

    def mark_executed(self, action_id: str, result: Any) -> None:
        with self._lock:
            action = self._actions.get(action_id)
            if action:
                action.status = "executed"
                action.result = result

    def mark_failed(self, action_id: str, error: str) -> None:
        with self._lock:
            action = self._actions.get(action_id)
            if action:
                action.status = "failed"
                action.error = error


# Module-level singleton
pending_store = PendingActionStore()
