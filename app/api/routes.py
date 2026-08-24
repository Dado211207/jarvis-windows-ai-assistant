"""FastAPI route definitions for the JARVIS local API."""

from typing import List, Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
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


@router.get("/conversation", response_model=List[ConversationEntry], dependencies=[Depends(require_session_token)])
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


@router.get("/memory/search", response_model=List[MemoryEntry], dependencies=[Depends(require_session_token)])
def search_memory(q: str = Query(..., min_length=1, description="Search query")) -> List[MemoryEntry]:
    from db.database import get_db
    results = get_db().search_memory(q)
    return results


class AddMemoryRequest(BaseModel):
    content: str
    tags: str = ""

    @field_validator("content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class MemoryActionResponse(BaseModel):
    success: bool
    message: str


@router.post(
    "/memory",
    response_model=MemoryActionResponse,
    dependencies=[Depends(require_session_token)],
)
def create_memory(req: AddMemoryRequest) -> MemoryActionResponse:
    """Save a memory from the Memory page.

    Goes through the same handler as the `memory add` command, so the
    privacy-mode refusal is identical whichever way it is reached — there
    is no path that saves while privacy mode says nothing should be
    saved.
    """
    from app.core.memory import add_memory

    result = add_memory(req.content.strip(), req.tags.strip())
    return MemoryActionResponse(success=result["success"], message=result["message"])


@router.post(
    "/memory/{memory_id}/delete",
    response_model=MemoryActionResponse,
    dependencies=[Depends(require_session_token)],
)
def remove_memory(memory_id: int) -> MemoryActionResponse:
    """Delete one memory. Deleting *everything* is a different decision
    and goes through the approval gate — see app/core/memory.py."""
    from app.core.memory import delete_memory

    result = delete_memory(memory_id)
    return MemoryActionResponse(success=result["success"], message=result["message"])


@router.get("/memory", response_model=List[MemoryEntry], dependencies=[Depends(require_session_token)])
def list_memory(limit: int = Query(default=20, ge=1, le=100)) -> List[MemoryEntry]:
    from db.database import get_db
    return get_db().get_all_memories(limit=limit)


@router.get("/logs", dependencies=[Depends(require_session_token)])
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


class SetVoiceOutputRequest(BaseModel):
    enabled: bool


@router.get("/voice/status", response_model=VoiceStatusResponse)
def voice_status() -> VoiceStatusResponse:
    """`tts_engine` is the engine actually speaking, not the configured
    one. It used to report `settings.jarvis_tts_engine`, a fixed string
    reading "pyttsx3" — which stopped being true the moment speech could
    come from any of three engines, and would have named the robotic one
    while the neural voice was talking. See /voice/engine-status for the
    full per-tier picture."""
    from app.voice import engines

    return VoiceStatusResponse(
        tts_enabled=tts_service.output_enabled,
        tts_engine=engines.DISPLAY_NAMES[tts_service.active_engine()],
        tts_available=tts_service.is_available(),
    )


@router.post(
    "/voice/output",
    response_model=VoiceStatusResponse,
    dependencies=[Depends(require_session_token)],
)
def set_voice_output(req: SetVoiceOutputRequest) -> VoiceStatusResponse:
    """Turn spoken replies on or off, remembered across restarts.

    Returns the state actually in effect, not the requested one — see
    tts_service.set_output_enabled().
    """
    tts_service.set_output_enabled(req.enabled)
    return voice_status()


@router.post("/voice/speak", dependencies=[Depends(require_session_token)])
def voice_speak(req: SpeakRequest) -> dict:
    """Speak text *automatically*, if the user turned spoken replies on.

    This is the path the chat page uses to read each new answer aloud by
    itself. The gate lives here rather than in the browser so a stale
    page cannot keep narrating after speech was switched off in another
    tab or by a `speak off` command.

    A person pressing the speaker button on one message is asking for
    something different and goes to /voice/speak-once below.
    """
    if not tts_service.output_enabled:
        return {
            "success": False,
            "message": "Voice output is turned off. Turn it on from the Voice page.",
        }
    result = tts_service.speak(req.text)
    return {"success": result.success, "message": result.message}


@router.post("/voice/speak-once", dependencies=[Depends(require_session_token)])
def voice_speak_once(req: SpeakRequest) -> dict:
    """Read one specific thing aloud because the user just asked for it.

    Deliberately not gated on `output_enabled`. That flag answers "speak
    every reply automatically"; someone who has just pressed a speaker
    button on a particular message has asked for that message, and
    refusing because a different setting is off is the defect this
    release exists to fix — JARVIS declining to use the voice it has.
    `tts_test` has always worked exactly this way, so there is still one
    flag and not two (CLAUDE.md's Phase 3 rule).

    The gate that matters is still here and still server-side: an
    unavailable engine is refused with the engine's own reason and the
    step that fixes it, and the endpoint requires the session token like
    every other mutating route. Nothing about this lets a stale page
    narrate on its own — it can only speak when a person presses a
    button on it, and the same person can press stop.
    """
    if not tts_service.is_available():
        from app.voice import engines

        return {
            "success": False,
            "message": engines.unavailable_message(tts_service.voice_key),
        }
    result = tts_service.speak(req.text)
    return {"success": result.success, "message": result.message}


@router.get("/voice/speaking")
def voice_speaking() -> dict:
    """Whether audio is playing right now.

    The speech service returns as soon as playback *starts*, so a page
    showing a Stop button has no other way to know when to put it back —
    and a Stop button left showing after the sound finished is a control
    that lies about the state of the machine.
    """
    from app.voice import engines

    try:
        return {"speaking": bool(engines.is_speaking())}
    except Exception:  # noqa: BLE001
        logger.debug("Could not read speech state.", exc_info=True)
        return {"speaking": False}


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


class STTDiagnosticsResponse(BaseModel):
    """Everything the server can honestly say about voice input.

    Deliberately several fields rather than one "available" boolean: a
    switched-off feature, a missing engine, a missing model and a model
    in a folder JARVIS is not looking at are four different problems, and
    the single "Speech runtime — Not ready" line that used to be shown
    could not tell them apart. The browser fills in what only it can
    know — microphone permission, the selected input device, the live
    level — since a server process cannot observe any of those.
    """

    enabled: bool
    available: bool
    reason: str
    runtime_ready: bool
    runtime_detail: str
    model_ready: bool
    model_detail: str
    model_path: str
    # One of app/voice/input_state.py's ten states, plus what to do about
    # it. Six accurate rows and no diagnosis is what the release
    # candidate shipped; this is the row that says which situation the
    # machine is actually in.
    state: str = ""
    headline: str = ""
    next_step: str = ""
    percent: int = 0
    last_failure: str = ""


class SetSTTEnabledRequest(BaseModel):
    enabled: bool


@router.get("/voice/stt-status", response_model=STTStatusResponse)
def voice_stt_status() -> STTStatusResponse:
    from app.voice.stt import stt_service
    available, reason = stt_service.is_available()
    return STTStatusResponse(available=available, reason=reason)


@router.get("/voice/diagnostics", response_model=STTDiagnosticsResponse, dependencies=[Depends(require_session_token)])
def voice_diagnostics() -> STTDiagnosticsResponse:
    from app.voice import input_state
    from app.voice.stt import input_enabled, stt_service

    available, reason = stt_service.is_available()
    runtime_ready, runtime_detail = stt_service.runtime_status()
    model_ready, model_detail = stt_service.model_status()
    path = stt_service.model_path()
    overall = input_state.describe()
    return STTDiagnosticsResponse(
        enabled=input_enabled(),
        available=available,
        reason=reason,
        runtime_ready=runtime_ready,
        runtime_detail=runtime_detail,
        model_ready=model_ready,
        model_detail=model_detail,
        # A path, never file contents. It is shown so someone can see
        # *where* JARVIS is looking, which is the difference between "no
        # model" and "a model somewhere else".
        model_path=str(path) if path is not None else "",
        state=overall.state,
        headline=overall.headline,
        next_step=overall.next_step,
        percent=overall.percent,
        last_failure=input_state.last_failure.message(),
    )


@router.post(
    "/voice/input-enabled",
    response_model=STTDiagnosticsResponse,
    dependencies=[Depends(require_session_token)],
)
def set_voice_input_enabled(req: SetSTTEnabledRequest) -> STTDiagnosticsResponse:
    """Turn push-to-talk on or off.

    The control the old status message already promised ("Turn it on from
    the Voice page") and which did not exist. Turning it off hides the
    feature; it is not the thing that stops the microphone being opened —
    that is the button press and the OS permission prompt, both unchanged.
    """
    from app.core.preferences import store

    store("stt_enabled", "true" if req.enabled else "false")
    logger.info("Voice input %s.", "enabled" if req.enabled else "disabled")
    return voice_diagnostics()


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

        # The diagnostics panel needs to know the last attempt failed, or
        # push-to-talk looks like a button that simply does nothing. The
        # *message* only — never the audio, never the transcript.
        from app.voice.input_state import last_failure

        if result.success:
            last_failure.clear()
        else:
            last_failure.record(result.message)

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


class StoredDataItem(BaseModel):
    key: str
    label: str
    count: int
    detail: str


class StoredDataResponse(BaseModel):
    items: List[StoredDataItem]
    location: str
    encrypted: bool


@router.get("/privacy/status", response_model=PrivacyStatusResponse)
def privacy_status() -> PrivacyStatusResponse:
    from app.core.privacy import privacy_mode
    changed_at = privacy_mode.changed_at
    return PrivacyStatusResponse(
        active=privacy_mode.active,
        changed_at=changed_at.isoformat() if changed_at else None,
    )


@router.get("/privacy/data", response_model=StoredDataResponse, dependencies=[Depends(require_session_token)])
def stored_data() -> StoredDataResponse:
    """What JARVIS is holding about you, counted.

    "Your data stays local" is easy to write and impossible for a user to
    check. This makes it checkable: how many rows of each kind exist,
    where the file is, and — stated plainly rather than omitted — that
    the file is not encrypted.

    Counts only. No content is returned, so this stays safe to render on
    a page that may be open while someone is looking over a shoulder.
    """
    from app.config import settings

    def _count(table: str) -> int:
        try:
            from db.database import get_db
            return get_db().count_rows(table)
        except Exception:  # noqa: BLE001 — a missing DB reads as "nothing stored"
            logger.warning("Could not count rows in %s.", table, exc_info=True)
            return 0

    return StoredDataResponse(
        items=[
            StoredDataItem(
                key="memories", label="Saved memories", count=_count("memories"),
                detail="Things you explicitly asked JARVIS to remember.",
            ),
            StoredDataItem(
                key="conversations", label="Chat messages", count=_count("conversations"),
                detail="Your side and JARVIS's side of past conversations.",
            ),
            StoredDataItem(
                key="action_logs", label="Action log entries", count=_count("action_logs"),
                detail="What was run, and whether it worked.",
            ),
            StoredDataItem(
                key="action_lifecycle", label="Audit records", count=_count("action_lifecycle"),
                detail="The full record of every action proposed on this computer.",
            ),
        ],
        location=str(settings.db_path),
        encrypted=False,
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
    # Which kind of failure, when there was one, so the page can say
    # something true instead of one generic sentence. None on success.
    category: Optional[str] = None
    # Whether the key ended up in the credential store. Distinct from
    # `success`: a key that is valid but could not be checked (offline,
    # rate-limited) is still stored, so the user does not have to type it
    # again later.
    stored: bool = False


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
    """Try the key, then store it.

    A key saved without being tried is a key whose first failure happens
    later, mid-conversation, with no visible connection to the setup
    screen where it was typed. So it is tried once here, and the four
    outcomes the user might hit — rejected, unfunded, rate-limited,
    unreachable — are reported as four different things.

    Only an outright rejection stops the key being stored: being offline
    during setup says nothing about the key, and making someone re-enter
    it afterwards would be punishing them for their network.
    """
    from app.core.ai.key_check import verify_anthropic_key
    from app.core.credentials import set_stored_api_key

    key = req.api_key.strip()
    verification = verify_anthropic_key(key)

    if not verification.ok and not verification.worth_storing:
        return ApiKeyActionResponse(
            success=False,
            message=verification.message,
            category=verification.category.value if verification.category else None,
            stored=False,
        )

    if not set_stored_api_key(key):
        return ApiKeyActionResponse(
            success=False,
            message="Could not save the key to the Windows credential store.",
            category="credential_store",
            stored=False,
        )

    logger.info("Anthropic API key stored via the OS credential store.")
    if verification.ok:
        return ApiKeyActionResponse(success=True, message="API key saved and verified.", stored=True)
    return ApiKeyActionResponse(
        success=False,
        message=f"Key saved, but it could not be checked right now. {verification.message}",
        category=verification.category.value if verification.category else None,
        stored=True,
    )


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
# The desktop readiness signal.
#
# Published by the launcher parent, which is the only process that can
# know all four facts, and served here because the parent has no HTTP
# surface of its own. See app/launcher/desktop_ready.py for why each fact
# is proved rather than assumed, and why a boot-trace log line was the
# wrong contract to build on.
#
# Writing is authenticated with the per-session secret the parent and
# server child already share, so nothing else on the machine can declare
# the desktop ready. Reading is open: it carries no user data, and
# answering "not yet" to anything that asks is harmless.
# ---------------------------------------------------------------------------

class DesktopReadyResponse(BaseModel):
    ready: bool = False
    server_healthy: bool = False
    window_alive: bool = False
    tray_listening: bool = False
    parent_running: bool = False
    session_id: str = ""
    detail: str = ""
    missing: List[str] = []


class PublishDesktopReadyRequest(BaseModel):
    server_healthy: bool = False
    window_alive: bool = False
    tray_listening: bool = False
    parent_running: bool = False
    session_id: str = ""
    detail: str = ""


_desktop_ready_state: dict = {}


def _require_desktop_secret(secret: Optional[str]) -> None:
    """Constant-time check against the secret the parent inherited.

    A server started without one (dev, tests) has nothing to compare
    against, so it refuses every publish rather than accepting any — the
    safe direction, and it keeps a missing secret from silently turning
    into an open endpoint.
    """
    import os
    import secrets as _secrets

    from app.launcher.server_process import SESSION_SECRET_ENV

    expected = os.environ.get(SESSION_SECRET_ENV, "")
    if not expected or not secret or not _secrets.compare_digest(secret, expected):
        raise HTTPException(status_code=403, detail="Not permitted.")


@router.get("/desktop/ready", response_model=DesktopReadyResponse)
def desktop_ready() -> DesktopReadyResponse:
    """Whether the desktop application is up and controllable.

    Distinct from /health, which answers for this server process alone
    and says nothing about the window or the tray.
    """
    from app.launcher.desktop_ready import DesktopReadyState

    state = DesktopReadyState(**_desktop_ready_state) if _desktop_ready_state else DesktopReadyState()
    payload = state.to_payload()
    payload["missing"] = state.missing()
    return DesktopReadyResponse(**payload)


@router.post("/desktop/ready", response_model=DesktopReadyResponse)
def publish_desktop_ready(
    req: PublishDesktopReadyRequest,
    x_jarvis_desktop_secret: Optional[str] = Header(default=None),
) -> DesktopReadyResponse:
    _require_desktop_secret(x_jarvis_desktop_secret)
    _desktop_ready_state.clear()
    _desktop_ready_state.update(req.model_dump())
    return desktop_ready()


# ---------------------------------------------------------------------------
# What JARVIS calls you, and what closing the window does.
#
# These are the two settings first-run actually needs (well, one of them
# — the other is here because its control existed on the setup screen and
# was wired to nothing at all). Both are ordinary preferences, never
# credentials, so they live in the preferences allowlist rather than the
# credential store.
# ---------------------------------------------------------------------------

class PreferredNameResponse(BaseModel):
    name: str


class SetPreferredNameRequest(BaseModel):
    name: str = ""


class CloseActionResponse(BaseModel):
    close_action: str
    detail: str


class SetCloseActionRequest(BaseModel):
    close_action: str


@router.get("/settings/preferred-name", response_model=PreferredNameResponse)
def preferred_name() -> PreferredNameResponse:
    from app.core.preferences import get as get_preference
    return PreferredNameResponse(name=get_preference("preferred_name") or "")


@router.post(
    "/settings/preferred-name",
    response_model=PreferredNameResponse,
    dependencies=[Depends(require_session_token)],
)
def set_preferred_name(req: SetPreferredNameRequest) -> PreferredNameResponse:
    """Store what JARVIS should call the user. Blank clears it.

    Sanitised here rather than only where it is used: this value ends up
    inside a system prompt, so control characters and quotes are stripped
    at the boundary that persists it, and again at the boundary that
    builds the prompt (app/core/system_prompt.py). Neither trusts the
    other to have done it.
    """
    from app.core.preferences import MAX_PREFERRED_NAME_LENGTH, store
    from app.core.system_prompt import _sanitise_name

    cleaned = _sanitise_name(req.name)[:MAX_PREFERRED_NAME_LENGTH]
    store("preferred_name", cleaned)
    return PreferredNameResponse(name=cleaned)


def _close_action_response(value: str) -> CloseActionResponse:
    return CloseActionResponse(
        close_action=value,
        detail=(
            "Closing the window keeps JARVIS running in the tray."
            if value == "tray" else
            "Closing the window quits JARVIS completely."
        ),
    )


@router.get("/settings/close-action", response_model=CloseActionResponse)
def close_action() -> CloseActionResponse:
    from app.launcher.gui import close_action as effective_close_action
    from app.launcher.webview_window import resolve_close_action

    return _close_action_response(resolve_close_action(effective_close_action()))


@router.post(
    "/settings/close-action",
    response_model=CloseActionResponse,
    dependencies=[Depends(require_session_token)],
)
def set_close_action(req: SetCloseActionRequest) -> CloseActionResponse:
    from app.core.preferences import store
    from app.launcher.webview_window import VALID_CLOSE_ACTIONS

    requested = (req.close_action or "").strip().lower()
    if requested not in VALID_CLOSE_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"close_action must be one of: {', '.join(VALID_CLOSE_ACTIONS)}.",
        )
    store("close_action", requested)
    # Takes effect the next time a window is created — say so rather than
    # implying the running window's behaviour just changed.
    logger.info("Window close action set to %s.", requested)
    return _close_action_response(requested)


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


# ---------------------------------------------------------------------------
# AI provider discovery (onboarding wizard + Settings). Read-only: this
# only reports what was actually detected — see app/core/providers.py,
# which never claims a provider is available without checking, and never
# returns a credential.
# ---------------------------------------------------------------------------

class ProviderStatusResponse(BaseModel):
    name: str
    display_name: str
    kind: str
    available: bool
    detail: str
    models: List[str] = []
    requires_credentials: bool = False


class ProvidersResponse(BaseModel):
    selected: str
    providers: List[ProviderStatusResponse]
    # The model in effect for the selected provider. Never a credential —
    # a model name is not sensitive; the key never appears here.
    selected_model: str = ""


class SelectProviderRequest(BaseModel):
    provider: str
    # Only meaningful for a local provider; ignored otherwise. Empty
    # means "whichever model the local instance reports first".
    model: str = ""


@router.get("/providers", response_model=ProvidersResponse)
def list_providers() -> ProvidersResponse:
    from app.core.providers import detect_all, selected_provider

    return ProvidersResponse(
        selected=selected_provider(),
        providers=[ProviderStatusResponse(**vars(status)) for status in detect_all()],
        selected_model=_selected_model_for_ui(),
    )


def _selected_model_for_ui() -> str:
    """What the Settings page shows as the current model. For Ollama this
    is a name the user picked (or "" for auto); for Anthropic it is the
    configured model, which is not user-selectable here."""
    from app.config import settings
    from app.core.providers import PROVIDER_OLLAMA, selected_ollama_model, selected_provider

    if selected_provider() == PROVIDER_OLLAMA:
        return selected_ollama_model()
    return settings.jarvis_ai_model or ""


@router.post(
    "/providers/select",
    response_model=ProvidersResponse,
    dependencies=[Depends(require_session_token)],
)
def select_provider(req: SelectProviderRequest) -> ProvidersResponse:
    """Choose which provider answers chat.

    Refuses a provider that is not actually available right now, and an
    Ollama model the local instance does not report — the same rule the
    rest of this app follows: never claim a capability that was not
    detected. The refusal names what *is* available so it is actionable.
    """
    from app.core.preferences import store
    from app.core.providers import PROVIDER_OLLAMA, detect_all, is_valid_provider

    requested = (req.provider or "").strip().lower()
    if not is_valid_provider(requested):
        raise HTTPException(status_code=400, detail="Unknown AI provider.")

    statuses = {status.name: status for status in detect_all()}
    chosen = statuses[requested]
    if not chosen.available:
        raise HTTPException(status_code=409, detail=chosen.detail)

    model = (req.model or "").strip()
    if requested == PROVIDER_OLLAMA and model and model not in chosen.models:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{model}' is not installed in Ollama. Installed models: "
                f"{', '.join(chosen.models) or 'none'}."
            ),
        )

    store("ai_provider", requested)
    if requested == PROVIDER_OLLAMA:
        store("ollama_model", model)

    logger.info("AI provider selected: %s", requested)
    return list_providers()


