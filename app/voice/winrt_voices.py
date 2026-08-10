"""Windows' own natural voices, as the secondary fallback.

The order the owner set is Kokoro first, these second, and the old SAPI5
voice only as a last resort. This module is the middle tier: the modern
Windows speech voices, reached through the WinRT projection rather than
the legacy SAPI interface pyttsx3 uses.

Two different things are called "Windows voices" and the difference
matters here. The SAPI5 voices (Microsoft David, Zira) are the robotic
ones this release exists to stop using. The natural voices — installed
through Settings, Accessibility, Narrator, "Add natural voices" — are
neural, run offline once installed, and are a genuine improvement. This
module prefers the latter and says which one it got.

Nothing here is required. The projection may not be installed, the
machine may have no natural voice, and both are ordinary states rather
than errors: `is_available()` answers honestly and the caller moves down
to the next engine. Every import is local to the function that needs it,
matching the rule the rest of this codebase's Windows-only code follows.
"""

import sys
import threading
from dataclasses import dataclass
from typing import List, Optional

from app.logging_config import get_logger

logger = get_logger("voice.winrt")

# Substring Microsoft uses in the display name of the offline neural
# voices. Matching on it is how a natural voice is told apart from a
# SAPI-era one through an API that lists both.
NATURAL_MARKER = "natural"


@dataclass(frozen=True)
class WinRTVoice:
    identifier: str
    display_name: str
    language: str
    natural: bool


def _synthesiser_class():
    """The WinRT SpeechSynthesizer, or None.

    Two projections exist and which one a machine has is not something
    this code should care about: `winsdk` is the current package,
    `winrt` the older name for the same thing.
    """
    if sys.platform != "win32":
        return None
    import importlib

    for module_name in (
        "winsdk.windows.media.speechsynthesis",
        "winrt.windows.media.speechsynthesis",
    ):
        try:
            module = importlib.import_module(module_name)
            return module.SpeechSynthesizer
        except Exception:  # noqa: BLE001
            continue
    return None


def projection_available() -> bool:
    """Whether the WinRT speech API can be reached at all."""
    return _synthesiser_class() is not None


def list_voices() -> List[WinRTVoice]:
    """Every voice Windows offers, natural ones marked. Empty on any
    failure — this is a fallback, and a fallback that raises is worse
    than one that is simply absent."""
    synthesiser = _synthesiser_class()
    if synthesiser is None:
        return []
    try:
        voices = []
        for voice in synthesiser.all_voices:
            name = str(getattr(voice, "display_name", "") or "")
            voices.append(
                WinRTVoice(
                    identifier=str(getattr(voice, "id", "") or ""),
                    display_name=name,
                    language=str(getattr(voice, "language", "") or ""),
                    natural=NATURAL_MARKER in name.lower(),
                )
            )
        return voices
    except Exception:  # noqa: BLE001
        logger.warning("Could not enumerate Windows voices.", exc_info=True)
        return []


def best_voice() -> Optional[WinRTVoice]:
    """The voice this fallback would use.

    A natural British male voice if there is one, then any natural voice,
    then any British voice, then whatever Windows considers default. The
    preference order is the product's, and it is applied here rather than
    left to Windows so the choice is explicable.
    """
    voices = list_voices()
    if not voices:
        return None

    def british(voice: WinRTVoice) -> bool:
        return voice.language.lower().startswith("en-gb")

    for predicate in (
        lambda v: v.natural and british(v),
        lambda v: v.natural,
        british,
        lambda v: True,
    ):
        for voice in voices:
            if predicate(voice):
                return voice
    return None


def is_available() -> bool:
    return best_voice() is not None


def describe() -> str:
    """What this engine would sound like, for the diagnostics panel."""
    if sys.platform != "win32":
        return "Windows natural voices are only available on Windows."
    if not projection_available():
        return (
            "The Windows speech API is not available in this build, so Windows' own "
            "voices cannot be used."
        )
    voice = best_voice()
    if voice is None:
        return "Windows reports no installed speech voices."
    if voice.natural:
        return f"Would use {voice.display_name}, a natural Windows voice."
    return (
        f"Would use {voice.display_name}. This is an older Windows voice — natural "
        "voices can be added in Settings, Accessibility, Narrator."
    )


def synthesise_wav(text: str, voice: Optional[WinRTVoice] = None) -> Optional[bytes]:
    """Speech as WAV bytes, or None if this engine cannot produce it.

    Returning bytes rather than playing them keeps playback in one place
    (app/voice/audio.py), so Stop works the same way whichever engine
    produced the sound.
    """
    synthesiser_class = _synthesiser_class()
    if synthesiser_class is None or not (text or "").strip():
        return None

    chosen = voice or best_voice()
    try:
        synthesiser = synthesiser_class()
        if chosen is not None:
            for candidate in synthesiser_class.all_voices:
                if str(getattr(candidate, "id", "")) == chosen.identifier:
                    synthesiser.voice = candidate
                    break
        return _read_stream(synthesiser.synthesize_text_to_stream_async(text))
    except Exception:  # noqa: BLE001
        logger.warning("Windows speech synthesis failed.", exc_info=True)
        return None


def _read_stream(operation, timeout_seconds: float = 30.0) -> Optional[bytes]:
    """Drain a WinRT async stream operation into bytes.

    The projection exposes these as awaitables; this is called from
    ordinary threaded code, so it is resolved with the projection's own
    blocking `get()` on a worker thread and bounded by a timeout — an
    unbounded wait inside a speech call would hang the request that
    triggered it.
    """
    result: dict = {}

    def _drain() -> None:
        try:
            stream = operation.get()
            size = int(stream.size)
            if size <= 0:
                result["bytes"] = b""
                return
            import winsdk.windows.storage.streams as streams  # noqa: PLC0415

            reader = streams.DataReader(stream.get_input_stream_at(0))
            reader.load_async(size).get()
            result["bytes"] = bytes(reader.read_bytes(size))
        except Exception:  # noqa: BLE001
            logger.warning("Could not read the Windows speech stream.", exc_info=True)

    worker = threading.Thread(target=_drain, daemon=True, name="jarvis-winrt-tts")
    worker.start()
    worker.join(timeout=timeout_seconds)
    return result.get("bytes")
