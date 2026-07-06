"""Safe folder opener — opens allowlisted user folders in the OS file manager.

Only known safe user folders are accepted. Arbitrary paths are blocked.
"""
import platform
import subprocess
from pathlib import Path

from app.core.models import PermissionLevel, ToolCategory, ToolDefinition
from app.logging_config import get_logger

logger = get_logger("desktop.folders")

# JARVIS project root directory
_JARVIS_DIR = Path(__file__).parents[2]

# Canonical display names shown in error messages
_CANONICAL_NAMES = frozenset({
    "downloads", "documents", "desktop",
    "pictures", "music", "videos", "notes", "jarvis",
})


def _build_folder_map() -> dict:
    home = Path.home()
    notes = home / "Documents" / "JARVIS_Notes"
    return {
        "downloads": home / "Downloads",
        "download": home / "Downloads",
        "documents": home / "Documents",
        "document": home / "Documents",
        "desktop": home / "Desktop",
        "pictures": home / "Pictures",
        "picture": home / "Pictures",
        "music": home / "Music",
        "videos": home / "Videos",
        "video": home / "Videos",
        "notes": notes,
        "note": notes,
        "jarvis": _JARVIS_DIR,
    }


def open_folder(folder_name: str) -> dict:
    """Open an allowlisted user folder in the OS file manager."""
    raw = folder_name.strip().lower()
    # Normalize spaces/hyphens and strip a trailing " folder" suffix
    key = raw.replace(" ", "_").replace("-", "_")
    if key.endswith("_folder"):
        key = key[: -len("_folder")]

    folder_map = _build_folder_map()
    if key not in folder_map:
        allowed = ", ".join(sorted(_CANONICAL_NAMES))
        return {
            "success": False,
            "message": (
                f"'{folder_name}' is not an allowed folder. "
                f"Allowed: {allowed}"
            ),
            "data": None,
        }

    path = folder_map[key]

    # Create the JARVIS Notes folder if it does not exist yet (safe)
    if "JARVIS_Notes" in str(path):
        path.mkdir(parents=True, exist_ok=True)

    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(["explorer", str(path)], shell=False)  # noqa: S603
        elif system == "Darwin":
            subprocess.Popen(["open", str(path)], shell=False)
        else:
            subprocess.Popen(["xdg-open", str(path)], shell=False)
        logger.info("Opened folder: %s (%s)", key, path)
        return {
            "success": True,
            "message": f"Opening {key} folder: {path}",
            "data": {"folder": key, "path": str(path)},
        }
    except Exception as exc:
        return {"success": False, "message": f"Failed to open folder: {exc}", "data": None}


def register_tools(registry) -> None:
    registry.register(
        ToolDefinition(
            name="open_folder",
            description=(
                "Open an allowlisted user folder (downloads, documents, desktop, "
                "pictures, music, videos, notes, jarvis) in the file manager."
            ),
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.UTILITY,
        ),
        open_folder,
    )
