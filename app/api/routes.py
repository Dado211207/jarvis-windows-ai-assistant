"""FastAPI route definitions for the JARVIS local API."""

from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, field_validator

from app import __phase__, __version__
from app.api.session import require_session_token
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
    ai_provider: str
    ai_model: str


class HealthResponse(BaseModel):
    status: str
    healthy: bool
    db: str
    db_accessible: bool
    brain: str
    brain_configured: bool
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
    brain_ok = brain.is_configured()
    return HealthResponse(
        status="ok",
        healthy=True,
        db="ok" if db_ok else "error",
        db_accessible=db_ok,
        brain="claude" if brain_ok else "local",
        brain_configured=brain_ok,
        version=__version__,
        phase=__phase__,
    )


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


@router.post("/command", response_model=CommandResponse, dependencies=[Depends(require_session_token)])
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


@router.post("/voice/speak", dependencies=[Depends(require_session_token)])
def voice_speak(req: SpeakRequest) -> dict:
    from app.config import settings
    if not settings.jarvis_tts_enabled:
        return {
            "success": False,
            "message": "TTS is disabled. Set JARVIS_TTS_ENABLED=true in .env to enable.",
        }
    result = tts_service.speak(req.text)
    return {"success": result.success, "message": result.message}


@router.post("/voice/stop", dependencies=[Depends(require_session_token)])
def voice_stop() -> dict:
    result = tts_service.stop()
    return {"success": result.success, "message": result.message}


# ---------------------------------------------------------------------------
# Push-to-talk speech-to-text (v0.2) — see app/voice/stt.py.
#
# One short recording per request; the browser starts and stops each
# capture explicitly (no continuous/always-listening capture exists in
# this codebase). The uploaded clip is written to exactly one temp file,
# transcribed, and deleted immediately after — success or failure — never
# logged, never committed, never persisted anywhere.
# ---------------------------------------------------------------------------

class STTStatusResponse(BaseModel):
    available: bool
    reason: str


class TranscribeResponse(BaseModel):
    success: bool
    text: str
    message: str


@router.get("/voice/stt-status", response_model=STTStatusResponse)
def voice_stt_status() -> STTStatusResponse:
    from app.voice.stt import stt_service
    available, reason = stt_service.is_available()
    return STTStatusResponse(available=available, reason=reason)


@router.post(
    "/voice/transcribe",
    response_model=TranscribeResponse,
    dependencies=[Depends(require_session_token)],
)
async def voice_transcribe(audio: UploadFile = File(...)) -> TranscribeResponse:
    import os
    import tempfile
    from pathlib import Path

    from app.config import settings
    from app.core.runtime_state import RuntimeState, runtime
    from app.voice.stt import MAX_AUDIO_BYTES, stt_service

    data = await audio.read()
    if not data:
        return TranscribeResponse(success=False, text="", message="No audio received.")
    if len(data) > MAX_AUDIO_BYTES:
        return TranscribeResponse(
            success=False, text="",
            message=f"Audio too large (max {MAX_AUDIO_BYTES // (1024 * 1024)} MB).",
        )

    suffix = Path(audio.filename or "").suffix or ".webm"
    fd, tmp_path_str = tempfile.mkstemp(suffix=suffix, prefix="jarvis_ptt_")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)

        runtime.try_transition(RuntimeState.TRANSCRIBING, reason="push-to-talk transcription")
        result = stt_service.transcribe(tmp_path, timeout_seconds=float(settings.jarvis_stt_timeout_seconds))
        runtime.try_transition(RuntimeState.STANDBY, reason="transcription complete")

        logger.info("Push-to-talk transcription: success=%s chars=%d", result.success, len(result.text))
        return TranscribeResponse(success=result.success, text=result.text, message=result.message)
    finally:
        tmp_path.unlink(missing_ok=True)  # always delete the temp recording, success or failure


# ---------------------------------------------------------------------------
# Privacy mode (v0.2) — read-only status. Toggling goes through POST
# /command ("privacy mode on"/"privacy mode off", see app/core/privacy.py),
# which is already a protected mutation and already publishes an audit
# record + WebSocket event; no separate write endpoint duplicates that.
# ---------------------------------------------------------------------------

class PrivacyStatusResponse(BaseModel):
    active: bool
    changed_at: Optional[str] = None


