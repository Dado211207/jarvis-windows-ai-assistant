"""Local text-to-speech service backed by pyttsx3 (offline, no cloud API needed).

One flag decides whether JARVIS speaks: `output_enabled`. It used to be
two, which is why the desktop app never talked. "Enable Speech" on the
Voice page set an in-memory `session_enabled` that only the CLI read,
while /voice/speak gated on `settings.jarvis_tts_enabled` — an
environment setting a packaged-app user cannot change. Every part
worked; nothing joined up, and the result was a button that appeared to
do something and did not.

Now: `output_enabled` reads a saved preference, falling back to the
configured default when nothing has been chosen. It is still off unless
someone opts in (CLAUDE.md's Phase 3 rule) — persisting an explicit
opt-in is not the same as defaulting to on.
"""

import threading
from dataclasses import dataclass

from app.logging_config import get_logger

logger = get_logger("voice.tts")

TTS_TEST_PHRASE = "JARVIS voice output is working."
MAX_SPEAK_LENGTH = 1000


@dataclass
class TTSResult:
    success: bool
    message: str


class TextToSpeechService:
    """Thread-safe local TTS service.  Never crashes on audio/engine failures."""

    def __init__(self) -> None:
        self._engine = None
        self._init_lock = threading.Lock()

    # --- availability ---

    def is_available(self) -> bool:
        """True when pyttsx3 can be imported (engine init happens lazily on first speak)."""
        try:
            import pyttsx3  # noqa: F401
            return True
        except Exception:
            return False

    # --- output state: the single flag every surface reads ---

    @property
    def output_enabled(self) -> bool:
        """Whether JARVIS speaks its replies. A choice saved in the app
        wins; otherwise the configured default (off) applies. Never
        raises — an unreadable preferences file means "not enabled",
        which is the safer reading for something that makes noise."""
        from app.core.preferences import get_bool

        saved = get_bool("speak_replies")
        if saved is not None:
            return saved
        try:
            from app.config import settings
            return bool(settings.jarvis_tts_enabled)
        except Exception:  # noqa: BLE001
            return False

    def set_output_enabled(self, enabled: bool) -> bool:
        """Persist the choice and return the state actually in effect.

        Reading it back rather than echoing the request matters: if the
        preferences file could not be written, the setting did not
        change, and a UI told otherwise would show a toggle that flips
        back on the next page load with no explanation."""
        from app.core.preferences import store

        store("speak_replies", "true" if enabled else "false")
        return self.output_enabled

    # --- engine lifecycle ---

    def _get_engine(self):
        """Return the pyttsx3 engine, initialising it on first call (double-checked lock)."""
        if self._engine is not None:
            return self._engine
        with self._init_lock:
            if self._engine is not None:
                return self._engine
            import pyttsx3
            from app.config import settings
            engine = pyttsx3.init()
            engine.setProperty("rate", settings.jarvis_tts_rate)
            engine.setProperty("volume", settings.jarvis_tts_volume)
            if settings.jarvis_tts_voice:
                engine.setProperty("voice", settings.jarvis_tts_voice)
            self._engine = engine
            return engine

    # --- public API ---

    def speak(self, text: str) -> TTSResult:
        """Speak *text* asynchronously in a daemon thread.  Returns immediately."""
        if not text or not text.strip():
            return TTSResult(success=False, message="Nothing to speak.")
        if len(text) > MAX_SPEAK_LENGTH:
            return TTSResult(
                success=False,
                message=f"Text too long ({len(text)} chars; max {MAX_SPEAK_LENGTH}).",
            )
        if not self.is_available():
            return TTSResult(success=False, message="TTS engine (pyttsx3) is not available.")

        def _run() -> None:
            try:
                engine = self._get_engine()
                engine.say(text)
                engine.runAndWait()
            except Exception as exc:
                logger.warning("TTS speak error: %s", exc)

        thread = threading.Thread(target=_run, daemon=True, name="jarvis-tts")
        thread.start()
        preview = text[:60] + ("…" if len(text) > 60 else "")
        return TTSResult(success=True, message=f"Speaking: {preview!r}")

    def stop(self) -> TTSResult:
        """Interrupt any currently active speech."""
        if self._engine is None:
            return TTSResult(success=True, message="TTS is not active.")
        try:
            self._engine.stop()
            return TTSResult(success=True, message="Speech stopped.")
        except Exception as exc:
            logger.warning("TTS stop error: %s", exc)
            return TTSResult(success=False, message=f"Stop failed: {exc}")