# ---------------------------------------------------------------------------
# "Start JARVIS when I sign in" — a real per-user Startup shortcut, not a
# stored preference. State is read from disk so it can never drift out of
# sync with reality; see app/launcher/startup_shortcut.py.
# ---------------------------------------------------------------------------

class StartupStatusResponse(BaseModel):
    supported: bool
    enabled: bool
    detail: str


class SetStartupRequest(BaseModel):
    enabled: bool


def _startup_status() -> StartupStatusResponse:
    from app.launcher import startup_shortcut

    supported = startup_shortcut.is_supported()
    enabled = startup_shortcut.is_enabled()
    return StartupStatusResponse(
        supported=supported,
        enabled=enabled,
        detail=(
            ("JARVIS starts automatically when you sign in." if enabled
             else "JARVIS does not start automatically.")
            if supported else
            "Starting with Windows is only available on Windows."
        ),
    )


@router.get("/settings/startup", response_model=StartupStatusResponse)
def startup_status() -> StartupStatusResponse:
    return _startup_status()


@router.post(
    "/settings/startup",
    response_model=StartupStatusResponse,
    dependencies=[Depends(require_session_token)],
)
def set_startup(req: SetStartupRequest) -> StartupStatusResponse:
    from app.launcher import startup_shortcut

    if req.enabled:
        startup_shortcut.enable()
    else:
        startup_shortcut.disable()
    # Report the real resulting state, not the requested one: if creating
    # the shortcut failed, the response must say so rather than echo back
    # a success the filesystem does not support.
    return _startup_status()


