"""The neural voice's own endpoints: install it, choose it, test it,
and teach it how to say a name.

Separate from app/api/routes.py because that file is already long and
because these belong together: everything here is about the voice
JARVIS speaks with, and all of it reads its facts from
app/voice/status.py so the Voice page, the diagnostics panel and the
`speak status` command cannot disagree.

Every route in this integration is session-token protected at the router
boundary. Several reads expose user-selected voices, pronunciations and
provider configuration; keeping the dependency on the router also protects
future provider-specific GETs by default. Nothing here downloads anything
without an explicit request naming what it is about to fetch.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.session import require_session_token
from app.logging_config import get_logger
from app.voice import engines, status as voice_status
from app.voice.samples import AB_TEST_PHRASE
from app.voice.kokoro import assets, install

logger = get_logger("api.voice")

router = APIRouter(tags=["voice"], dependencies=[Depends(require_session_token)])


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
TEST_PHRASE = AB_TEST_PHRASE


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


# ---------------------------------------------------------------------------
# The optional cloud voice (ElevenLabs).
#
# Every mutating endpoint here carries require_session_token, like every
# other mutating endpoint in the application. The key is write-only from
# the browser's point of view: it can be saved, replaced, deleted and
# validated, and there is no endpoint that returns it. What comes back is
# always a boolean.
# ---------------------------------------------------------------------------

class CloudVoiceStatusResponse(BaseModel):
    selected: bool
    key_configured: bool
    voice_id: str
    voice_name: str
    settings: dict
    defaults: dict
    ranges: dict
    fallback_allowed: bool
    blocked_by_privacy: bool
    detail: str
    last_fallback: str
    test_phrase: str
    max_text_chars: int


class SaveCloudKeyRequest(BaseModel):
    # No max_length trap: an over-long value is refused below with a
    # sentence rather than a 422 nobody can act on.
    api_key: str = Field(..., min_length=1)


class SelectEngineRequest(BaseModel):
    engine: str = Field("", max_length=32)


class SelectCloudVoiceRequest(BaseModel):
    voice_id: str = Field(..., min_length=1, max_length=128)
    voice_name: str = Field("", max_length=80)


class CloudSettingsRequest(BaseModel):
    settings: dict = Field(default_factory=dict)


class CloudFallbackRequest(BaseModel):
    allowed: bool


class CloudActionResponse(BaseModel):
    success: bool
    message: str
    status: CloudVoiceStatusResponse


class CloudVoiceListResponse(BaseModel):
    success: bool
    message: str
    voices: List[dict]


def _cloud_response(success: bool, message: str) -> CloudActionResponse:
    return CloudActionResponse(
        success=success, message=message,
        status=CloudVoiceStatusResponse(**engines.cloud_status()),
    )


@router.get("/voice/cloud", response_model=CloudVoiceStatusResponse)
def cloud_status() -> CloudVoiceStatusResponse:
    return CloudVoiceStatusResponse(**engines.cloud_status())


@router.post(
    "/voice/cloud/key",
    response_model=CloudActionResponse,
    dependencies=[Depends(require_session_token)],
)
def save_cloud_key(req: SaveCloudKeyRequest) -> CloudActionResponse:
    """Save the ElevenLabs key into the Windows Credential Manager.

    Saving does not validate: a network call the user did not ask for is
    a network call they did not consent to, and a key saved while the
    machine is offline is still the key they meant to save. Validation is
    its own button.
    """
    from app.core.credentials import set_elevenlabs_key

    key = req.api_key.strip()
    if len(key) > 512:
        return _cloud_response(False, "That does not look like an API key — it is too long.")
    if not set_elevenlabs_key(key):
        return _cloud_response(
            False,
            "The Windows Credential Manager could not be written to, so the key was not "
            "saved. It has not been stored anywhere else.",
        )
    return _cloud_response(True, "Key saved to the Windows Credential Manager.")


@router.post(
    "/voice/cloud/key/delete",
    response_model=CloudActionResponse,
    dependencies=[Depends(require_session_token)],
)
def delete_cloud_key() -> CloudActionResponse:
    from app.core.credentials import clear_elevenlabs_key

    if not clear_elevenlabs_key():
        return _cloud_response(False, "The Windows Credential Manager could not be reached.")
    return _cloud_response(True, "Key removed.")


@router.post(
    "/voice/cloud/validate",
    response_model=CloudActionResponse,
    dependencies=[Depends(require_session_token)],
)
def validate_cloud_key() -> CloudActionResponse:
    """Check the saved key against ElevenLabs, only when asked.

    Uses the subscription endpoint — the cheapest authenticated call
    there is. It generates no audio and spends no credits.
    """
    from app.core.credentials import get_elevenlabs_key
    from app.core.privacy import privacy_mode
    from app.voice import elevenlabs

    if privacy_mode.active:
        return _cloud_response(
            False, "Privacy mode is on, so JARVIS did not contact ElevenLabs.",
        )
    key = get_elevenlabs_key()
    if not key:
        return _cloud_response(False, elevenlabs._MESSAGES[elevenlabs.NOT_CONFIGURED])
    ok, message = elevenlabs.validate_key(key)
    return _cloud_response(ok, message)


@router.post(
    "/voice/cloud/voices",
    response_model=CloudVoiceListResponse,
    dependencies=[Depends(require_session_token)],
)
def refresh_cloud_voices() -> CloudVoiceListResponse:
    """The voices this account can use. A POST because it costs a network
    round trip and must not happen because a page was opened."""
    from app.core.credentials import get_elevenlabs_key
    from app.core.privacy import privacy_mode
    from app.voice import elevenlabs

    if privacy_mode.active:
        return CloudVoiceListResponse(
            success=False,
            message="Privacy mode is on, so JARVIS did not contact ElevenLabs.",
            voices=[],
        )
    key = get_elevenlabs_key()
    if not key:
        return CloudVoiceListResponse(
            success=False, message=elevenlabs._MESSAGES[elevenlabs.NOT_CONFIGURED], voices=[],
        )
    try:
        voices = elevenlabs.list_voices(key)
    except elevenlabs.ElevenLabsError as exc:
        return CloudVoiceListResponse(success=False, message=exc.message, voices=[])
    return CloudVoiceListResponse(
        success=True,
        message=f"{len(voices)} voice(s) available to this account.",
        voices=[voice.as_dict() for voice in voices],
    )


@router.post(
    "/voice/cloud/select-voice",
    response_model=CloudActionResponse,
    dependencies=[Depends(require_session_token)],
)
def select_cloud_voice(req: SelectCloudVoiceRequest) -> CloudActionResponse:
    engines.set_selected_cloud_voice(req.voice_id, req.voice_name)
    name = engines.selected_cloud_voice_name() or engines.selected_cloud_voice_id()
    return _cloud_response(True, f"Cloud voice set to {name}.")


@router.post(
    "/voice/engine",
    response_model=CloudActionResponse,
    dependencies=[Depends(require_session_token)],
)
def select_engine(req: SelectEngineRequest) -> CloudActionResponse:
    """Choose either cloud provider, or go back to the local chain."""
    chosen = engines.set_selected_engine(req.engine)
    if chosen == engines.OPENAI:
        return _cloud_response(True, "JARVIS will use OpenAI Speech for opted-in voice output.")
    if chosen == engines.ELEVENLABS:
        return _cloud_response(True, "JARVIS will speak with the ElevenLabs cloud voice.")
    return _cloud_response(True, "JARVIS will speak with the best local voice.")


@router.post(
    "/voice/cloud/settings",
    response_model=CloudActionResponse,
    dependencies=[Depends(require_session_token)],
)
def save_cloud_settings(req: CloudSettingsRequest) -> CloudActionResponse:
    engines.set_cloud_settings(req.settings)
    return _cloud_response(True, "Voice settings saved.")


@router.post(
    "/voice/cloud/settings/reset",
    response_model=CloudActionResponse,
    dependencies=[Depends(require_session_token)],
)
def reset_cloud_settings() -> CloudActionResponse:
    from app.voice import elevenlabs

    engines.set_cloud_settings(dict(elevenlabs.DEFAULT_SETTINGS))
    return _cloud_response(True, "Voice settings reset to the recommended values.")


@router.post(
    "/voice/cloud/fallback",
    response_model=CloudActionResponse,
    dependencies=[Depends(require_session_token)],
)
def set_cloud_fallback(req: CloudFallbackRequest) -> CloudActionResponse:
    allowed = engines.set_fallback_allowed(req.allowed)
    return _cloud_response(
        True,
        "The local voice will speak if the cloud voice cannot, and will say why."
        if allowed
        else "JARVIS will stay silent and report the error if the cloud voice cannot speak.",
    )


@router.post(
    "/voice/cloud/test",
    response_model=CloudActionResponse,
    dependencies=[Depends(require_session_token)],
)
def test_cloud_voice() -> CloudActionResponse:
    """Speak the test phrase through ElevenLabs, and only ElevenLabs.

    Deliberately does not fall back: this button answers "does the cloud
    voice work", and a local voice answering it would be the wrong answer
    to the question that was asked.
    """
    from app.core.credentials import get_elevenlabs_key
    from app.core.privacy import privacy_mode
    from app.voice import audio, elevenlabs

    if privacy_mode.active:
        return _cloud_response(
            False, "Privacy mode is on, so no text was sent to ElevenLabs.",
        )
    key = get_elevenlabs_key()
    if not key:
        return _cloud_response(False, elevenlabs._MESSAGES[elevenlabs.NOT_CONFIGURED])
    voice_id = engines.selected_cloud_voice_id()
    if not voice_id:
        return _cloud_response(False, "Choose a voice first.")

    try:
        wav = elevenlabs.synthesise_wav(
            elevenlabs.TEST_PHRASE, voice_id=voice_id, api_key=key,
            settings=engines.cloud_settings(),
        )
    except elevenlabs.ElevenLabsError as exc:
        return _cloud_response(False, exc.message)

    engines.stop()  # one utterance at a time, like every other speech path
    audio.player.play_wav_bytes(wav)
    return _cloud_response(True, f"Speaking: “{elevenlabs.TEST_PHRASE}”")


# ---------------------------------------------------------------------------
# OpenAI Speech — a separate, explicitly selected cloud provider.
# ---------------------------------------------------------------------------

class OpenAIVoiceStatusResponse(BaseModel):
    selected: bool
    key_configured: bool
    model: str
    models: List[str]
    voice: str
    voices: List[str]
    speed: float
    speed_range: List[float]
    instructions: str
    default_instructions: str
    max_instruction_chars: int
    fallback_allowed: bool
    blocked_by_privacy: bool
    detail: str
    last_fallback: str
    test_phrase: str
    ai_generated: bool
    internet_required: bool
    usage_may_incur_cost: bool


class OpenAIActionResponse(BaseModel):
    success: bool
    message: str
    status: OpenAIVoiceStatusResponse


class OpenAISettingsRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=64)
    voice: str = Field(..., min_length=1, max_length=32)
    speed: float
    instructions: str = Field(default="", max_length=4096)


def _openai_response(success: bool, message: str) -> OpenAIActionResponse:
    return OpenAIActionResponse(
        success=success,
        message=message,
        status=OpenAIVoiceStatusResponse(**engines.openai_status()),
    )


@router.get("/voice/openai", response_model=OpenAIVoiceStatusResponse)
def openai_voice_status() -> OpenAIVoiceStatusResponse:
    # Deliberately does not load the credential or contact OpenAI.
    return OpenAIVoiceStatusResponse(**engines.openai_status())


@router.post(
    "/voice/openai/key",
    response_model=OpenAIActionResponse,
    dependencies=[Depends(require_session_token)],
)
def save_openai_key(req: SaveCloudKeyRequest) -> OpenAIActionResponse:
    from app.core.credentials import set_openai_key
    from app.voice import openai_tts

    key = req.api_key.strip()
    if not key or len(key) > openai_tts.MAX_API_KEY_CHARS:
        return _openai_response(False, "That does not look like a valid API key length.")
    if not set_openai_key(key):
        return _openai_response(
            False,
            "The Windows Credential Manager could not be written to. The key was not stored elsewhere.",
        )
    engines.set_openai_key_configured(True)
    return _openai_response(True, "OpenAI voice key saved to the Windows Credential Manager.")


@router.post(
    "/voice/openai/key/delete",
    response_model=OpenAIActionResponse,
    dependencies=[Depends(require_session_token)],
)
def delete_openai_key() -> OpenAIActionResponse:
    from app.core.credentials import clear_openai_key

    if not clear_openai_key():
        return _openai_response(False, "The Windows Credential Manager could not be reached.")
    engines.set_openai_key_configured(False)
    return _openai_response(True, "OpenAI voice key removed.")


@router.post(
    "/voice/openai/validate",
    response_model=OpenAIActionResponse,
    dependencies=[Depends(require_session_token)],
)
def validate_openai_key() -> OpenAIActionResponse:
    from app.core.credentials import get_openai_key
    from app.core.privacy import privacy_mode
    from app.voice import openai_tts

    if privacy_mode.active:
        return _openai_response(False, "Privacy mode is on, so JARVIS made no OpenAI request.")
    key = get_openai_key()
    if not key:
        return _openai_response(False, openai_tts._MESSAGES[openai_tts.NOT_CONFIGURED])
    ok, message = openai_tts.validate_key(key)
    return _openai_response(ok, message)


@router.post(
    "/voice/openai/settings",
    response_model=OpenAIActionResponse,
    dependencies=[Depends(require_session_token)],
)
def save_openai_settings(req: OpenAISettingsRequest) -> OpenAIActionResponse:
    try:
        engines.set_openai_settings(req.model, req.voice, req.speed, req.instructions)
    except ValueError as exc:
        return _openai_response(False, str(exc))
    return _openai_response(True, "OpenAI voice settings saved.")


@router.post(
    "/voice/openai/settings/reset",
    response_model=OpenAIActionResponse,
    dependencies=[Depends(require_session_token)],
)
def reset_openai_settings() -> OpenAIActionResponse:
    from app.voice import openai_tts

    engines.set_openai_settings(
        openai_tts.DEFAULT_MODEL,
        openai_tts.DEFAULT_VOICE,
        openai_tts.DEFAULT_SPEED,
        openai_tts.DEFAULT_INSTRUCTIONS,
    )
    return _openai_response(True, "OpenAI voice settings reset to the original profile.")


@router.post(
    "/voice/openai/fallback",
    response_model=OpenAIActionResponse,
    dependencies=[Depends(require_session_token)],
)
def set_openai_fallback(req: CloudFallbackRequest) -> OpenAIActionResponse:
    allowed = engines.set_openai_fallback_allowed(req.allowed)
    return _openai_response(
        True,
        (
            "The best available local voice will be used and disclosed if OpenAI Speech cannot speak."
            if allowed
            else "JARVIS will stay silent and report why if OpenAI Speech cannot speak."
        ),
    )


@router.post(
    "/voice/openai/test",
    response_model=OpenAIActionResponse,
    dependencies=[Depends(require_session_token)],
)
def test_openai_voice() -> OpenAIActionResponse:
    """Generate the shared A/B phrase through OpenAI only; never fall back."""
    from app.core.credentials import get_openai_key
    from app.core.privacy import privacy_mode
    from app.voice import audio, openai_tts

    if privacy_mode.active:
        return _openai_response(False, "Privacy mode is on, so no text was sent to OpenAI.")
    key = get_openai_key()
    if not key:
        return _openai_response(False, openai_tts._MESSAGES[openai_tts.NOT_CONFIGURED])

    cancel = audio.player.begin_utterance()
    try:
        wav = openai_tts.synthesise_wav(
            openai_tts.TEST_PHRASE,
            key,
            model=engines.selected_openai_model(),
            voice=engines.selected_openai_voice(),
            speed=engines.selected_openai_speed(),
            instructions=engines.selected_openai_instructions(),
            cancel=cancel,
        )
    except openai_tts.OpenAITTSError as exc:
        return _openai_response(False, exc.message)
    if not audio.player.play_wav_bytes_if_current(wav, cancel):
        return _openai_response(False, openai_tts._MESSAGES[openai_tts.CANCELLED])
    return _openai_response(True, f"Speaking: “{openai_tts.TEST_PHRASE}”")


# ---------------------------------------------------------------------------
# Double-clap activation
#
# Four settings and one activation report. The activation endpoint takes
# no request body at all — read app/voice/clap.py for why that is a
# design constraint rather than an omission: there is no field a clap
# could carry that would be safe to accept.
# ---------------------------------------------------------------------------

class ClapStatusResponse(BaseModel):
    enabled: bool
    sensitivity: str
    sensitivities: List[str]
    greet: bool
    greeting: str
    detector: dict
    privacy_blocking: bool
    activations: int
    last_reason: str
    seconds_since_activation: Optional[float] = None
    min_interval_seconds: float
    device_id: str = ""
    tuning: dict = Field(default_factory=dict)
    calibrated: bool = False
    safe_bounds: dict = Field(default_factory=dict)
    listener_state: str = "disabled"
    tray_label: str = ""


class ClapEnabledRequest(BaseModel):
    enabled: bool


class ClapSettingsRequest(BaseModel):
    sensitivity: Optional[str] = None
    greet: Optional[bool] = None
    greeting: Optional[str] = Field(default=None, max_length=200)
    # The shared microphone choice. A device id, never a label.
    device_id: Optional[str] = Field(default=None, max_length=200)
    # Calibrated overrides. Clamped to SAFE_BOUNDS server-side whatever
    # arrives, and `{}` clears them back to the chosen profile.
    tuning: Optional[dict] = None


class ClapListenerRequest(BaseModel):
    """What the page says its own listener is doing.

    One field, from a fixed allowlist. Deliberately not a free-text
    status: a report that can carry an arbitrary string is a channel,
    and this endpoint exists so the tray can stop guessing, not so the
    page can say things.
    """

    state: str = Field(..., max_length=32)


class ClapActivateResponse(BaseModel):
    accepted: bool
    reason: str
    window_shown: bool
    greeted: bool
    message: str


_CLAP_REASONS = {
    "activated": "JARVIS was brought to the front.",
    "disabled": "Double-clap activation is switched off.",
    "privacy_mode": "Privacy mode is on, so JARVIS is not listening for claps.",
    "too_soon": "That was too soon after the last activation.",
}


@router.get("/voice/clap", response_model=ClapStatusResponse)
def clap_status() -> ClapStatusResponse:
    """Settings, detector tuning and what happened last.

    Readable without the session token, like every other status read —
    it reports no audio and no content, only whether a feature is on.
    """
    from app.voice import clap

    return ClapStatusResponse(**clap.status())


@router.post(
    "/voice/clap/enabled",
    response_model=ClapStatusResponse,
    dependencies=[Depends(require_session_token)],
)
def set_clap_enabled(req: ClapEnabledRequest) -> ClapStatusResponse:
    from app.voice import clap

    clap.set_enabled(req.enabled)
    return ClapStatusResponse(**clap.status())


@router.post(
    "/voice/clap/settings",
    response_model=ClapStatusResponse,
    dependencies=[Depends(require_session_token)],
)
def set_clap_settings(req: ClapSettingsRequest) -> ClapStatusResponse:
    from app.voice import clap

    if req.sensitivity is not None:
        clap.set_sensitivity(req.sensitivity)
    if req.greet is not None:
        clap.set_greet_enabled(req.greet)
    if req.greeting is not None:
        clap.set_greeting(req.greeting)
    if req.device_id is not None:
        clap.set_device_id(req.device_id)
    if req.tuning is not None:
        clap.set_tuning(req.tuning)
    return ClapStatusResponse(**clap.status())


@router.post(
    "/voice/clap/listener",
    response_model=ClapStatusResponse,
    dependencies=[Depends(require_session_token)],
)
def report_clap_listener(req: ClapListenerRequest) -> ClapStatusResponse:
    """The page reporting what its own listener is actually doing.

    This exists so the tray can tell the truth. A stored preference says
    what somebody wanted; only the page that owns the microphone knows
    whether one is open, and `clap.listener_state()` stops believing this
    report once it goes stale.
    """
    from app.voice import clap

    clap.report_listener_state(req.state)
    return ClapStatusResponse(**clap.status())


@router.post(
    "/voice/clap/activate",
    response_model=ClapActivateResponse,
    dependencies=[Depends(require_session_token)],
)
def clap_activate() -> ClapActivateResponse:
    """A page detected a clap pair.

    Takes no body. The page has nothing to tell the server beyond the
    fact that it happened, and accepting a field here would be the first
    step towards a microphone that can say things.

    Every gate that matters is re-checked on this side — the stored
    setting, privacy mode and the refractory interval — because a page
    left open before the feature was switched off must not still be able
    to act. The session token requirement means an ordinary web page
    cannot reach it at all.
    """
    from app.voice import clap

    outcome = clap.activate()
    return ClapActivateResponse(
        accepted=outcome.accepted,
        reason=outcome.reason,
        window_shown=outcome.window_shown,
        greeted=outcome.greeted,
        message=_CLAP_REASONS.get(outcome.reason, "Nothing happened."),
    )
