"""Long-term memory: things the user explicitly asked JARVIS to remember.

Deleting is deliberately split in two. Removing *one* entry is an
ordinary correction — the user is looking at the thing they are
deleting, in a list, and clicking it. Removing *everything* is not: it
is irreversible, it is easy to trigger by accident or by a
misheard/mistyped command, and there is nothing on screen identifying
what would be lost. So the bulk version is registered as an
approval-required tool and goes through the same gate as clearing the
action log, while the single-entry delete is a plain endpoint the page
calls after its own confirmation.
"""

from typing import List

from pydantic import BaseModel

from app.core.models import MemoryEntry, PermissionLevel, RiskLevel, ToolCategory, ToolDefinition
from app.logging_config import get_logger

logger = get_logger("memory")


class AddMemoryInput(BaseModel):
    content: str
    tags: str = ""


def add_memory(content: str, tags: str = "") -> dict:
    from app.core.privacy import privacy_mode

    if privacy_mode.active:
        logger.info("Memory write rejected: privacy mode is active.")
        return {
            "success": False,
            "message": "Memory was not saved: privacy mode is on. Turn it off to save long-term memories.",
            "data": None,
        }

    from db.database import get_db

    db = get_db()
    entry_id = db.add_memory(content=content, tags=tags or None)
    logger.info("Memory added (id=%s)", entry_id)
    return {
        "success": True,
        "message": f"Memory saved (id={entry_id}).",
        "data": {"id": entry_id, "content": content},
    }


def search_memory(query: str) -> dict:
    from db.database import get_db

    db = get_db()
    results: List[MemoryEntry] = db.search_memory(query)
    if not results:
        return {
            "success": True,
            "message": f"No memories found matching '{query}'.",
            "data": [],
        }
    items = [{"id": m.id, "content": m.content, "tags": m.tags} for m in results]
    return {
        "success": True,
        "message": f"Found {len(items)} memory entries.",
        "data": items,
    }


def delete_memory(memory_id: int) -> dict:
    """Delete one memory. Not gated: the user is looking at the entry
    they are removing, and the page confirms first."""
    from db.database import get_db

    try:
        removed = get_db().delete_memory(int(memory_id))
    except (TypeError, ValueError):
        return {"success": False, "message": "That is not a valid memory reference.", "data": None}

    if not removed:
        return {"success": False, "message": "That memory no longer exists.", "data": None}

    logger.info("Memory deleted (id=%s)", memory_id)
    return {"success": True, "message": "Memory deleted.", "data": {"id": int(memory_id)}}


def clear_memory() -> dict:
    """Delete every stored memory. Approval-required — see the module
    docstring for why this is not the same decision as deleting one."""
    from db.database import get_db

    removed = get_db().clear_memories()
    logger.info("All memories cleared (%d removed).", removed)
    return {
        "success": True,
        "message": (
            f"All {removed} memories deleted."
            if removed else "There were no memories to delete."
        ),
        "data": {"removed": removed},
    }


def register_tools(registry) -> None:
    registry.register(
        ToolDefinition(
            name="add_memory",
            description="Save a note or piece of information to long-term memory.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.REVERSIBLE,
            input_model=AddMemoryInput,
        ),
        add_memory,
    )
    registry.register(
        ToolDefinition(
            name="search_memory",
            description="Search stored memories by keyword.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.READ_ONLY,
            verification_strategy="Read-only search — nothing is changed.",
        ),
        search_memory,
    )
    registry.register(
        ToolDefinition(
            name="clear_memory",
            description=(
                "Delete every saved memory from the local database. Requires explicit "
                "confirmation and cannot be undone."
            ),
            permission_level=PermissionLevel.APPROVAL_REQUIRED,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.SENSITIVE,
            reversible=False,
            verification_strategy="Handler reports how many rows were removed.",
        ),
        clear_memory,
    )
