"""Clipboard reader — SENSITIVE, approval-required.

Reads the current text clipboard contents via the stdlib (tkinter's
clipboard access), so no new dependency is needed. This is deliberately
the ONLY clipboard capability in this milestone: no writing, no history,
no monitoring/polling — a persistent clipboard watcher is a surveillance
tool and is explicitly out of scope (see CLAUDE.md's no-surveillance rule
and the milestone's blocked-capabilities list).

The clipboard can hold anything the user last copied — passwords, private
messages, tokens — so this tool is SENSITIVE and always requires explicit
approval; it is never auto-executed. Its success *message* deliberately
reports only a character count, never the content itself, so the content
cannot leak into a log line, the action_lifecycle audit record, or a
WebSocket event — all of which persist/broadcast `message`, never `data`.
The raw content is returned only in `data`, delivered solely in the
direct HTTP response to the specific /actions/{id}/confirm call the user
themselves triggered after approving.
"""

from app.core.models import PermissionLevel, RiskLevel, ToolCategory, ToolDefinition
from app.logging_config import get_logger

logger = get_logger("desktop.clipboard")


def read_clipboard() -> dict:
    """Read the current text clipboard contents."""
    try:
        import tkinter
    except ImportError:
        return {
            "success": False,
            "message": "Clipboard access is unavailable: tkinter is not installed.",
            "data": None,
        }

    try:
        root = tkinter.Tk()
        root.withdraw()
        try:
            content = root.clipboard_get()
        finally:
            root.destroy()
    except tkinter.TclError:
        # Empty clipboard, or it holds non-text content (an image, a file
        # list) that clipboard_get() cannot return as a string.
        return {
            "success": True,
            "message": "Clipboard is empty or does not contain text.",
            "data": {"content": ""},
        }
    except Exception as exc:
        return {"success": False, "message": f"Could not read the clipboard: {exc}", "data": None}

    logger.info("Clipboard read (%d chars).", len(content))
    return {
        "success": True,
        "message": f"Clipboard contains {len(content)} character(s).",
        "data": {"content": content},
    }


def register_tools(registry) -> None:
    registry.register(
        ToolDefinition(
            name="read_clipboard",
            description=(
                "Read the current text clipboard contents. Requires explicit approval "
                "before running because the clipboard may hold private information "
                "(passwords, personal messages, tokens)."
            ),
            permission_level=PermissionLevel.APPROVAL_REQUIRED,
            category=ToolCategory.UTILITY,
            risk=RiskLevel.SENSITIVE,
            reversible=True,
            supports_preview=False,
            verification_strategy="Handler's own success flag; a read has no further effect to verify.",
        ),
        read_clipboard,
    )
