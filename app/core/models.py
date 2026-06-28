from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel


class PermissionLevel(str, Enum):
    SAFE = "safe"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


class ToolCategory(str, Enum):
    SYSTEM = "system"
    APP = "app"
    MEMORY = "memory"
    UTILITY = "utility"


class ToolDefinition(BaseModel):
    name: str
    description: str
    permission_level: PermissionLevel
    category: ToolCategory

    model_config = {"arbitrary_types_allowed": True}


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


class ActionLog(BaseModel):
    id: Optional[int] = None
    command: str
    tool_name: str
    status: str
    message: str
    created_at: Optional[datetime] = None
