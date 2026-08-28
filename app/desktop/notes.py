"""Safe notes — text notes confined to ~/Documents/JARVIS_Notes/.

No arbitrary file paths. No system folders. No deletion, ever: a note is
removed by the person who wrote it, in their file manager, not by
something they said to an assistant.

Notes can be written, listed and read back. Reading was missing at
first, which made the feature half of itself — somewhere to put things
and no way to get them out again without leaving the app.

Every path that leaves this module goes through `_note_in_dir()`, which
resolves and re-checks containment. A note is addressed by *filename*
only, never a path: a name containing a separator or `..` is refused
outright rather than sanitised into something that might still escape.
"""
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from app.core.models import PermissionLevel, RiskLevel, ToolCategory, ToolDefinition
from app.logging_config import get_logger

logger = get_logger("desktop.notes")

NOTES_DIR = Path.home() / "Documents" / "JARVIS_Notes"

# A note is a small text file; this bounds what a single read can pull
# into a chat message, not what a user may store.
MAX_NOTE_READ_BYTES = 20_000
MAX_NOTES_LISTED = 50


class ReadNoteInput(BaseModel):
    filename: str


def _sanitize_filename(text: str) -> str:
    """Return a safe filename fragment derived from arbitrary text."""
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "", text)
    safe = re.sub(r"\s+", "_", safe.strip())
    return safe[:50] or "note"


def _note_in_dir(filename: str) -> Optional[Path]:
    """The path of a note inside NOTES_DIR, or None if *filename* is not
    a plain name that stays inside it.

    Refuses rather than repairs. A name like "../../.ssh/id_rsa" is not a
    typo to be cleaned up — it is the thing this check exists to stop,
    and quietly turning it into a valid name would hide that.
    """
    name = (filename or "").strip()
    if not name or name in (".", ".."):
        return None
    if "/" in name or "\\" in name or ".." in name:
        return None
    if Path(name).name != name:  # anything the OS still reads as a path
        return None

    candidate = NOTES_DIR / name
    try:
        candidate.resolve().relative_to(NOTES_DIR.resolve())
    except (ValueError, OSError):
        logger.warning("Refused a note path outside the notes folder: %r", filename)
        return None
    return candidate


def list_notes() -> dict:
    """List the notes in the JARVIS Notes folder, newest first."""
    if not NOTES_DIR.exists():
        return {
            "success": True,
            "message": "You have no notes yet. Create one with: create note <text>",
            "data": {"notes": [], "notes_dir": str(NOTES_DIR)},
        }

    try:
        files = [p for p in NOTES_DIR.glob("*.txt") if p.is_file()]
    except OSError as exc:
        logger.error("Could not list notes: %s", exc)
        return {"success": False, "message": "Could not read the notes folder.", "data": None}

    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    shown = files[:MAX_NOTES_LISTED]
    notes = [
        {"filename": p.name, "size_bytes": p.stat().st_size,
         "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
        for p in shown
    ]

    if not notes:
        return {
            "success": True,
            "message": "You have no notes yet. Create one with: create note <text>",
            "data": {"notes": [], "notes_dir": str(NOTES_DIR)},
        }

    lines = [f"{len(files)} note(s) in {NOTES_DIR}:"]
    if len(files) > len(shown):
        lines[0] = f"{len(files)} note(s) in {NOTES_DIR} — showing the {len(shown)} most recent:"
    lines += [f"  {n['modified']}  {n['filename']}" for n in notes]
    lines.append("Read one with: read note <filename>")

    return {
        "success": True,
        "message": "\n".join(lines),
        "data": {"notes": notes, "total": len(files), "notes_dir": str(NOTES_DIR)},
    }


def read_note(filename: str) -> dict:
    """Read one note back by filename."""
    note_path = _note_in_dir(filename)
    if note_path is None:
        return {
            "success": False,
            "message": "That is not a valid note name. Use the filename shown by: list notes",
            "data": None,
        }
    if not note_path.is_file():
        return {
            "success": False,
            "message": f"No note named '{note_path.name}'. See your notes with: list notes",
            "data": None,
        }

    try:
        raw = note_path.read_bytes()
    except OSError as exc:
        logger.error("Could not read note %s: %s", note_path.name, exc)
        return {"success": False, "message": "That note could not be read.", "data": None}

    truncated = len(raw) > MAX_NOTE_READ_BYTES
    text = raw[:MAX_NOTE_READ_BYTES].decode("utf-8", "replace")
    message = f"{note_path.name}:\n{text}"
    if truncated:
        message += f"\n… (showing the first {MAX_NOTE_READ_BYTES} characters)"

    return {
        "success": True,
        "message": message,
        "data": {"filename": note_path.name, "content": text, "truncated": truncated},
    }


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
            risk=RiskLevel.REVERSIBLE,
        ),
        create_note,
    )
    registry.register(
        ToolDefinition(
            name="list_notes",
            description="List the notes saved in ~/Documents/JARVIS_Notes/, newest first.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.READ_ONLY,
            verification_strategy="Read-only listing — nothing is changed.",
        ),
        list_notes,
    )
    registry.register(
        ToolDefinition(
            name="read_note",
            description=(
                "Read one saved note back by filename. Only reads inside "
                "~/Documents/JARVIS_Notes/; a name that is not a plain filename is refused."
            ),
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.READ_ONLY,
            input_model=ReadNoteInput,
            verification_strategy="Read-only — nothing is changed.",
        ),
        read_note,
    )
