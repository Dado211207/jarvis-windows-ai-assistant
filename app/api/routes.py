"""FastAPI route definitions for the JARVIS local API."""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from app import __phase__, __version__
from app.core.brain import brain
from app.core.models import CommandRequest, CommandResponse, MemoryEntry, ToolDefinition
from app.core.tool_registry import registry
from app.logging_config import get_logger
from app.voice.tts import MAX_SPEAK_LENGTH, tts_service

logger = get_logger("api.routes")

router = APIRouter()


# --- response schemas ---

class StatusResponse(BaseModel):
    status: str
    version: str
    phase: str
    tools_registered: int
    brain_configured: bool
    ai_provider: str
    ai_model: str


class HealthResponse(BaseModel):
    healthy: bool
    db_accessible: bool
    brain_configured: bool
    version: str


# --- routes ---

@router.get("/", response_model=StatusResponse)
def root() -> StatusResponse:
    from app.config import settings
    return StatusResponse(
        status="running",
        version=__version__,
        phase=__phase__,
        tools_registered=len(registry),
        brain_configured=brain.is_configured(),
        ai_provider=settings.jarvis_ai_provider,
        ai_model=settings.jarvis_ai_model,
    )


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
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
        brain_configured=brain.is_configured(),
        version=__version__,
    )


@router.post("/command", response_model=CommandResponse)
def run_command(req: CommandRequest) -> CommandResponse:
    logger.info("API command: %r", req.command)
    return brain.process(req.command)


@router.get("/tools", response_model=List[ToolDefinition])
def list_tools() -> List[ToolDefinition]:
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


# ---------------------------------------------------------------------------
# Voice / TTS endpoints (Phase 3)  —  local-only, no cloud TTS
# ---------------------------------------------------------------------------

class SpeakRequest(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _validate_text(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be empty")
        if len(v) > MAX_SPEAK_LENGTH:
            raise ValueError(f"text too long (max {MAX_SPEAK_LENGTH} chars)")
        return v


class VoiceStatusResponse(BaseModel):
    tts_enabled: bool
    tts_engine: str
    tts_available: bool


@router.get("/voice/status", response_model=VoiceStatusResponse)
def voice_status() -> VoiceStatusResponse:
    from app.config import settings
    return VoiceStatusResponse(
        tts_enabled=settings.jarvis_tts_enabled,
        tts_engine=settings.jarvis_tts_engine,
        tts_available=tts_service.is_available(),
    )


@router.post("/voice/speak")
def voice_speak(req: SpeakRequest) -> dict:
    from app.config import settings
    if not settings.jarvis_tts_enabled:
        return {
            "success": False,
            "message": "TTS is disabled. Set JARVIS_TTS_ENABLED=true in .env to enable.",
        }
    result = tts_service.speak(req.text)
    return {"success": result.success, "message": result.message}


@router.post("/voice/stop")
def voice_stop() -> dict:
    result = tts_service.stop()
    return {"success": result.success, "message": result.message}