# ---------------------------------------------------------------------------
# Diagnostics (About page). Built from an explicit allowlist and passed
# through the same redactor used for child-process output — see
# app/core/diagnostics.py for why an allowlist rather than a denylist.
# ---------------------------------------------------------------------------

class DiagnosticItemResponse(BaseModel):
    label: str
    value: str


class DiagnosticSectionResponse(BaseModel):
    title: str
    items: List[DiagnosticItemResponse]


class DiagnosticsResponse(BaseModel):
    sections: List[DiagnosticSectionResponse]
    text: str


# ---------------------------------------------------------------------------
# About: version and the bundled open-source notices.
#
# There is deliberately no automatic update check. JARVIS makes no
# network request of its own accord — the only outbound traffic in the
# whole application is a chat message to a provider the user configured.
# A background version poll would break that, and no amount of care with
# the request would put the traffic back.
#
# (This used to add that a poll could not work anyway, the repository
# being private. It is public now, so that half is simply wrong — and it
# was never the reason. The reason is the sentence above.)
#
# "Check for updates" therefore opens the releases page in the user's
# browser and lets them compare versions themselves, which is honest
# about what it does rather than implying a check happened.
# ---------------------------------------------------------------------------

class AboutResponse(BaseModel):
    version: str
    build: str
    packaged: bool
    releases_url: str


