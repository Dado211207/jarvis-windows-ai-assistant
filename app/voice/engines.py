"""Which engine speaks, and what to say when none of them can.

Three tiers, in the order the owner set:

  1. **Kokoro** — the neural British voice this release is for. Local,
     offline once installed, no API key and no GPU.
  2. **Windows natural voices** — the modern Windows neural voices, when
     the machine has them.
  3. **SAPI5**, through pyttsx3 — the robotic voice. Kept only so that a
     machine which can run neither of the above is not left silent, and
     never selected while either of them is usable.

The selection is computed, never remembered. A voice that was installed
after startup, a model file deleted by hand, a Windows voice added in
Settings — all of them change the answer, and a cached one would be
wrong in exactly the situation where a user is trying to work out why
their speech stopped working.

**Every tier reports why it is unusable, separately.** "Speech is not
available" is not a diagnosis. Not installed, no runtime, no voice on the
machine and a damaged model file are four different problems with four
different fixes, and the diagnostics panel shows each engine's own
answer.
"""

from dataclasses import dataclass
from typing import List, Optional

from app.logging_config import get_logger
from app.voice import audio, winrt_voices
from app.voice.kokoro import assets, engine as kokoro_engine, install

logger = get_logger("voice.engines")

KOKORO = "kokoro"
WINDOWS = "windows"
SAPI5 = "sapi5"
ELEVENLABS = "elevenlabs"
NONE = "none"

# The automatic local chain. ElevenLabs is deliberately **not** in it.
#
# Everything in this tuple is local, offline and free, so picking the best
# available one is a decision JARVIS can make on somebody's behalf.
# ElevenLabs is none of those things: it sends the reply text to a third
# party and spends money doing it. A tier with those properties may only
# ever be chosen explicitly, so it is selected by name (see
# selected_engine()) and can never be reached by falling through this
# list.
ENGINE_ORDER = (KOKORO, WINDOWS, SAPI5)

AUTO = ""  # "use the best local engine" — the default, and what an unset preference means

DISPLAY_NAMES = {
    KOKORO: "JARVIS neural voice",
    WINDOWS: "Windows natural voice",
    SAPI5: "Windows classic voice",
    ELEVENLABS: "ElevenLabs cloud voice",
    NONE: "No speech engine",
}


@dataclass
class EngineStatus:
    key: str
    display_name: str
    available: bool
    detail: str
    tier: int          # 1 is preferred
    active: bool = False


def _sapi5_available() -> bool:
    try:
        import pyttsx3  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _kokoro_status(voice_key: str) -> EngineStatus:
    reason = kokoro_engine.engine.unavailable_reason(voice_key)
    voice = assets.resolve_voice(voice_key)
    return EngineStatus(
        key=KOKORO,
        display_name=DISPLAY_NAMES[KOKORO],
        available=not reason,
        detail=reason or f"Ready — speaking as {voice.display_name}.",
        tier=1,
    )


def _windows_status() -> EngineStatus:
    available = winrt_voices.is_available()
    return EngineStatus(
        key=WINDOWS,
        display_name=DISPLAY_NAMES[WINDOWS],
        available=available,
        detail=winrt_voices.describe(),
        tier=2,
    )


def _sapi5_status() -> EngineStatus:
    available = _sapi5_available()
    return EngineStatus(
        key=SAPI5,
        display_name=DISPLAY_NAMES[SAPI5],
        available=available,
        detail=(
            "Available. Used only if neither of the voices above can run — it is the "
            "older, robotic Windows voice."
            if available
            else "The classic Windows speech interface is not available in this build."
        ),
        tier=3,
    )


def statuses(voice_key: str = assets.DEFAULT_VOICE_KEY) -> List[EngineStatus]:
    """Every engine and its own reason, best first."""
    result = [_kokoro_status(voice_key), _windows_status(), _sapi5_status()]
    for status in result:
        if status.available:
            status.active = True
            break
    return result


def active_engine(voice_key: str = assets.DEFAULT_VOICE_KEY) -> str:
    for status in statuses(voice_key):
        if status.available:
            return status.key
    return NONE