@router.get("/privacy/status", response_model=PrivacyStatusResponse)
def privacy_status() -> PrivacyStatusResponse:
    from app.core.privacy import privacy_mode
    changed_at = privacy_mode.changed_at
    return PrivacyStatusResponse(
        active=privacy_mode.active,
        changed_at=changed_at.isoformat() if changed_at else None,
    )


# ---------------------------------------------------------------------------
# Anthropic API key storage (v0.2 packaging) — set through this local API
# (the packaged app's first-run/settings UI), stored via the OS credential
# store, never echoed back once submitted. See app/core/credentials.py and
# app/config.py::Settings.effective_api_key for where ANTHROPIC_API_KEY
# (dev/CI) still takes precedence when set.
# ---------------------------------------------------------------------------

class ApiKeyStatusResponse(BaseModel):
    configured: bool


class SetApiKeyRequest(BaseModel):
    api_key: str

    @field_validator("api_key")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("api_key must not be blank")
        return value


class ApiKeyActionResponse(BaseModel):
    success: bool
    message: str


@router.get("/settings/api-key-status", response_model=ApiKeyStatusResponse)
def api_key_status() -> ApiKeyStatusResponse:
    from app.config import settings
    return ApiKeyStatusResponse(configured=settings.has_anthropic_key)


@router.post(
    "/settings/api-key",
    response_model=ApiKeyActionResponse,
    dependencies=[Depends(require_session_token)],
)
def set_api_key(req: SetApiKeyRequest) -> ApiKeyActionResponse:
    from app.core.credentials import set_stored_api_key
    if set_stored_api_key(req.api_key.strip()):
        logger.info("Anthropic API key stored via the OS credential store.")
        return ApiKeyActionResponse(success=True, message="API key saved.")
    return ApiKeyActionResponse(success=False, message="Could not save the key to the OS credential store.")


@router.post(
    "/settings/api-key/remove",
    response_model=ApiKeyActionResponse,
    dependencies=[Depends(require_session_token)],
)
def remove_api_key() -> ApiKeyActionResponse:
    from app.core.credentials import clear_stored_api_key
    if clear_stored_api_key():
        logger.info("Anthropic API key removed from the OS credential store.")
        return ApiKeyActionResponse(success=True, message="API key removed.")
    return ApiKeyActionResponse(success=False, message="Could not remove the key from the OS credential store.")


# ---------------------------------------------------------------------------
# First-run onboarding readiness (v0.2 packaging). One place that answers
# "is JARVIS actually ready to use, and if not, exactly which part isn't" —
# the packaged app must never send a user to .env or a config folder to
# find this out themselves. Microphone readiness is deliberately absent
# here: only the browser can know whether a mic device/permission exists
# (see app/ui/static/app.js's own getUserMedia-based check on the
# onboarding page), a server process cannot observe it.
# ---------------------------------------------------------------------------

class ReadinessItem(BaseModel):
    ready: bool
    detail: str


class OnboardingReadinessResponse(BaseModel):
    core: ReadinessItem
    text_chat: ReadinessItem
    ai_provider: ReadinessItem
    mode: ReadinessItem
    stt_runtime: ReadinessItem
    speech_model: ReadinessItem
    tts: ReadinessItem
    database: ReadinessItem
    windows_automation: ReadinessItem


@router.get("/onboarding/readiness", response_model=OnboardingReadinessResponse)
def onboarding_readiness() -> OnboardingReadinessResponse:
    import platform

    from app.config import settings
    from app.voice.stt import stt_service
    from app.voice.tts import tts_service

    db_ok = False
    try:
        from db.database import get_db
        get_db().get_recent_logs(limit=1)
        db_ok = True
    except Exception:
        pass

    ai_ready = settings.has_anthropic_key
    stt_ready, stt_detail = stt_service.is_available()
    model_ready, model_detail = stt_service.model_status()
    tts_ready = tts_service.is_available()
    is_windows = platform.system() == "Windows"

    return OnboardingReadinessResponse(
        core=ReadinessItem(ready=True, detail="JARVIS core is running."),
        text_chat=ReadinessItem(ready=True, detail="Text chat works regardless of any other setting below."),
        ai_provider=ReadinessItem(
            ready=ai_ready,
            detail="Anthropic API key configured." if ai_ready else "No Anthropic API key configured yet.",
        ),
        mode=ReadinessItem(
            ready=True,
            detail=(
                "Cloud AI mode — natural-language questions use Claude."
                if ai_ready else
                "Local-only mode — deterministic commands work; natural-language chat uses simple local replies."
            ),
        ),
        stt_runtime=ReadinessItem(ready=stt_ready, detail=stt_detail),
        speech_model=ReadinessItem(ready=model_ready, detail=model_detail),
        tts=ReadinessItem(
            ready=tts_ready,
            detail="Local text-to-speech engine available." if tts_ready else "pyttsx3 is not available on this system.",
        ),
        database=ReadinessItem(ready=db_ok, detail="SQLite database is accessible." if db_ok else "SQLite database could not be reached."),
        windows_automation=ReadinessItem(
            ready=True,
            detail=(
                "Windows action tools active (app launcher, folders, safe URLs)."
                if is_windows else
                "Running outside Windows — Windows-specific actions use a limited POSIX fallback (development only)."
            ),
        ),
    )


