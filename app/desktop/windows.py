"""Windows-specific utilities — placeholder for Phase 2 expansion.

Phase 1: exposes only read-only, safe helpers.
Future phases may add window management, clipboard access (with approval), etc.
"""

import platform
from app.core.models import PermissionLevel, ToolCategory, ToolDefinition
from app.logging_config import get_logger

logger = get_logger("desktop.windows")


def get_platform_info() -> dict:
    info = {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": platform.python_version(),
    }
    msg = (
        f"Platform: {info['system']} {info['release']}\n"
        f"Machine : {info['machine']}  Processor: {info['processor']}\n"
        f"Python  : {info['python']}"
    )
    return {"success": True, "message": msg, "data": info}


def register_tools(registry) -> None:
    registry.register(
        ToolDefinition(
            name="platform_info",
            description="Show platform and Python version info.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.SYSTEM,
        ),
        get_platform_info,
    )
