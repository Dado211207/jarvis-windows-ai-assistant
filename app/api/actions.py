"""Action approval API endpoints — Phase 5.

All execution of approved actions goes through registry.execute_approved(),
which calls the tool handler directly after explicit user confirmation.
The permission check is intentionally skipped for confirmed actions only;
it still fires for any direct registry.execute() call.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.session import require_session_token
from app.core.pending_actions import PendingAction, pending_store
from app.logging_config import get_logger

logger = get_logger("api.actions")

router = APIRouter(prefix="/actions", tags=["actions"])


# --- response schemas ---

class ActionPreview(BaseModel):
    id: str
    command: str
    tool_name: str
    action_name: str
    description: str
    risk_level: str
    parameters: Dict[str, Any]
    status: str
    created_at: str
    expires_at: Optional[str] = None
    result: Optional[Any] = None
    error: Optional[str] = None


class ActionResponse(BaseModel):
    success: bool
    message: str
    action_id: str
    status: str
    data: Optional[Any] = None


# --- helpers ---

def _to_preview(action: PendingAction) -> ActionPreview:
    return ActionPreview(
        id=action.id,
        command=action.command,
        tool_name=action.tool_name,
        action_name=action.action_name,
        description=action.description,
        risk_level=action.risk_level,
        parameters=action.parameters,
        status=action.status,
        created_at=action.created_at.isoformat(),
        expires_at=action.expires_at.isoformat() if action.expires_at else None,
        result=action.result,
        error=action.error,
    )


# --- routes ---

@router.get("/pending", response_model=List[ActionPreview])
def list_pending_actions() -> List[ActionPreview]:
    return [_to_preview(a) for a in pending_store.list_pending()]


@router.get("/{action_id}", response_model=ActionPreview)
def get_action(action_id: str) -> ActionPreview:
    action = pending_store.get(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail=f"Action '{action_id}' not found.")
    return _to_preview(action)


def _sync_lifecycle(action_id: str, status, **fields) -> None:
    """Best-effort mirror of a pending-action transition into the v0.2
    persisted audit trail. Actions created outside the router (some tests
    construct a PendingAction directly) have no matching lifecycle record —
    that is expected and not an error, so a missing record is silently
    skipped rather than treated as a failure of the REST call it backs.
    Never allowed to fail the request it's attached to: this is audit
    trail bookkeeping, not part of the approval decision itself.
    """
    from app.core.action_lifecycle import TerminalStateError, get as lifecycle_get, transition as lifecycle_transition

    try:
        if lifecycle_get(action_id) is None:
            return
        lifecycle_transition(action_id, status, **fields)
    except TerminalStateError:
        logger.warning("Lifecycle record %s already terminal; skipped mirroring %s.", action_id, status)
    except Exception:
        logger.exception("Failed to mirror action %s into the lifecycle audit trail.", action_id)


@router.post("/{action_id}/confirm", response_model=ActionResponse, dependencies=[Depends(require_session_token)])
def confirm_action(action_id: str) -> ActionResponse:
    """Confirm and execute a pending action.

    The action must be in 'pending' status. Already-executed and cancelled
    actions return a safe error without re-executing.
    """
    from app.core.events import EventType, event_bus
    from app.core.models import ActionLifecycleStatus
    from app.core.runtime_state import RuntimeState, runtime

    action = pending_store.confirm(action_id)
    if action is None:
        existing = pending_store.get(action_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Action '{action_id}' not found.")
        if existing.status == "expired":
            _sync_lifecycle(action_id, ActionLifecycleStatus.EXPIRED)
        return ActionResponse(
            success=False,
            message=f"Action cannot be confirmed: current status is '{existing.status}'.",
            action_id=action_id,
            status=existing.status,
        )

    _sync_lifecycle(action_id, ActionLifecycleStatus.APPROVED, approved_by="user", approval_source="dashboard")
    event_bus.publish(
        EventType.ACTION_APPROVAL_CHANGED,
        {"action_id": action_id, "tool_name": action.tool_name, "status": "approved"},
        correlation_id=action_id,
    )
    runtime.try_transition(RuntimeState.EXECUTING, correlation_id=action_id, reason=f"executing approved {action.tool_name}")
    _sync_lifecycle(action_id, ActionLifecycleStatus.EXECUTING)

    # Execute via the approved path — bypasses the permission check because
    # the user has explicitly confirmed. Logging happens here, not in the router.
    from app.core.tool_registry import registry
    result = registry.execute_approved(action.tool_name, **action.parameters)

    runtime.try_transition(RuntimeState.STANDBY, correlation_id=action_id, reason="approved action complete")

    try:
        from db.database import get_db
        get_db().log_action(
            command=action.command,
            tool_name=action.tool_name,
            status="success" if result.get("success") else "failure",
            message=result.get("message", ""),
        )
    except Exception:
        pass

    if result.get("success"):
        pending_store.mark_executed(action_id, result.get("data"))
        _sync_lifecycle(action_id, ActionLifecycleStatus.SUCCEEDED, result_summary=str(result.get("message", ""))[:500])
        event_bus.publish(
            EventType.ACTION_RESULT,
            {"action_id": action_id, "tool_name": action.tool_name, "success": True, "message": result.get("message", "")},
            correlation_id=action_id,
        )
        logger.info("Action confirmed and executed: %s (id=%s)", action.tool_name, action_id)
        return ActionResponse(
            success=True,
            message=result.get("message", "Action executed successfully."),
            action_id=action_id,
            status="executed",
            data=result.get("data"),
        )
    else:
        result_data = result.get("data") or {}
        pending_store.mark_failed(action_id, result.get("message", ""))
        _sync_lifecycle(
            action_id, ActionLifecycleStatus.FAILED,
            result_summary=str(result.get("message", ""))[:500],
            error_category=result_data.get("error_category", "tool_execution_failed"),
        )
        event_bus.publish(
            EventType.ACTION_RESULT,
            {
                "action_id": action_id, "tool_name": action.tool_name, "success": False,
                "message": result.get("message", ""), "timed_out": bool(result_data.get("timed_out", False)),
            },
            correlation_id=action_id,
        )
        logger.warning("Action confirmed but execution failed: %s (id=%s)", action.tool_name, action_id)
        return ActionResponse(
            success=False,
            message=result.get("message", "Execution failed."),
            action_id=action_id,
            status="failed",
        )


@router.post("/{action_id}/cancel", response_model=ActionResponse, dependencies=[Depends(require_session_token)])
def cancel_action(action_id: str) -> ActionResponse:
    """Cancel a pending action. A cancelled action is never executed."""
    from app.core.events import EventType, event_bus
    from app.core.models import ActionLifecycleStatus
    from app.core.runtime_state import RuntimeState, runtime

    action = pending_store.cancel(action_id)
    if action is None:
        existing = pending_store.get(action_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Action '{action_id}' not found.")
        if existing.status == "expired":
            _sync_lifecycle(action_id, ActionLifecycleStatus.EXPIRED)
        return ActionResponse(
            success=False,
            message=f"Action cannot be cancelled: current status is '{existing.status}'.",
            action_id=action_id,
            status=existing.status,
        )

    try:
        from db.database import get_db
        # 'blocked' is the reserved DB status for actions that were stopped before execution
        get_db().log_action(
            command=action.command,
            tool_name=action.tool_name,
            status="blocked",
            message="Action cancelled by user.",
        )
    except Exception:
        pass

    _sync_lifecycle(action_id, ActionLifecycleStatus.CANCELLED)
    event_bus.publish(
        EventType.ACTION_APPROVAL_CHANGED,
        {"action_id": action_id, "tool_name": action.tool_name, "status": "cancelled"},
        correlation_id=action_id,
    )
    runtime.try_transition(RuntimeState.STANDBY, correlation_id=action_id, reason="action cancelled")

    logger.info("Action cancelled: %s (id=%s)", action.tool_name, action_id)
    return ActionResponse(
        success=True,
        message="Action cancelled. No changes were made.",
        action_id=action_id,
        status="cancelled",
    )
