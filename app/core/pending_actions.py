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

    @staticmethod
    def _scrub_terminal(action: PendingAction) -> None:
        """Remove or redact data no longer needed once approval is over."""
        from app.core.redaction import redact_message, redact_params

        action.command = redact_message(action.command)
        action.parameters = redact_params(action.parameters)
        action.result = None
        action.error = None

    def get(self, action_id: str) -> Optional[PendingAction]:
        with self._lock:
            action = self._actions.get(action_id)
            if action is None:
                return None
            if action.status == "pending" and action.expires_at and datetime.utcnow() > action.expires_at:
                action.status = "expired"
                self._scrub_terminal(action)
            return action

    def list_pending(self) -> List[PendingAction]:
        now = datetime.utcnow()
        result = []
        with self._lock:
            for action in self._actions.values():
                if action.status == "pending":
                    if action.expires_at and now > action.expires_at:
                        action.status = "expired"
                        self._scrub_terminal(action)
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
                self._scrub_terminal(action)
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
                self._scrub_terminal(action)
                return None
            if action.status != "pending":
                return None
            action.status = "cancelled"
            self._scrub_terminal(action)
            return action

    def mark_executed(self, action_id: str, result: Any = None) -> None:
        """Mark complete without retaining the handler payload.

        The result is accepted for compatibility with older internal
        callers but deliberately discarded: approval results can contain
        clipboard text or other sensitive one-shot data. The confirming
        HTTP response is the only place that payload is returned.
        """
        with self._lock:
            action = self._actions.get(action_id)
            if action:
                action.status = "executed"
                self._scrub_terminal(action)

    def mark_failed(self, action_id: str, error: str = "") -> None:
        """Mark failure without retaining a handler-controlled payload.

        The confirming response already returns the safe error once. Keeping
        it on PendingAction would make clipboard-like or future sensitive
        tool output readable again through the detail endpoint.
        """
        with self._lock:
            action = self._actions.get(action_id)
            if action:
                action.status = "failed"
                self._scrub_terminal(action)


# Module-level singleton
pending_store = PendingActionStore()
