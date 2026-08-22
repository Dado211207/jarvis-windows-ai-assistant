"""Windows session actions.

Only one thing lives here, and the boundary is deliberate: **locking the
workstation is the only session action JARVIS will ever perform.** Sign
out, restart, shut down and sleep all end running programs and can lose
unsaved work in other applications; "lock" cannot. Everything the user
had open is still there when they come back.

That is also why it is REVERSIBLE rather than approval-gated: the worst
case of an unwanted lock is typing a password. An approval prompt for it
would train people to click through prompts, which costs more safety
than it buys.

Windows-only, honestly. There is no cross-platform lock in this
codebase; on any other OS this reports that plainly rather than
pretending, guessing at a desktop environment, or shelling out to
whatever might be installed.
"""

import platform

from app.core.models import PermissionLevel, RiskLevel, ToolCategory, ToolDefinition
from app.logging_config import get_logger

logger = get_logger("desktop.session")


def lock_workstation() -> dict:
    """Lock the Windows session. Nothing is closed and nothing is lost."""
    if platform.system() != "Windows":
        return {
            "success": False,
            "message": "Locking the screen is only available on Windows.",
            "data": {"platform": platform.system()},
        }

    try:
        import ctypes

        # user32.LockWorkStation() — the same call the Win+L shortcut
        # makes. No subprocess, no shell, no arguments to get wrong.
        locked = bool(ctypes.windll.user32.LockWorkStation())
    except Exception as exc:  # noqa: BLE001
        logger.warning("LockWorkStation call failed: %s", exc)
        return {"success": False, "message": "The screen could not be locked.", "data": None}

    if not locked:
        # Windows returns zero when it refuses — e.g. from a service
        # session with no interactive desktop. Reported, not swallowed.
        return {
            "success": False,
            "message": "Windows refused to lock the screen from this session.",
            "data": None,
        }

    logger.info("Workstation locked on request.")
    return {"success": True, "message": "Screen locked.", "data": {"locked": True}}


def register_tools(registry) -> None:
    registry.register(
        ToolDefinition(
            name="lock_workstation",
            description=(
                "Lock the Windows screen, exactly like pressing Win+L. Nothing is "
                "closed and no work is lost. JARVIS never signs out, restarts, "
                "sleeps or shuts down the computer."
            ),
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.SYSTEM,
            risk=RiskLevel.REVERSIBLE,
            platform=["windows"],
            verification_strategy=(
                "The Windows API call's own return value; a refusal is reported "
                "rather than assumed to have worked."
            ),
        ),
        lock_workstation,
    )
