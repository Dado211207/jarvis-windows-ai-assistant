from typing import List

from app.core.models import MemoryEntry, PermissionLevel, ToolCategory, ToolDefinition
from app.logging_config import get_logger

logger = get_logger("memory")


def add_memory(content: str, tags: str = "") -> dict:
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


def register_tools(registry) -> None:
    from app.core.tool_registry import ToolRegistry

    registry.register(
        ToolDefinition(
            name="add_memory",
            description="Save a note or piece of information to long-term memory.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.MEMORY,
        ),
        add_memory,
    )
    registry.register(
        ToolDefinition(
            name="search_memory",
            description="Search stored memories by keyword.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.MEMORY,
        ),
        search_memory,
    )
