"""Local text-to-speech service backed by pyttsx3 (offline, no cloud API needed)."""

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
        self._session_enabled: bool = False

    # --- availability ---

    def is_available(self) -> bool:
        """True when pyttsx3 can be imported (engine init happens lazily on first speak)."""
        try:
            import pyttsx3  # noqa: F401
            return True
        except Exception:
            return False

    # --- session state (CLI only; API uses settings.jarvis_tts_enabled) ---

    @property
    def session_enabled(self) -> bool:
        return self._session_enabled

    @session_enabled.setter
    def session_enabled(self, value: bool) -> None:
        self._session_enabled = value

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
    tts_service.session_enabled = True
    if not tts_service.is_available():
        return {
            "success": True,
            "message": (
                "Voice output enabled for this session, but pyttsx3 is not installed. "
                "Run: pip install pyttsx3"
            ),
            "data": None,
        }
    return {"success": True, "message": "Voice output enabled for this session.", "data": None}


def _tts_disable() -> dict:
    tts_service.session_enabled = False
    return {"success": True, "message": "Voice output disabled for this session.", "data": None}


def _tts_status() -> dict:
    from app.config import settings
    available = tts_service.is_available()
    lines = [
        "TTS (Text-to-Speech) status:",
        f"  Config enabled   : {settings.jarvis_tts_enabled}",
        f"  Session enabled  : {tts_service.session_enabled}",
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
            "message": "TTS test failed: pyttsx3 is not available. Run: pip install pyttsx3",
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
            description="Enable voice output for the current session ('speak on').",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.VOICE,
        ),
        _tts_enable,
    )
    registry.register(
        ToolDefinition(
            name="tts_disable",
            description="Disable voice output for the current session ('speak off').",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.VOICE,
        ),
        _tts_disable,
    )
    registry.register(
        ToolDefinition(
            name="tts_status",
            description="Show TTS engine status and whether voice output is enabled.",
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
