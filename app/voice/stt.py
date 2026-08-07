"""Speech-to-text adapters for push-to-talk voice input (v0.2).

Push-to-talk only — no wake word, no always-listening, no continuous
audio capture or storage. A user explicitly starts and stops each
recording via the dashboard's push-to-talk button; the browser uploads
one short clip per press, the server writes it to a single temporary
file, transcribes it, and deletes that file immediately afterward —
success or failure. Nothing about a recording is ever committed to git
or written to a persistent log; see app/api/routes.py's transcribe
endpoint for exactly where that temp file lives and how it's cleaned up.

Two adapters behind one interface:
  - FakeSTTAdapter: deterministic, no real audio processing. Every
    automated test in this repository uses this — none exercise real
    model inference, since no real model is available in CI.
  - FasterWhisperAdapter: optional, real local transcription via the
    faster-whisper package (deliberately NOT in requirements.txt — see
    requirements-voice.txt). Disabled by default
    (JARVIS_STT_ENABLED=false) and, even when enabled, never silently
    downloads a model: it requires either a local JARVIS_STT_MODEL_PATH
    or an explicit JARVIS_STT_ALLOW_DOWNLOAD=true opt-in.

stt_service resolves to whichever adapter is actually usable at call
time and reports an honest "not installed" / "not configured" reason
when neither is. Text input remains fully functional regardless.
"""

import concurrent.futures
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Tuple

from app.logging_config import get_logger

logger = get_logger("voice.stt")

MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB — generous for a short push-to-talk clip
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass
class STTResult:
    success: bool
    text: str
    message: str


class STTAdapter(Protocol):
    def is_available(self) -> Tuple[bool, str]: ...
    def transcribe(self, audio_path: Path, timeout_seconds: float) -> STTResult: ...


class FakeSTTAdapter:
    """Deterministic adapter for tests — never touches real audio, a real
    model, or the filesystem beyond reading the path it's given (which it
    doesn't even open). Records every call for test assertions."""

    def __init__(self, transcript: str = "this is a fake transcript", available: bool = True) -> None:
        self._transcript = transcript
        self._available = available
        self.calls = []

    def is_available(self) -> Tuple[bool, str]:
        return self._available, ("fake adapter available" if self._available else "fake adapter forced unavailable")

    def transcribe(self, audio_path: Path, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> STTResult:
        self.calls.append((audio_path, timeout_seconds))
        return STTResult(success=True, text=self._transcript, message="Transcribed (fake adapter — test only).")


class FasterWhisperAdapter:
    """Real local transcription via faster-whisper, CPU by default."""

    def __init__(self) -> None:
        self._model = None
        self._model_lock = threading.Lock()

    def is_available(self) -> Tuple[bool, str]:
        from app.config import settings

        if not settings.jarvis_stt_enabled:
            return False, "STT is disabled (set JARVIS_STT_ENABLED=true in .env to enable)."
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False, "faster-whisper is not installed. Run: pip install -r requirements-voice.txt"
        return True, "faster-whisper is available."

    def _get_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            from app.config import settings
            from faster_whisper import WhisperModel

            if settings.jarvis_stt_model_path:
                model_ref = settings.jarvis_stt_model_path  # a local path never triggers a download
            elif settings.jarvis_stt_allow_download:
                model_ref = settings.jarvis_stt_model_size  # explicit opt-in to fetch from HF Hub
            else:
                raise RuntimeError(
                    f"No local model at JARVIS_STT_MODEL_PATH and JARVIS_STT_ALLOW_DOWNLOAD is "
                    f"not set — refusing to silently download '{settings.jarvis_stt_model_size}'. "
                    "Set JARVIS_STT_MODEL_PATH to an already-downloaded local model, or set "
                    "JARVIS_STT_ALLOW_DOWNLOAD=true to allow fetching it."
                )
            logger.info("Loading STT model: %s", model_ref)
            self._model = WhisperModel(model_ref, device="cpu", compute_type="int8")
            return self._model

    def transcribe(self, audio_path: Path, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> STTResult:
        def _run() -> str:
            model = self._get_model()
            segments, _info = model.transcribe(str(audio_path))
            return " ".join(seg.text.strip() for seg in segments).strip()

        # A short-lived, single-use pool — mirrors app/core/tool_registry.py's
        # bounded-timeout pattern for the same reason: Python cannot safely
        # force-kill a running thread, so a timeout here means "stop
        # waiting," not "guaranteed stopped." The abandoned thread's result,
        # if it ever completes, is discarded — never returned, never logged
        # with content, never persisted.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-stt")
        try:
            future = executor.submit(_run)
            text = future.result(timeout=timeout_seconds)
            return STTResult(success=True, text=text, message="Transcribed.")
        except concurrent.futures.TimeoutError:
            future.cancel()
            return STTResult(
                success=False, text="",
                message=f"Transcription did not finish within {timeout_seconds:.0f}s and was stopped.",
            )
        except Exception as exc:
            logger.warning("STT transcription failed: %s", type(exc).__name__)
            return STTResult(success=False, text="", message="Transcription failed. See server logs for detail.")
        finally:
            executor.shutdown(wait=False)


class STTService:
    """Resolves to the real adapter unless a test has injected an
    override — see set_adapter_override()."""

    def __init__(self) -> None:
        self._real: STTAdapter = FasterWhisperAdapter()
        self._override: Optional[STTAdapter] = None

    def set_adapter_override(self, adapter: Optional[STTAdapter]) -> None:
        """Test-only injection point. Pass None to restore the real
        adapter."""
        self._override = adapter

    def _adapter(self) -> STTAdapter:
        return self._override if self._override is not None else self._real

    def is_available(self) -> Tuple[bool, str]:
        return self._adapter().is_available()

    def transcribe(self, audio_path: Path, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> STTResult:
        available, reason = self.is_available()
        if not available:
            return STTResult(success=False, text="", message=reason)
        return self._adapter().transcribe(audio_path, timeout_seconds)


# Module-level singleton, matching tts_service's own pattern.
stt_service = STTService()