def unavailable_message(voice_key: str = assets.DEFAULT_VOICE_KEY) -> str:
    """What to tell someone when nothing can speak. Names the fix that
    is actually available rather than the general problem."""
    kokoro = _kokoro_status(voice_key)
    if not kokoro.available and "not installed" in kokoro.detail:
        return (
            f"{kokoro.detail} Install it from the Voice page and JARVIS will speak "
            "with it."
        )
    return (
        "No speech engine is available on this machine, so nothing can be spoken. "
        "The Voice page shows what each one reported."
    )


@dataclass
class SpeakOutcome:
    started: bool
    engine: str
    message: str


def speak(
    text: str,
    voice_key: str = assets.DEFAULT_VOICE_KEY,
    speed: float = 1.0,
) -> SpeakOutcome:
    """Say *text* through the best engine available, starting at once.

    Returns as soon as playback has been handed to the player, not when
    the speech has finished — the caller is answering a request, and a
    request that blocks for the length of a spoken paragraph is a
    request that has timed out.
    """
    if selected_engine() == ELEVENLABS:
        outcome = _speak_elevenlabs(text)
        if outcome is not None:
            return outcome
        # None means "the cloud voice could not be used and falling back
        # was allowed" — carry on into the local chain below, and say so.

    chosen = active_engine(voice_key)

    if chosen == KOKORO:
        return _speak_kokoro(text, voice_key, speed)
    if chosen == WINDOWS:
        return _speak_windows(text)
    if chosen == SAPI5:
        return _speak_sapi5(text)
    return SpeakOutcome(started=False, engine=NONE, message=unavailable_message(voice_key))


def _speak_elevenlabs(text: str) -> Optional["SpeakOutcome"]:
    """Speak through the cloud, or explain why not.

    Returns an outcome when the answer is final, and None when the caller
    should fall through to the local chain — which happens only if the
    user has left fallback switched on. A silent fallback would hide the
    two failures that matter most: an expired key and an exhausted quota
    both sound exactly like "it worked" if the local voice quietly takes
    over.
    """
    from app.core.privacy import privacy_mode
    from app.core.credentials import get_elevenlabs_key
    from app.voice import elevenlabs

    def _refuse_or_fall_through(message: str) -> Optional[SpeakOutcome]:
        if fallback_allowed():
            logger.info("Cloud voice unavailable; falling back to the local voice.")
            _note_fallback(message)
            return None
        return SpeakOutcome(started=False, engine=ELEVENLABS, message=message)

    if privacy_mode.active:
        # Not a failure to fall back from — a rule. Privacy mode exists to
        # stop text leaving the machine, and quietly speaking it with a
        # local voice instead is the correct thing to do, said out loud.
        return _refuse_or_fall_through(
            "Privacy mode is on, so nothing was sent to ElevenLabs. "
            "The local voice is unaffected."
        )

    voice_id = selected_cloud_voice_id()
    if not voice_id:
        return _refuse_or_fall_through(
            "No ElevenLabs voice is selected yet. Choose one on the Voice page."
        )

    key = get_elevenlabs_key()
    if not key:
        return _refuse_or_fall_through(elevenlabs._MESSAGES[elevenlabs.NOT_CONFIGURED])

    try:
        wav = elevenlabs.synthesise_wav(
            text, voice_id=voice_id, api_key=key, settings=cloud_settings(),
        )
    except elevenlabs.ElevenLabsError as exc:
        return _refuse_or_fall_through(exc.message)
    except Exception:  # noqa: BLE001 — a provider never raises past its own boundary
        logger.warning("Cloud speech failed unexpectedly.", exc_info=False)
        return _refuse_or_fall_through(elevenlabs._MESSAGES[elevenlabs.PROVIDER_ERROR])

    _note_fallback("")
    audio.player.play_wav_bytes(wav)
    return SpeakOutcome(started=True, engine=ELEVENLABS, message="Speaking with the cloud voice.")


# The last fallback reason, so the UI can show that it happened rather
# than leaving somebody to wonder why the voice changed. Deliberately
# in-memory and single-slot: it is a transient notice, not a record.
_last_fallback: str = ""


def _note_fallback(reason: str) -> None:
    global _last_fallback
    _last_fallback = reason


def last_fallback_reason() -> str:
    """Why the local voice spoke when the cloud voice was selected, or ""
    if the last utterance went out as configured."""
    return _last_fallback


