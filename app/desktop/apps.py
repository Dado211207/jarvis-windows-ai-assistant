"""App launcher — opens allowlisted applications only."""

import platform
import subprocess
from typing import Dict, Optional

from app.core.models import PermissionLevel, ToolCategory, ToolDefinition
from app.logging_config import get_logger

logger = get_logger("desktop.apps")

# Allowlisted app names mapped to their Windows executable paths.
# Key = lowercase alias the user types, value = Windows executable.
APP_ALLOWLIST: Dict[str, str] = {
    "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "brave": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    "edge": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "vscode": r"C:\Users\%USERNAME%\AppData\Local\Programs\Microsoft VS Code\Code.exe",
    "spotify": r"C:\Users\%USERNAME%\AppData\Roaming\Spotify\Spotify.exe",
    "discord": r"C:\Users\%USERNAME%\AppData\Local\Discord\Update.exe",
}

# Short aliases for cross-platform test environments
_POSIX_FALLBACKS: Dict[str, str] = {
    "chrome": "google-chrome",
    "brave": "brave-browser",
    "edge": "microsoft-edge",
    "notepad": "gedit",
    "calculator": "gnome-calculator",
    "vscode": "code",
    "spotify": "spotify",
    "discord": "discord",
}


def open_app(app_name: str) -> dict:
    """Launch an allowlisted application by name."""
    name = app_name.strip().lower()
    if name not in APP_ALLOWLIST:
        allowed = ", ".join(sorted(APP_ALLOWLIST.keys()))
        return {
            "success": False,
            "message": (
                f"'{app_name}' is not in the allowlist. "
                f"Allowed apps: {allowed}"
            ),
            "data": None,
        }

    is_windows = platform.system() == "Windows"

    if is_windows:
        exe = APP_ALLOWLIST[name]
        try:
            subprocess.Popen([exe], shell=False)  # noqa: S603
            logger.info("Launched app: %s (%s)", name, exe)
            return {"success": True, "message": f"Launched {name}.", "data": {"app": name}}
        except FileNotFoundError:
            return {
                "success": False,
                "message": f"Could not find '{name}' at '{exe}'. Is it installed?",
                "data": None,
            }
        except Exception as exc:
            return {"success": False, "message": f"Failed to launch '{name}': {exc}", "data": None}
    else:
        # Non-Windows: attempt posix fallback (best-effort for dev/CI)
        fallback = _POSIX_FALLBACKS.get(name)
        if fallback:
            try:
                subprocess.Popen([fallback], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logger.info("Launched posix fallback: %s (%s)", name, fallback)
                return {
                    "success": True,
                    "message": f"Launched {name} (posix fallback: {fallback}).",
                    "data": {"app": name},
                }
            except Exception:
                pass
        return {
            "success": False,
            "message": (
                f"App launcher is designed for Windows. "
                f"'{name}' could not be launched on {platform.system()}."
            ),
            "data": None,
        }


def list_apps() -> dict:
    apps = sorted(APP_ALLOWLIST.keys())
    return {
        "success": True,
        "message": "Allowlisted apps: " + ", ".join(apps),
        "data": apps,
    }


def register_tools(registry) -> None:
    registry.register(
        ToolDefinition(
            name="open_app",
            description="Open an allowlisted application by name (chrome, notepad, etc.).",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.APP,
        ),
        open_app,
    )
    registry.register(
        ToolDefinition(
            name="list_apps",
            description="List all allowlisted applications.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.APP,
        ),
        list_apps,
    )
