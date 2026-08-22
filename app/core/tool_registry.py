import concurrent.futures
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import ValidationError

from app.core.errors import ErrorCategory, to_safe_error
from app.core.models import PermissionLevel, RegisteredTool, ToolDefinition
from app.core.permissions import (
    ApprovalRequiredError,
    PermissionDeniedError,
    check_permission,
)
from app.logging_config import get_logger

logger = get_logger("tool_registry")

# Shared, bounded worker pool for running tool handlers under a timeout.
# A module-level pool (not one-per-call) avoids unbounded thread creation
# under concurrent requests; 8 workers is generous for a single-user local
# desktop app. Threads are daemonic by default under ThreadPoolExecutor in
# Python 3.9+, so an abandoned (timed-out) handler thread never blocks
# process shutdown.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="jarvis-tool")


def _run_with_timeout(handler: Callable, kwargs: dict, timeout_seconds: float) -> Tuple[bool, Any]:
    """Run handler(**kwargs) with a bounded wait.

    Returns (True, result) if the handler finished within timeout_seconds,
    or (False, None) if it did not.

    Python has no safe way to forcibly kill a running thread. When this
    times out, the underlying worker thread may keep running in the
    background until it finishes naturally — this function simply stops
    *waiting* for it. Its eventual result, whatever it turns out to be, is
    never looked at again by any caller in this codebase: no state
    mutation, no audit write, no re-surfacing to the user happens because
    of it. That — not a forced kill — is the real, honest cancellation
    guarantee available for arbitrary synchronous tool handlers.
    """
    future = _EXECUTOR.submit(handler, **kwargs)
    try:
        result = future.result(timeout=timeout_seconds)
        return True, result
    except concurrent.futures.TimeoutError:
        future.cancel()  # best-effort only; a no-op once the handler has started
        return False, None


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
            completed, result = _run_with_timeout(tool.handler, kwargs, tool.definition.timeout_seconds)
            if not completed:
                return _timeout_result(name, tool.definition.timeout_seconds)
            logger.info("Tool executed: %s", name)
            if isinstance(result, dict):
                return result
            return {"success": True, "message": str(result), "data": result}
        except Exception as exc:
            safe_error = to_safe_error(exc, category=ErrorCategory.TOOL_ERROR, context=f"tool '{name}'")
            return {
                "success": False,
                "message": f"{safe_error.message} (reference: {safe_error.correlation_id[:8]})",
                "data": {"error_category": safe_error.category.value, "correlation_id": safe_error.correlation_id},
            }

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
            completed, result = _run_with_timeout(tool.handler, kwargs, tool.definition.timeout_seconds)
            if not completed:
                return _timeout_result(name, tool.definition.timeout_seconds)
            logger.info("Approved tool executed: %s", name)
            if isinstance(result, dict):
                return result
            return {"success": True, "message": str(result), "data": result}
        except Exception as exc:
            safe_error = to_safe_error(exc, category=ErrorCategory.TOOL_ERROR, context=f"approved tool '{name}'")
            return {
                "success": False,
                "message": f"{safe_error.message} (reference: {safe_error.correlation_id[:8]})",
                "data": {"error_category": safe_error.category.value, "correlation_id": safe_error.correlation_id},
            }

    def __len__(self) -> int:
        return len(self._tools)


def _timeout_result(name: str, timeout_seconds: float) -> dict:
    safe_error = to_safe_error(
        TimeoutError(f"Tool '{name}' exceeded its {timeout_seconds}s timeout."),
        category=ErrorCategory.TOOL_TIMEOUT,
        context=f"tool '{name}' timeout",
    )
    return {
        "success": False,
        "message": f"{safe_error.message} (reference: {safe_error.correlation_id[:8]})",
        "data": {
            "error_category": safe_error.category.value,
            "correlation_id": safe_error.correlation_id,
            "timed_out": True,
        },
    }


# Module-level singleton
registry = ToolRegistry()