def _speak_kokoro(text: str, voice_key: str, speed: float) -> SpeakOutcome:
    """Synthesis is lazy and playback consumes it, so the first sentence
    is heard while the rest is still being made."""
    cancel = audio.player.cancel_event()
    try:
        chunks = kokoro_engine.engine.synthesise(
            text, voice_key=voice_key, speed=speed, cancel=cancel,
        )
        audio.player.play_stream(chunks, kokoro_engine.SAMPLE_RATE)
    except kokoro_engine.EngineUnavailable as exc:
        logger.warning("Kokoro became unavailable mid-request: %s", exc)
        return _speak_windows(text) if winrt_voices.is_available() else SpeakOutcome(
            started=False, engine=NONE, message=str(exc),
        )
    except Exception:  # noqa: BLE001
        logger.warning("Neural speech failed.", exc_info=True)
        return SpeakOutcome(
            started=False, engine=KOKORO,
            message="The neural voice could not speak that. The Voice page can test it.",
        )
    return SpeakOutcome(started=True, engine=KOKORO, message="Speaking.")


def _speak_windows(text: str) -> SpeakOutcome:
    wav = winrt_voices.synthesise_wav(text)
    if not wav:
        return _speak_sapi5(text)
    audio.player.play_wav_bytes(wav)
    return SpeakOutcome(started=True, engine=WINDOWS, message="Speaking.")


def _speak_sapi5(text: str) -> SpeakOutcome:
    """The last resort. pyttsx3 owns its own playback, so this is the one
    tier whose sound the player does not hold — stop() knows that."""
    from app.voice.sapi5 import speak_with_sapi5

    if speak_with_sapi5(text):
        return SpeakOutcome(started=True, engine=SAPI5, message="Speaking.")
    return SpeakOutcome(
        started=False, engine=NONE, message=unavailable_message(),
    )


def is_speaking() -> bool:
    """Whether any engine could be making sound right now."""
    from app.voice import sapi5

    return audio.player.is_playing() or sapi5.is_active()


def stop() -> bool:
    """Silence, whichever engine is making the sound.

    Both tiers are always asked, not just the one currently selected: a
    reply could have started on one engine and the selection changed
    underneath it, and a Stop that only reached the current choice would
    leave the other one talking.
    """
    stopped = audio.player.stop()
    try:
        from app.voice.sapi5 import stop_sapi5

        if not stop_sapi5():
            stopped = False
    except Exception:  # noqa: BLE001
        logger.warning("Could not stop the classic engine.", exc_info=True)
        stopped = False
    return stopped


def installed_voices() -> List[dict]:
    """The selectable voices, marked with whether each is downloaded.

    Voices that are not installed are still listed: hiding them would
    make the choice look smaller than it is, and the download is the
    point of the list.
    """
    installed = set(install.installed_voice_keys())
    return [
        {
            "key": voice.key,
            "display_name": voice.display_name,
            "description": voice.description,
            "installed": voice.key in installed,
            "download_bytes": voice.asset.size_bytes,
        }
        for voice in assets.VOICES
    ]


def selected_voice_key() -> str:
    """The saved choice, or the default. Never an unknown key: a voice
    removed in a later build must not leave the app unable to speak."""
    from app.core.preferences import get

    return assets.resolve_voice(get("voice_key") or "").key


def set_selected_voice(key: str) -> Optional[str]:
    """Save the chosen voice. Returns the key actually in effect, or None
    if the key was not one of ours."""
    from app.core.preferences import store

    voice = assets.find_voice(key)
    if voice is None:
        return None
    store("voice_key", voice.key)
    return selected_voice_key()


# ---------------------------------------------------------------------------
# The cloud tier's own settings. All optional, all off by default.
# ---------------------------------------------------------------------------

def selected_engine() -> str:
    """Which engine the user chose, or AUTO for the local chain.

    An unrecognised saved value resolves to AUTO rather than to nothing:
    a preference file written by a later build must never leave this one
    unable to speak.
    """
    from app.core.preferences import get

    value = (get("tts_engine") or "").strip().lower()
    return ELEVENLABS if value == ELEVENLABS else AUTO


