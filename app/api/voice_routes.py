"""The neural voice's own endpoints: install it, choose it, test it,
and teach it how to say a name.

Separate from app/api/routes.py because that file is already long and
because these belong together: everything here is about the voice
JARVIS speaks with, and all of it reads its facts from
app/voice/status.py so the Voice page, the diagnostics panel and the
`speak status` command cannot disagree.

Every mutating route is session-token protected, the same as every other
mutation in this application. Nothing here downloads anything without an
explicit request naming what it is about to fetch.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.session import require_session_token
from app.logging_config import get_logger
from app.voice import engines, status as voice_status
from app.voice.kokoro import assets, install

logger = get_logger("api.voice")

router = APIRouter(tags=["voice"])


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class EngineTier(BaseModel):
    key: str
    name: str
    available: bool
    detail: str
    tier: int
    active: bool


class VoiceOption(BaseModel):
    key: str
    display_name: str
    description: str
    installed: bool
    download_bytes: int


class VoiceEngineStatusResponse(BaseModel):
    speaks_replies: bool
    available: bool
    active_engine: str
    active_engine_name: str
    engines: List[EngineTier]
    voice_key: str
    voice_name: str
    voices: List[VoiceOption]
    speed: float
    model_installed: bool
    download_bytes_required: int
    install_dir: str


@router.get("/voice/engine-status", response_model=VoiceEngineStatusResponse)
def voice_engine_status() -> VoiceEngineStatusResponse:
    """What is installed, what is speaking, and what each tier reported."""
    return VoiceEngineStatusResponse(**voice_status.snapshot())


# ---------------------------------------------------------------------------
# Choosing a voice
# ---------------------------------------------------------------------------

class SelectVoiceRequest(BaseModel):
    voice_key: str


class SetSpeedRequest(BaseModel):
    speed: float


@router.post(
    "/voice/select",
    response_model=VoiceEngineStatusResponse,
    dependencies=[Depends(require_session_token)],
)
def select_voice(req: SelectVoiceRequest) -> VoiceEngineStatusResponse:
    """Choose which voice JARVIS speaks with.

    A key that is not one of ours is refused rather than saved and
    silently ignored later — see engines.set_selected_voice(). The loaded
    style pack is dropped so the next reply uses the new voice rather
    than the one already in memory.
    """
    if engines.set_selected_voice(req.voice_key) is None:
        logger.info("Refused an unknown voice key.")
    else:
        engines.kokoro_engine.engine.unload()
    return voice_engine_status()


@router.post(
    "/voice/speed",
    response_model=VoiceEngineStatusResponse,
    dependencies=[Depends(require_session_token)],
)
def set_speed(req: SetSpeedRequest) -> VoiceEngineStatusResponse:
    engines.set_selected_speed(req.speed)
    return voice_engine_status()


# ---------------------------------------------------------------------------
# Installing a voice
# ---------------------------------------------------------------------------

class VoiceInstallPreview(BaseModel):
    voice_key: str
    voice_name: str
    already_installed: bool
    download_bytes: int
    files: List[str]
    licence: str
    source: str
    destination: str


class VoiceInstallStatusResponse(BaseModel):
    status: str
    current_file: str
    bytes_downloaded: int
    bytes_total: int
    percent: int
    message: str
    voice_key: str
    running: bool


class InstallVoiceRequest(BaseModel):
    voice_key: str = Field(default=assets.DEFAULT_VOICE_KEY)


@router.get("/voice/install-preview", response_model=VoiceInstallPreview)
def install_preview(voice_key: str = assets.DEFAULT_VOICE_KEY) -> VoiceInstallPreview:
    """Exactly what would be downloaded, before anything is.

    Shown to the user first, deliberately: nothing about the voice is
    fetched without the size, the source and the licence being on
    screen.
    """
    voice = assets.resolve_voice(voice_key)
    missing = install.missing_assets(voice.key)
    return VoiceInstallPreview(
        voice_key=voice.key,
        voice_name=voice.display_name,
        already_installed=not missing,
        download_bytes=sum(asset.size_bytes for asset in missing),
        files=[asset.filename for asset in missing],
        licence=assets.MODEL_LICENCE,
        source=assets.MODEL_SOURCE_URL,
        destination=str(install.install_dir()),
    )


@router.get("/voice/install-status", response_model=VoiceInstallStatusResponse)
def install_status() -> VoiceInstallStatusResponse:
    state = install.voice_installer.state()
    return VoiceInstallStatusResponse(
        status=state.status,
        current_file=state.current_file,
        bytes_downloaded=state.bytes_downloaded,
        bytes_total=state.bytes_total,
        percent=state.percent,
        message=state.message,
        voice_key=state.voice_key,
        running=install.voice_installer.is_running(),
    )


@router.post(
    "/voice/install",
    response_model=VoiceInstallStatusResponse,
    dependencies=[Depends(require_session_token)],
)
def start_install(req: InstallVoiceRequest) -> VoiceInstallStatusResponse:
    if not install.voice_installer.start(req.voice_key):
        logger.info("An install is already running; the second request was ignored.")
    return install_status()


@router.post(
    "/voice/install/cancel",
    response_model=VoiceInstallStatusResponse,
    dependencies=[Depends(require_session_token)],
)
def cancel_install() -> VoiceInstallStatusResponse:
    install.voice_installer.cancel()
    return install_status()


# ---------------------------------------------------------------------------
# Trying it out
# ---------------------------------------------------------------------------

class TestVoiceRequest(BaseModel):
    voice_key: Optional[str] = None
    text: Optional[str] = None


class TestVoiceResponse(BaseModel):
    success: bool
    message: str
    engine: str


# What a test says. Long enough to hear the voice's rhythm rather than a
# single word, and it names the product rather than reading a stock
# sentence about the weather.
TEST_PHRASE = "Good evening. This is the voice JARVIS will use when it speaks to you."


@router.post(
    "/voice/test",
    response_model=TestVoiceResponse,
    dependencies=[Depends(require_session_token)],
)
def test_voice(req: TestVoiceRequest) -> TestVoiceResponse:
    """Speak a sample.

    Deliberately not gated on "speak replies": this is somebody pressing
    a button labelled Test Voice, which is a direct request to hear it,
    not JARVIS deciding to talk. The gate on `/voice/speak` is what stops
    a stale page making JARVIS speak its replies.
    """
    voice_key = req.voice_key or engines.selected_voice_key()
    text = (req.text or TEST_PHRASE)[:300]

    outcome = engines.speak(text, voice_key=voice_key, speed=engines.selected_speed())
    return TestVoiceResponse(
        success=outcome.started, message=outcome.message, engine=outcome.engine,
    )


# ---------------------------------------------------------------------------
# Custom pronunciations
# ---------------------------------------------------------------------------

class PronunciationEntry(BaseModel):
    word: str
    input: str
    phonemes: str


class PronunciationsResponse(BaseModel):
    entries: List[PronunciationEntry]
    preferred_name: str
    name_needs_pronunciation: bool


class SavePronunciationRequest(BaseModel):
    word: str
    spoken_as: str


class PronunciationResultResponse(BaseModel):
    success: bool
    message: str
    phonemes: str = ""


def _pronunciations_response() -> PronunciationsResponse:
    from app.core.preferences import get
    from app.voice import pronunciations

    name = (get("preferred_name") or "").strip()
    return PronunciationsResponse(
        entries=[
            PronunciationEntry(word=entry.word, input=entry.input, phonemes=entry.phonemes)
            for entry in pronunciations.entries()
        ],
        preferred_name=name,
        name_needs_pronunciation=pronunciations.name_needs_pronunciation(name),
    )


@router.get("/voice/pronunciations", response_model=PronunciationsResponse)
def list_pronunciations() -> PronunciationsResponse:
    """Saved pronunciations, plus whether the name from first run is one
    JARVIS would otherwise spell out."""
    return _pronunciations_response()


@router.post(
    "/voice/pronunciations/preview",
    response_model=PronunciationResultResponse,
    dependencies=[Depends(require_session_token)],
)
def preview_pronunciation(req: SavePronunciationRequest) -> PronunciationResultResponse:
    """Resolve an entry without saving it, so it can be heard first."""
    from app.voice import pronunciations

    accepted, phonemes, message = pronunciations.resolve(req.word, req.spoken_as)
    return PronunciationResultResponse(
        success=accepted,
        message=message or "Ready to hear.",
        phonemes=phonemes,
    )


@router.post(
    "/voice/pronunciations",
    response_model=PronunciationsResponse,
    dependencies=[Depends(require_session_token)],
)
def save_pronunciation(req: SavePronunciationRequest) -> PronunciationsResponse:
    from app.voice import pronunciations

    saved, message = pronunciations.save_entry(req.word, req.spoken_as)
    if not saved:
        logger.info("A pronunciation was refused: %s", message)
    return _pronunciations_response()


@router.post(
    "/voice/pronunciations/remove",
    response_model=PronunciationsResponse,
    dependencies=[Depends(require_session_token)],
)
def remove_pronunciation(req: SavePronunciationRequest) -> PronunciationsResponse:
    from app.voice import pronunciations

    pronunciations.remove_entry(req.word)
    return _pronunciations_response()


# ---------------------------------------------------------------------------
# The licence record, served rather than hard-coded in a template
# ---------------------------------------------------------------------------

class LicenceComponent(BaseModel):
    component: str
    role: str
    licence: str
    source: str
    distributed: str
    acknowledgement: str = ""


@router.get("/voice/licences", response_model=List[LicenceComponent])
def voice_licences() -> List[LicenceComponent]:
    """What the voice is made of and under what terms.

    Served from the same manifest the code uses, so the page cannot
    drift away from what actually ships.
    """
    return [LicenceComponent(**entry) for entry in assets.LICENCE_MANIFEST]
