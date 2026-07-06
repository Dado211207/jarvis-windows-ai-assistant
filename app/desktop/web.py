"""Safe URL opener — opens http/https URLs in the default browser only.

Blocked schemes: file:, javascript:, data:, powershell:, cmd:, vbscript:
"""
import webbrowser
from urllib.parse import urlparse

from app.core.models import PermissionLevel, ToolCategory, ToolDefinition
from app.logging_config import get_logger

logger = get_logger("desktop.web")

JARVIS_DASHBOARD_URL = "http://127.0.0.1:5555/ui/"

_BLOCKED_SCHEMES = frozenset({
    "file", "javascript", "data", "powershell", "cmd",
    "vbscript", "ms-dos", "about", "blob",
})


def open_website(url: str) -> dict:
    """Open a URL in the default browser. Only http/https allowed."""
    raw = url.strip()
    if not raw:
        return {"success": False, "message": "No URL provided.", "data": None}

    # Parse first to see if a scheme is already present (e.g. javascript:, file:)
    try:
        pre = urlparse(raw)
    except Exception:
        return {"success": False, "message": "Invalid URL.", "data": None}

    if not pre.scheme:
        # No scheme detected (e.g. "github.com") — safely add https://
        raw = "https://" + raw
        try:
            parsed = urlparse(raw)
        except Exception:
            return {"success": False, "message": "Invalid URL.", "data": None}
    else:
        parsed = pre

    scheme = parsed.scheme.lower()

    if scheme in _BLOCKED_SCHEMES:
        logger.warning("Blocked URL scheme attempt: %s", scheme)
        return {
            "success": False,
            "message": (
                f"Scheme '{scheme}:' is blocked for safety. "
                "Only http:// and https:// URLs are allowed."
            ),
            "data": None,
        }

    if scheme not in ("http", "https"):
        return {
            "success": False,
            "message": f"Only http:// and https:// URLs are allowed. Got: '{scheme}://'",
            "data": None,
        }

    if not parsed.netloc:
        return {"success": False, "message": "Invalid URL: no host found.", "data": None}

    try:
        webbrowser.open(raw)
        logger.info("Opened URL: %s", raw)
        return {
            "success": True,
            "message": f"Opening {raw} in your default browser.",
            "data": {"url": raw},
        }
    except Exception as exc:
        return {"success": False, "message": f"Failed to open URL: {exc}", "data": None}


def open_dashboard() -> dict:
    """Open the JARVIS local dashboard in the default browser."""
    return open_website(JARVIS_DASHBOARD_URL)


def register_tools(registry) -> None:
    registry.register(
        ToolDefinition(
            name="open_website",
            description=(
                "Open an http/https URL in the default browser. "
                "Blocked: file:, javascript:, data:, powershell:, cmd:"
            ),
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.UTILITY,
        ),
        open_website,
    )
    registry.register(
        ToolDefinition(
            name="open_dashboard",
            description="Open the JARVIS local dashboard (http://127.0.0.1:5555/ui/) in the browser.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.UTILITY,
        ),
        open_dashboard,
    )