def set_selected_engine(key: str) -> str:
    from app.core.preferences import store

    value = ELEVENLABS if (key or "").strip().lower() == ELEVENLABS else AUTO
    store("tts_engine", value)
    return selected_engine()


def selected_cloud_voice_id() -> str:
    from app.core.preferences import get

    return (get("elevenlabs_voice_id") or "").strip()


def selected_cloud_voice_name() -> str:
    """The human-readable name saved alongside the id.

    Stored because a voice id is not a label. Showing `21m00Tcm4TlvDq8ikWAM`
    as the only identification of the selected voice would be true and
    unusable.
    """
    from app.core.preferences import get

    return (get("elevenlabs_voice_name") or "").strip()


def set_selected_cloud_voice(voice_id: str, display_name: str = "") -> str:
    from app.core.preferences import store

    cleaned = (voice_id or "").strip()
    store("elevenlabs_voice_id", cleaned)
    store("elevenlabs_voice_name", (display_name or "").strip()[:80])
    return selected_cloud_voice_id()


def cloud_settings() -> dict:
    from app.core.preferences import get
    from app.voice import elevenlabs

    raw = get("elevenlabs_settings") or ""
    if not raw:
        return dict(elevenlabs.DEFAULT_SETTINGS)
    try:
        import json

        return elevenlabs.clamp_settings(json.loads(raw))
    except Exception:  # noqa: BLE001 — a corrupt preference means "use the defaults"
        return dict(elevenlabs.DEFAULT_SETTINGS)


def set_cloud_settings(values: dict) -> dict:
    import json

    from app.core.preferences import store
    from app.voice import elevenlabs

    clamped = elevenlabs.clamp_settings(values)
    store("elevenlabs_settings", json.dumps(clamped, separators=(",", ":")))
    return clamped


def fallback_allowed() -> bool:
    """Whether the local voice may speak when the cloud voice cannot.

    Defaults to True: being told why, in a message, and still hearing the
    reply is better than silence. It is a preference because somebody
    paying for a particular voice may prefer to hear nothing rather than
    hear a different one and not notice.
    """
    from app.core.preferences import get

    value = (get("elevenlabs_fallback") or "").strip().lower()
    return value != "false"


def set_fallback_allowed(allowed: bool) -> bool:
    from app.core.preferences import store

    store("elevenlabs_fallback", "true" if allowed else "false")
    return fallback_allowed()


def cloud_status() -> dict:
    """Everything the Voice page needs about the cloud tier — and never
    the key itself, only whether one exists."""
    from app.core.credentials import has_elevenlabs_key
    from app.core.privacy import privacy_mode
    from app.voice import elevenlabs

    configured = has_elevenlabs_key()
    voice_id = selected_cloud_voice_id()
    if privacy_mode.active:
        detail = "Privacy mode is on — nothing is sent to ElevenLabs while it stays on."
    elif not configured:
        detail = "No API key saved. The local voice works without one."
    elif not voice_id:
        detail = "Key saved. Choose a voice to finish setting this up."
    else:
        name = selected_cloud_voice_name() or voice_id
        detail = f"Ready — speaking as {name}."

    return {
        "selected": selected_engine() == ELEVENLABS,
        "key_configured": configured,
        "voice_id": voice_id,
        "voice_name": selected_cloud_voice_name(),
        "settings": cloud_settings(),
        "defaults": dict(elevenlabs.DEFAULT_SETTINGS),
        "ranges": {k: list(v) for k, v in elevenlabs.SETTING_RANGES.items()},
        "fallback_allowed": fallback_allowed(),
        "blocked_by_privacy": privacy_mode.active,
        "detail": detail,
        "last_fallback": last_fallback_reason(),
        "test_phrase": elevenlabs.TEST_PHRASE,
        "max_text_chars": elevenlabs.MAX_TEXT_CHARS,
    }


def selected_speed() -> float:
    from app.core.preferences import get

    return kokoro_engine.clamp_speed(get("voice_speed") or kokoro_engine.DEFAULT_SPEED)


def set_selected_speed(speed: float) -> float:
    from app.core.preferences import store

    value = kokoro_engine.clamp_speed(speed)
    store("voice_speed", f"{value:.2f}")
    return selected_speed()
