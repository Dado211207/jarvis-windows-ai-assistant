"""FastAPI route definitions for the JARVIS local API."""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from app import __phase__, __version__
from app.core.brain import brain
from app.core.models import CommandRequest, CommandResponse, ConversationEntry, MemoryEntry, ToolDefinition
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
    db_accessible: bool
    ai_provider: str
    ai_model: str


class HealthResponse(BaseModel):
    """Deliberately minimal — this is the one JSON endpoint that stays
    reachable without a session token (see app/api/local_guard.py), so it
    must never carry database status, provider/API-key status, or anything
    else beyond "is the process up." Version/phase are software identity,
    not application data, so they stay — everything richer (DB integrity,
    brain_configured, provider) lives on the token-protected `/` and
    `/diagnostics` endpoints instead."""
    status: str
    healthy: bool
    version: str
    phase: str


class SystemStatusResponse(BaseModel):
    cpu_percent: float
    ram_percent: float
    uptime: str
    tools_registered: int
    version: str
    phase: str


# --- routes ---

@router.get("/", response_model=StatusResponse)
def root() -> StatusResponse:
    from app.config import settings

    db_ok = False
    try:
        from db.database import get_db
        get_db().get_recent_logs(limit=1)
        db_ok = True
    except Exception:
        pass

    return StatusResponse(
        status="running",
        version=__version__,
        phase=__phase__,
        tools_registered=len(registry),
        brain_configured=brain.is_configured(),
        db_accessible=db_ok,
        ai_provider=settings.jarvis_ai_provider,
        ai_model=settings.jarvis_ai_model,
    )


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", healthy=True, version=__version__, phase=__phase__)


@router.get("/system", response_model=SystemStatusResponse)
def system_status() -> SystemStatusResponse:
    import datetime
    import psutil
    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory().percent
    boot_ts = psutil.boot_time()
    delta = datetime.datetime.now() - datetime.datetime.fromtimestamp(boot_ts)
    total_s = int(delta.total_seconds())
    hours, rem = divmod(total_s, 3600)
    mins = rem // 60
    uptime = f"{hours}h {mins}m"
    return SystemStatusResponse(
        cpu_percent=round(cpu, 1),
        ram_percent=round(ram, 1),
        uptime=uptime,
        tools_registered=len(registry),
        version=__version__,
        phase=__phase__,
    )


@router.get("/conversation", response_model=List[ConversationEntry])
def get_conversation(limit: int = Query(default=50, ge=1, le=200)) -> List[ConversationEntry]:
    from db.database import get_db
    return get_db().get_recent_conversations(limit=limit)


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
