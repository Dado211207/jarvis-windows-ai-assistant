"""Action approval API endpoints — Phase 5.

All execution of approved actions goes through registry.execute_approved(),
which calls the tool handler directly after explicit user confirmation.
The permission check is intentionally skipped for confirmed actions only;
it still fires for any direct registry.execute() call.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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


@router.post("/{action_id}/confirm", response_model=ActionResponse)
def confirm_action(action_id: str) -> ActionResponse:
    """Confirm and execute a pending action.

    The action must be in 'pending' status. Already-executed and cancelled
    actions return a safe error without re-executing.
    """
    action = pending_store.confirm(action_id)
    if action is None:
        existing = pending_store.get(action_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Action '{action_id}' not found.")
        return ActionResponse(
            success=False,
            message=f"Action cannot be confirmed: current status is '{existing.status}'.",
            action_id=action_id,
            status=existing.status,
        )

    # Execute via the approved path — bypasses the permission check because
    # the user has explicitly confirmed. Logging happens here, not in the router.
    from app.core.tool_registry import registry
    result = registry.execute_approved(action.tool_name, **action.parameters)

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
        logger.info("Action confirmed and executed: %s (id=%s)", action.tool_name, action_id)
        return ActionResponse(
            success=True,
            message=result.get("message", "Action executed successfully."),
            action_id=action_id,
            status="executed",
            data=result.get("data"),
        )
    else:
        pending_store.mark_failed(action_id, result.get("message", ""))
        logger.warning("Action confirmed but execution failed: %s (id=%s)", action.tool_name, action_id)
        return ActionResponse(
            success=False,
            message=result.get("message", "Execution failed."),
            action_id=action_id,
            status="failed",
        )


@router.post("/{action_id}/cancel", response_model=ActionResponse)
def cancel_action(action_id: str) -> ActionResponse:
    """Cancel a pending action. A cancelled action is never executed."""
    action = pending_store.cancel(action_id)
    if action is None:
        existing = pending_store.get(action_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Action '{action_id}' not found.")
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

    logger.info("Action cancelled: %s (id=%s)", action.tool_name, action_id)
    return ActionResponse(
        success=True,
        message="Action cancelled. No changes were made.",
        action_id=action_id,
        status="cancelled",
    )
