from typing import Callable, Dict, List, Optional

from pydantic import ValidationError

from app.core.models import PermissionLevel, RegisteredTool, ToolDefinition
from app.core.permissions import (
    ApprovalRequiredError,
    PermissionDeniedError,
    check_permission,
)
from app.logging_config import get_logger

logger = get_logger("tool_registry")


def _validate_input(tool: RegisteredTool, kwargs: dict) -> Optional[dict]:
    """Returns an error result dict if *kwargs* fails the tool's declared
    input_model, or None if validation passed (or the tool declares none —
    every tool registered before v0.2 has no input_model and is unaffected).
    Validated, coerced values replace kwargs in place so handlers receive
    the same types Pydantic parsed, not the raw untyped input."""
    input_model = tool.definition.input_model
    if input_model is None:
        return None
    try:
        validated = input_model(**kwargs)
    except ValidationError as exc:
        return {
            "success": False,
            "message": f"Invalid input for tool '{tool.definition.name}': {exc.error_count()} validation error(s).",
            "data": None,
        }
    kwargs.clear()
    kwargs.update(validated.model_dump())
    return None


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

        validation_error = _validate_input(tool, kwargs)
        if validation_error is not None:
            return validation_error

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

        validation_error = _validate_input(tool, kwargs)
        if validation_error is not None:
            return validation_error

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
