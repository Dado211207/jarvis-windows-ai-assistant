from app.core.models import PermissionLevel
from app.logging_config import get_logger

logger = get_logger("permissions")

# Tools that are absolutely forbidden — never implemented, always refused
BLOCKED_KEYWORDS = {
    "steal_password",
    "read_browser_passwords",
    "bypass_login",
    "exfiltrate",
    "keylogger",
    "screen_spy",
}


class PermissionDeniedError(Exception):
    pass


class ApprovalRequiredError(Exception):
    pass


def check_permission(tool_name: str, level: PermissionLevel) -> None:
    """Raise if the tool cannot run in the current phase.

    Safe tools pass through.
    Approval-required tools raise ApprovalRequiredError.
    Blocked tools raise PermissionDeniedError unconditionally.
    """
    if tool_name in BLOCKED_KEYWORDS or level == PermissionLevel.BLOCKED:
        logger.warning("Blocked tool execution attempt: %s", tool_name)
        raise PermissionDeniedError(
            f"Tool '{tool_name}' is permanently blocked. "
            "This action is not permitted by JARVIS."
        )

    if level == PermissionLevel.APPROVAL_REQUIRED:
        logger.info("Approval required for tool: %s", tool_name)
        raise ApprovalRequiredError(
            f"Tool '{tool_name}' requires explicit user approval before execution. "
            "Approval workflow is not yet implemented in Phase 1."
        )

    # PermissionLevel.SAFE — allowed
    logger.debug("Permission granted: %s (%s)", tool_name, level)
