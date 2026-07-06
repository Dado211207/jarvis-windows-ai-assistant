"""Safe note creator — writes text notes only inside ~/Documents/JARVIS_Notes/.

No arbitrary file paths. No system folders. No deletion.
"""
import re
from datetime import datetime
from pathlib import Path

from app.core.models import PermissionLevel, ToolCategory, ToolDefinition
from app.logging_config import get_logger

logger = get_logger("desktop.notes")

NOTES_DIR = Path.home() / "Documents" / "JARVIS_Notes"


def _sanitize_filename(text: str) -> str:
    """Return a safe filename fragment derived from arbitrary text."""
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "", text)
    safe = re.sub(r"\s+", "_", safe.strip())
    return safe[:50] or "note"


def create_note(content: str) -> dict:
    """Create a timestamped text note in the JARVIS Notes folder."""
    content = content.strip()
    if not content:
        return {"success": False, "message": "Note content cannot be empty.", "data": None}

    NOTES_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now()
    date_str = timestamp.strftime("%Y%m%d_%H%M%S")
    fragment = _sanitize_filename(content[:40])
    filename = f"{date_str}_{fragment}.txt"
    note_path = NOTES_DIR / filename

    # Safety: verify the resolved path stays inside NOTES_DIR
    try:
        note_path.resolve().relative_to(NOTES_DIR.resolve())
    except ValueError:
        logger.error("Path traversal attempt blocked: %s", note_path)
        return {"success": False, "message": "Invalid note path.", "data": None}

    body = f"Date: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n---\n{content}\n"

    try:
        note_path.write_text(body, encoding="utf-8")
        logger.info("Note created: %s", note_path)
        return {
            "success": True,
            "message": f"Note saved: {filename}",
            "data": {
                "filename": filename,
                "path": str(note_path),
                "notes_dir": str(NOTES_DIR),
            },
        }
    except Exception as exc:
        logger.error("Failed to create note: %s", exc)
        return {"success": False, "message": f"Failed to create note: {exc}", "data": None}


def register_tools(registry) -> None:
    registry.register(
        ToolDefinition(
            name="create_note",
            description=(
                "Create a timestamped text note inside ~/Documents/JARVIS_Notes/. "
                "Never writes outside the JARVIS Notes directory."
            ),
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.MEMORY,
        ),
        create_note,
    )
