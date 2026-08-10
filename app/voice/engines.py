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
NONE = "none"

ENGINE_ORDER = (KOKORO, WINDOWS, SAPI5)

DISPLAY_NAMES = {
    KOKORO: "JARVIS neural voice",
    WINDOWS: "Windows natural voice",
    SAPI5: "Windows classic voice",
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
    chosen = active_engine(voice_key)

    if chosen == KOKORO:
        return _speak_kokoro(text, voice_key, speed)
    if chosen == WINDOWS:
        return _speak_windows(text)
    if chosen == SAPI5:
        return _speak_sapi5(text)
    return SpeakOutcome(started=False, engine=NONE, message=unavailable_message(voice_key))


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


def selected_speed() -> float:
    from app.core.preferences import get

    return kokoro_engine.clamp_speed(get("voice_speed") or kokoro_engine.DEFAULT_SPEED)


def set_selected_speed(speed: float) -> float:
    from app.core.preferences import store

    value = kokoro_engine.clamp_speed(speed)
    store("voice_speed", f"{value:.2f}")
    return selected_speed()
