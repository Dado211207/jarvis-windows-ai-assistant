"""The last-resort voice: SAPI5, through pyttsx3.

This is the robotic voice the owner asked to stop hearing, and it is kept
for one reason only — a machine that can run neither the neural voice nor
a Windows natural voice should still be able to speak rather than be
silently mute. app/voice/engines.py never selects it while either of the
others is usable.

It is the one tier that owns its own playback: pyttsx3 drives the audio
device itself and does not hand back samples, so Stop has to reach into
it rather than through app/voice/audio.py's player. That asymmetry is
why this lives in its own module instead of being inlined into the
chain.

Every pyttsx3 call is wrapped. A speech engine that raises must never
take a request, or the app, with it.
"""

import threading
from typing import Optional

from app.logging_config import get_logger

logger = get_logger("voice.sapi5")

_engine = None
_lock = threading.Lock()

# Set while an utterance is actually in flight. "An engine object
# exists" is not the same question: pyttsx3's engine is a singleton that
# outlives every utterance, so asking whether it exists reports speech
# long after the room went quiet.
_speaking = threading.Event()


def is_available() -> bool:
    try:
        import pyttsx3  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _get_engine():
    """The pyttsx3 engine, built on first use.

    Kept as a singleton because pyttsx3 does not tolerate several live
    engines on one process on Windows, and rebuilt from scratch if it
    ever fails — a driver left in a bad state stays bad otherwise.
    """
    global _engine
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is not None:
            return _engine
        import pyttsx3

        from app.config import settings

        engine = pyttsx3.init()
        engine.setProperty("rate", settings.jarvis_tts_rate)
        engine.setProperty("volume", settings.jarvis_tts_volume)
        if settings.jarvis_tts_voice:
            engine.setProperty("voice", settings.jarvis_tts_voice)
        _engine = engine
        return engine


def speak_with_sapi5(text: str) -> bool:
    """Start speaking on a worker thread. True when it started."""
    if not (text or "").strip() or not is_available():
        return False

    def _run() -> None:
        try:
            engine = _get_engine()
            engine.say(text)
            engine.runAndWait()
        except Exception:  # noqa: BLE001
            logger.warning("Classic speech engine failed.", exc_info=True)
            _reset()
        finally:
            _speaking.clear()

    _speaking.set()
    threading.Thread(target=_run, daemon=True, name="jarvis-sapi5").start()
    return True


def is_active() -> bool:
    """Whether an utterance is in flight on this engine."""
    return _speaking.is_set()


def stop_sapi5() -> bool:
    """Stop this engine. False only when it was asked to stop and could
    not — a stop that failed must not be reported as one that worked."""
    engine: Optional[object] = _engine
    if engine is None:
        return True
    try:
        engine.stop()  # type: ignore[attr-defined]
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Could not stop the classic speech engine.", exc_info=True)
        return False
    finally:
        _speaking.clear()


def _reset() -> None:
    global _engine
    with _lock:
        _engine = None
