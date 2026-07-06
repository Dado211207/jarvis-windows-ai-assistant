from typing import Callable, Dict, List, Optional

from app.core.models import PermissionLevel, RegisteredTool, ToolDefinition
from app.core.permissions import (
    ApprovalRequiredError,
    PermissionDeniedError,
    check_permission,
)
from app.logging_config import get_logger

logger = get_logger("tool_registry")


class ToolRegistry:
    """Central registry for all JARVIS tools."""

    def __init__(self) -> None:
        self._tools: Dict[str, RegisteredTool] = {}

    def register(self, definition: ToolDefinition, handler: Callable) -> None:
        if definition.name in self._tools:
            logger.warning("Re-registering tool: %s", definition.name)
        self._tools[definition.name] = RegisteredTool(
            definition=definition, handler=handler
        )
        logger.debug(
            "Registered tool '%s' [%s/%s]",
            definition.name,
            definition.permission_level,
            definition.category,
        )

    def get(self, name: str) -> Optional[RegisteredTool]:
        return self._tools.get(name)

    def list_tools(self) -> List[RegisteredTool]:
        return list(self._tools.values())

    def list_definitions(self) -> List[ToolDefinition]:
        return [t.definition for t in self._tools.values()]

    def execute(self, name: str, **kwargs) -> dict:
        """Execute a registered tool after permission check.

        Returns a dict with keys: success, message, data.
        """
        tool = self.get(name)
        if tool is None:
            return {"success": False, "message": f"Unknown tool: '{name}'", "data": None}

        try:
            check_permission(name, tool.definition.permission_level)
        except PermissionDeniedError as exc:
            return {"success": False, "message": str(exc), "data": None}
        except ApprovalRequiredError as exc:
            return {"success": False, "message": str(exc), "data": None}

        try:
            result = tool.handler(**kwargs)
            logger.info("Tool executed: %s", name)
            if isinstance(result, dict):
                return result
            return {"success": True, "message": str(result), "data": result}
        except Exception as exc:
            logger.error("Tool '%s' raised an error: %s", name, exc, exc_info=True)
            return {"success": False, "message": f"Tool error: {exc}", "data": None}

    def execute_approved(self, name: str, **kwargs) -> dict:
        """Execute a tool that has been explicitly approved by the user.

        Skips the permission check because the caller (the confirm endpoint)
        has already verified that the user consented to this specific action.
        Only call this from the action confirmation path.
        """
        tool = self._tools.get(name)
        if tool is None:
            return {"success": False, "message": f"Unknown tool: '{name}'", "data": None}
        try:
            result = tool.handler(**kwargs)
            logger.info("Approved tool executed: %s", name)
            if isinstance(result, dict):
                return result
            return {"success": True, "message": str(result), "data": result}
        except Exception as exc:
            logger.error("Approved tool '%s' raised an error: %s", name, exc, exc_info=True)
            return {"success": False, "message": f"Tool error: {exc}", "data": None}

    def __len__(self) -> int:
        return len(self._tools)


# Module-level singleton
registry = ToolRegistry()
