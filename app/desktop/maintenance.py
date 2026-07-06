"""Maintenance tools — Phase 5 Action Approval System.

All tools here use APPROVAL_REQUIRED and will not execute until the user
explicitly confirms through the approval UI or API.
"""

from app.core.models import PermissionLevel, ToolCategory, ToolDefinition
from app.core.tool_registry import ToolRegistry
from app.logging_config import get_logger

logger = get_logger("desktop.maintenance")


def _clear_logs() -> dict:
    """Delete all action log entries from the local database."""
    try:
        from db.database import get_db
        count = get_db().clear_logs()
        logger.info("Action logs cleared: %d rows deleted.", count)
        noun = "entry" if count == 1 else "entries"
        return {
            "success": True,
            "message": f"Cleared {count} action log {noun} from the database.",
            "data": {"rows_deleted": count},
        }
    except Exception as exc:
        logger.error("Failed to clear logs: %s", exc)
        return {"success": False, "message": f"Failed to clear logs: {exc}"}


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="clear_logs",
            description="Clear all action log entries from the local database. Requires approval.",
            permission_level=PermissionLevel.APPROVAL_REQUIRED,
            category=ToolCategory.UTILITY,
        ),
        _clear_logs,
    )