class OnboardingCompleteResponse(BaseModel):
    success: bool


@router.get("/onboarding/complete", response_model=OnboardingCompleteResponse)
def onboarding_complete_status() -> OnboardingCompleteResponse:
    from app.core.onboarding import is_onboarding_complete
    return OnboardingCompleteResponse(success=is_onboarding_complete())


@router.post(
    "/onboarding/complete",
    response_model=OnboardingCompleteResponse,
    dependencies=[Depends(require_session_token)],
)
def mark_onboarding_complete() -> OnboardingCompleteResponse:
    from app.core.onboarding import mark_onboarding_complete as _mark
    _mark()
    return OnboardingCompleteResponse(success=True)


# ---------------------------------------------------------------------------
# Guided speech-model download (v0.2 packaging) — see
# app/voice/model_installer.py for the full honesty/verification story.
# Never starts without an explicit POST from the user; GET endpoints are
# read-only previews/status.
# ---------------------------------------------------------------------------

class ModelFileInfoResponse(BaseModel):
    name: str
    size: int
    sha256_verified: bool


class SpeechModelInfoResponse(BaseModel):
    available: bool
    repo: Optional[str] = None
    display_name: Optional[str] = None
    license: Optional[str] = None
    source_url: Optional[str] = None
    destination: Optional[str] = None
    language_note: Optional[str] = None
    total_size: Optional[int] = None
    files: List[ModelFileInfoResponse] = []
    error: Optional[str] = None


@router.get("/onboarding/speech-model/info", response_model=SpeechModelInfoResponse)
def speech_model_info() -> SpeechModelInfoResponse:
    from app.voice.model_installer import fetch_model_info
    try:
        info = fetch_model_info()
    except Exception:
        logger.warning("Could not fetch speech model info.", exc_info=True)
        return SpeechModelInfoResponse(available=False, error="Could not reach Hugging Face to check the model. Check your connection and try again.")

    return SpeechModelInfoResponse(
        available=True,
        repo=info.repo,
        display_name=info.display_name,
        license=info.license,
        source_url=info.source_url,
        destination=info.destination,
        language_note=info.language_note,
        total_size=info.total_size,
        files=[ModelFileInfoResponse(name=f.name, size=f.size, sha256_verified=f.sha256 is not None) for f in info.files],
    )


class SpeechModelInstallStatusResponse(BaseModel):
    status: str
    current_file: str
    bytes_downloaded: int
    bytes_total: int
    message: str


def _installer_state_response() -> SpeechModelInstallStatusResponse:
    from app.voice.model_installer import model_installer
    state = model_installer.state()
    return SpeechModelInstallStatusResponse(
        status=state.status,
        current_file=state.current_file,
        bytes_downloaded=state.bytes_downloaded,
        bytes_total=state.bytes_total,
        message=state.message,
    )


@router.get("/onboarding/speech-model/install-status", response_model=SpeechModelInstallStatusResponse)
def speech_model_install_status() -> SpeechModelInstallStatusResponse:
    return _installer_state_response()


@router.post(
    "/onboarding/speech-model/install",
    response_model=SpeechModelInstallStatusResponse,
    dependencies=[Depends(require_session_token)],
)
def speech_model_install_start() -> SpeechModelInstallStatusResponse:
    from app.voice.model_installer import model_installer
    model_installer.start()  # False (already running) is not an error — status reflects the truth either way
    return _installer_state_response()


@router.post(
    "/onboarding/speech-model/cancel",
    response_model=SpeechModelInstallStatusResponse,
    dependencies=[Depends(require_session_token)],
)
def speech_model_install_cancel() -> SpeechModelInstallStatusResponse:
    from app.voice.model_installer import model_installer
    model_installer.cancel()
    return _installer_state_response()
