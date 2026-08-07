from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, field_serializer

from app.core.errors import SafeError


class PermissionLevel(str, Enum):
    SAFE = "safe"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


class RiskLevel(str, Enum):
    """Five-tier risk classification (v0.2). A tool that doesn't declare
    one explicitly is evaluated on a conservative mapping from its legacy
    PermissionLevel instead — see app/core/policy.py:risk_for(). Every tool
    registered before v0.2 keeps working unchanged; this is additive
    metadata, not a replacement for PermissionLevel enforcement."""
    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    SENSITIVE = "sensitive"
    DESTRUCTIVE = "destructive"
    BLOCKED = "blocked"


class ToolCategory(str, Enum):
    SYSTEM = "system"
    APP = "app"
    MEMORY = "memory"
    UTILITY = "utility"
    VOICE = "voice"


class ActionLifecycleStatus(str, Enum):
    """The full action audit-trail lifecycle (v0.2). Distinct from
    PendingAction.status in app/core/pending_actions.py, which remains the
    live in-memory approval queue used by app/api/actions.py unchanged —
    this is the persisted historical record of what happened to a proposed
    action, across every risk tier, not just ones that needed approval."""
    PROPOSED = "proposed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    BLOCKED = "blocked"


TERMINAL_ACTION_STATUSES = {
    ActionLifecycleStatus.SUCCEEDED,
    ActionLifecycleStatus.FAILED,
    ActionLifecycleStatus.CANCELLED,
    ActionLifecycleStatus.EXPIRED,
    ActionLifecycleStatus.BLOCKED,
}


class ActionLifecycleRecord(BaseModel):
    """One row of the action_lifecycle audit table (db/migrations.py)."""
    id: str
    correlation_id: Optional[str] = None
    tool_name: str
    status: ActionLifecycleStatus
    input_summary: Dict[str, Any] = {}
    risk: Optional[str] = None
    policy_action: Optional[str] = None
    policy_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    approved_by: Optional[str] = None
    approval_source: Optional[str] = None
    result_summary: Optional[str] = None
    verification_result: Optional[str] = None
    error_category: Optional[str] = None
    duration_ms: Optional[float] = None
    idempotency_key: Optional[str] = None


class ToolDefinition(BaseModel):
    name: str
    description: str
    permission_level: PermissionLevel
    category: ToolCategory

    # --- v0.2 typed contract fields — all optional, all backward compatible.
    # A tool registered without any of these behaves exactly as it did
    # before this milestone; see app/core/policy.py for how risk is derived
    # when not declared explicitly.
    risk: Optional[RiskLevel] = None
    input_model: Optional[Any] = None  # a Pydantic model class, or None
    timeout_seconds: float = 10.0
    reversible: bool = True
    supports_preview: bool = False
    verification_strategy: str = ""
    platform: List[str] = ["any"]

    model_config = {"arbitrary_types_allowed": True}

    @field_serializer("input_model")
    def _serialize_input_model(self, value: Any) -> Optional[Dict[str, Any]]:
        """input_model holds a Pydantic model CLASS, not an instance, so the
        registry can validate raw kwargs against it — a bare class isn't
        JSON-serializable, so API responses (e.g. GET /tools) get its JSON
        schema instead, which also tells a client what fields the tool
        actually expects."""
        if value is None or not hasattr(value, "model_json_schema"):
            return None
        return value.model_json_schema()


class RegisteredTool(BaseModel):
    definition: ToolDefinition
    handler: Any  # Callable — excluded from serialization

    model_config = {"arbitrary_types_allowed": True}


class CommandRequest(BaseModel):
    command: str


class CommandResponse(BaseModel):
    success: bool
    message: str
    data: Optional[Any] = None
    tool_used: Optional[str] = None
    requires_approval: bool = False
    pending_action_id: Optional[str] = None


class MemoryEntry(BaseModel):
    id: Optional[int] = None
    content: str
    tags: Optional[str] = None
    created_at: Optional[datetime] = None


class ConversationEntry(BaseModel):
    id: Optional[int] = None
    role: str
    content: str
    created_at: Optional[datetime] = None


class BrainResponse(BaseModel):
    content: str
    provider: str
    model: Optional[str] = None
    used_api: bool = False
    error: Optional[SafeError] = None


class ActionLog(BaseModel):
    id: Optional[int] = None
    command: str
    tool_name: str
    status: str
    message: str
    created_at: Optional[datetime] = None