# Module-level singleton
tts_service = TextToSpeechService()


# ---------------------------------------------------------------------------
# Tool handlers (registered via register_tools)
# ---------------------------------------------------------------------------

def _tts_enable() -> dict:
    enabled = tts_service.set_output_enabled(True)
    if not enabled:
        return {
            "success": False,
            "message": "Voice output could not be turned on — the setting could not be saved.",
            "data": {"enabled": False},
        }
    if not tts_service.is_available():
        # Honest: the setting really did change, and it really will not
        # produce sound on this machine.
        return {
            "success": True,
            "message": (
                "Voice output is on, but no speech engine is available on this "
                "system, so nothing will be spoken."
            ),
            "data": {"enabled": True},
        }
    return {"success": True, "message": "Voice output is on. JARVIS will speak its replies.", "data": {"enabled": True}}


def _tts_disable() -> dict:
    enabled = tts_service.set_output_enabled(False)
    return {
        "success": not enabled,
        "message": (
            "Voice output is off."
            if not enabled
            else "Voice output could not be turned off — the setting could not be saved."
        ),
        "data": {"enabled": enabled},
    }


def _tts_status() -> dict:
    from app.config import settings
    available = tts_service.is_available()
    lines = [
        "Voice output status:",
        f"  Speaks replies   : {tts_service.output_enabled}",
        f"  Engine available : {available}",
        f"  Engine           : {settings.jarvis_tts_engine}",
        f"  Rate             : {settings.jarvis_tts_rate} wpm",
        f"  Volume           : {settings.jarvis_tts_volume}",
    ]
    return {"success": True, "message": "\n".join(lines), "data": None}


def _tts_test() -> dict:
    if not tts_service.is_available():
        return {
            "success": False,
            "message": "Voice output is not available on this system, so nothing can be spoken.",
            "data": None,
        }
    result = tts_service.speak(TTS_TEST_PHRASE)
    msg = (
        f'Speaking test phrase: "{TTS_TEST_PHRASE}"'
        if result.success
        else result.message
    )
    return {"success": result.success, "message": msg, "data": None}


def _tts_stop() -> dict:
    result = tts_service.stop()
    return {"success": result.success, "message": result.message, "data": None}


def register_tools(registry) -> None:
    from app.core.models import PermissionLevel, ToolCategory, ToolDefinition

    registry.register(
        ToolDefinition(
            name="tts_enable",
            description="Turn on spoken replies ('speak on'). The choice is remembered.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.VOICE,
        ),
        _tts_enable,
    )
    registry.register(
        ToolDefinition(
            name="tts_disable",
            description="Turn off spoken replies ('speak off'). The choice is remembered.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.VOICE,
        ),
        _tts_disable,
    )
    registry.register(
        ToolDefinition(
            name="tts_status",
            description="Show whether JARVIS speaks its replies, and the speech engine status.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.VOICE,
        ),
        _tts_status,
    )
    registry.register(
        ToolDefinition(
            name="tts_test",
            description="Speak a test phrase to verify voice output is working.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.VOICE,
        ),
        _tts_test,
    )
    registry.register(
        ToolDefinition(
            name="tts_stop",
            description="Stop any currently active speech.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.VOICE,
        ),
        _tts_stop,
    )