class NoticesResponse(BaseModel):
    available: bool
    text: str


PROJECT_RELEASES_URL = "https://github.com/Dado211207/jarvis-windows-ai-assistant/releases"


@router.get("/about", response_model=AboutResponse)
def about() -> AboutResponse:
    import sys

    return AboutResponse(
        version=__version__,
        build=__phase__,
        packaged=bool(getattr(sys, "frozen", False)),
        releases_url=PROJECT_RELEASES_URL,
    )


def _notices_path():
    """Where THIRD_PARTY_NOTICES.md lives in each mode.

    PyInstaller unpacks bundled data next to the executable's temporary
    root (sys._MEIPASS); in a source checkout the file is in docs/. Both
    are checked rather than assuming one, so the About page is not blank
    in whichever mode the developer happens not to be testing.
    """
    import sys
    from pathlib import Path

    candidates = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "THIRD_PARTY_NOTICES.md")
        candidates.append(Path(sys.executable).parent / "THIRD_PARTY_NOTICES.md")
    candidates.append(Path(__file__).resolve().parent.parent.parent / "docs" / "THIRD_PARTY_NOTICES.md")

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


@router.get("/about/notices", response_model=NoticesResponse)
def about_notices() -> NoticesResponse:
    """The bundled third-party notices, as plain text.

    Served rather than linked because the packaged app has no file
    manager and no browser pointed at its install directory — a licence
    file that ships with the product and cannot be read from it is not
    really shipped.
    """
    path = _notices_path()
    if path is None:
        return NoticesResponse(available=False, text="")
    try:
        return NoticesResponse(available=True, text=path.read_text(encoding="utf-8"))
    except OSError:
        logger.warning("Third-party notices could not be read.", exc_info=True)
        return NoticesResponse(available=False, text="")


@router.get("/diagnostics", response_model=DiagnosticsResponse, dependencies=[Depends(require_session_token)])
def diagnostics() -> DiagnosticsResponse:
    from app.core.diagnostics import build_report, render_report_text

    sections = build_report()
    return DiagnosticsResponse(
        sections=[
            DiagnosticSectionResponse(
                title=section.title,
                items=[DiagnosticItemResponse(**item) for item in section.items],
            )
            for section in sections
        ],
        text=render_report_text(sections),
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
