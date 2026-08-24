"""Spoken replies: one flag, one service, three engines behind it.

One flag decides whether JARVIS speaks: `output_enabled`. It used to be
two, which is why the desktop app never talked. "Enable Speech" on the
Voice page set an in-memory `session_enabled` that only the CLI read,
while /voice/speak gated on `settings.jarvis_tts_enabled` — an
environment setting a packaged-app user cannot change. Every part
worked; nothing joined up, and the result was a button that appeared to
do something and did not. That flag is unchanged here and every surface
still reads it.

What changed underneath is which engine makes the sound.
app/voice/engines.py picks the best one available — the local neural
voice first, Windows' own natural voices second, the old robotic SAPI5
voice only when neither can run. This service does not know how any of
them work; it owns whether to speak, what may be spoken, and what to say
when nothing can.

Speech is still off until someone opts in (CLAUDE.md's Phase 3 rule),
and this is still output only: nothing here opens a microphone.
"""

import threading
from dataclasses import dataclass
from typing import Optional

from app.logging_config import get_logger

logger = get_logger("voice.tts")

TTS_TEST_PHRASE = "JARVIS voice output is working."
MAX_SPEAK_LENGTH = 1000


@dataclass
class TTSResult:
    success: bool
    message: str
    engine: str = ""
    fallback: Optional[dict] = None


class TextToSpeechService:
    """Thread-safe. Never crashes on an audio or engine failure."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    # --- availability ---

    def is_available(self) -> bool:
        """True when *something* on this machine can speak."""
        return self.active_engine() != "none"

    def active_engine(self) -> str:
        from app.voice import engines
        from app.core.privacy import privacy_mode

        selected = engines.selected_engine()
        if selected == engines.OPENAI:
            if engines.openai_key_configured() and not privacy_mode.active:
                return engines.OPENAI
            return (
                engines.active_engine(self.voice_key)
                if engines.openai_fallback_allowed() else engines.NONE
            )
        if selected == engines.ELEVENLABS:
            from app.core.credentials import has_elevenlabs_key

            if (
                has_elevenlabs_key()
                and engines.selected_cloud_voice_id()
                and not privacy_mode.active
            ):
                return engines.ELEVENLABS
            return (
                engines.active_engine(self.voice_key)
                if engines.fallback_allowed() else engines.NONE
            )
        return engines.active_engine(self.voice_key)

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

    # --- voice selection ---

    @property
    def voice_key(self) -> str:
        from app.voice import engines

        return engines.selected_voice_key()

    @property
    def speed(self) -> float:
        from app.voice import engines

        return engines.selected_speed()

    # --- speaking ---

    def speak(self, text: str) -> TTSResult:
        """Start speaking *text*. Returns as soon as playback has begun,
        never after it has finished."""
        from app.voice import engines

        if not text or not text.strip():
            return TTSResult(success=False, message="Nothing to speak.")
        if len(text) > MAX_SPEAK_LENGTH:
            return TTSResult(
                success=False,
                message=f"Text too long ({len(text)} chars; max {MAX_SPEAK_LENGTH}).",
            )

        outcome = engines.speak(text, voice_key=self.voice_key, speed=self.speed)
        if not outcome.started:
            return TTSResult(
                success=False,
                message=outcome.message,
                engine=outcome.engine,
                fallback=outcome.fallback.as_dict() if outcome.fallback else None,
            )

        preview = text[:60] + ("…" if len(text) > 60 else "")
        if outcome.message != "Speaking.":
            return TTSResult(
                success=True,
                message=f"{outcome.message} Speaking: {preview!r}",
                engine=outcome.engine,
                fallback=outcome.fallback.as_dict() if outcome.fallback else None,
            )
        return TTSResult(
            success=True, message=f"Speaking: {preview!r}", engine=outcome.engine,
        )

    def stop(self) -> TTSResult:
        """Interrupt any speech in progress, whichever engine is making
        it. Safe to call when nothing is playing, and honest about which
        of those two things happened."""
        from app.voice import engines

        try:
            was_speaking = engines.is_speaking()
            stopped = engines.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("TTS stop error: %s", exc)
            return TTSResult(success=False, message=f"Stop failed: {exc}")

        if not stopped:
            return TTSResult(
                success=False,
                message="Stop failed: the speech engine did not respond to it.",
            )
        if not was_speaking:
            return TTSResult(success=True, message="TTS is not active.")
        return TTSResult(success=True, message="Speech stopped.")


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
        # produce sound on this machine until something can speak.
        from app.voice import engines

        return {
            "success": True,
            "message": f"Voice output is on, but {engines.unavailable_message(tts_service.voice_key)}",
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
    """Every engine's own answer, not a single verdict — "speech is not
    available" tells nobody what to do about it."""
    from app.voice import status

    return {"success": True, "message": status.status_text(), "data": None}


def _tts_test() -> dict:
    if not tts_service.is_available():
        from app.voice import engines

        return {
            "success": False,
            "message": engines.unavailable_message(tts_service.voice_key),
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
