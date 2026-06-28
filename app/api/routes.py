"""FastAPI route definitions for the JARVIS local API."""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app import __phase__, __version__
from app.core.brain import brain
from app.core.models import CommandRequest, CommandResponse, MemoryEntry, ToolDefinition
from app.logging_config import get_logger

logger = get_logger("api.routes")

router = APIRouter()


# --- response schemas ---

class StatusResponse(BaseModel):
    status: str
    version: str
    phase: str
    tools_registered: int


class HealthResponse(BaseModel):
    healthy: bool
    db_accessible: bool
    version: str


# --- routes ---

@router.get("/", response_model=StatusResponse)
def root() -> StatusResponse:
    return StatusResponse(
        status="running",
        version=__version__,
        phase=__phase__,
        tools_registered=len(brain._registry) if hasattr(brain, "_registry") else 0,
    )


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    from app.core.tool_registry import registry

    db_ok = False
    try:
        from db.database import get_db
        get_db().get_recent_logs(limit=1)
        db_ok = True
    except Exception:
        pass
    return HealthResponse(
        healthy=True,
        db_accessible=db_ok,
        version=__version__,
    )


@router.post("/command", response_model=CommandResponse)
def run_command(req: CommandRequest) -> CommandResponse:
    logger.info("API command: %r", req.command)
    return brain.process(req.command)


@router.get("/tools", response_model=List[ToolDefinition])
def list_tools() -> List[ToolDefinition]:
    from app.core.tool_registry import registry
    return registry.list_definitions()


@router.get("/memory/search", response_model=List[MemoryEntry])
def search_memory(q: str = Query(..., min_length=1, description="Search query")) -> List[MemoryEntry]:
    from db.database import get_db
    results = get_db().search_memory(q)
    return results


@router.get("/memory", response_model=List[MemoryEntry])
def list_memory(limit: int = Query(default=20, ge=1, le=100)) -> List[MemoryEntry]:
    from db.database import get_db
    return get_db().get_all_memories(limit=limit)


@router.get("/logs")
def recent_logs(limit: int = Query(default=20, ge=1, le=100)):
    from db.database import get_db
    logs = get_db().get_recent_logs(limit=limit)
    return [l.model_dump() for l in logs]
