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
  - FasterWhisperAdapter: real local transcription via the
    faster-whisper package. It **ships with the Windows installer** (see
    requirements-windows.txt and packaging/jarvis.spec); it previously
    did not, which meant the packaged app reported "Speech runtime — Not
    ready" permanently and no user action could change that. It still
    never silently downloads a model: it requires either a local
    JARVIS_STT_MODEL_PATH, a model installed from the Voice page, or an
    explicit JARVIS_STT_ALLOW_DOWNLOAD=true opt-in.

stt_service resolves to whichever adapter is actually usable at call
time and reports an honest "not installed" / "not configured" reason
when neither is. Text input remains fully functional regardless.

Four states, deliberately distinguishable, because a single "Not ready"
line is what made the reported failure impossible to act on:

    input_enabled()   the feature is offered at all (a user switch)
    runtime_status()  the speech engine is present and loadable
    model_status()    a model is on disk
    model_path()      *where* it is, so "no model" and "a model JARVIS
                      is not looking at" are different answers
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


def input_enabled() -> bool:
    """Whether push-to-talk is offered at all.

    A saved preference wins over the environment variable, which supplies
    the starting default — preferences.py's documented precedence rule.
    This exists because the packaged app previously had *no* way to turn
    voice input on: the setting was environment-only, defaulted off, and
    the status message told users to "turn it on from the Voice page",
    where no such control existed.

    What this flag is, and is not: it decides whether the feature is
    offered, not whether the microphone is open. Nothing in this codebase
    can capture audio without a button being pressed in the UI and the
    OS/WebView2 microphone permission being granted — those are the real
    gates, and they are unchanged. There is still no wake word and no
    continuous listening.
    """
    from app.config import settings
    from app.core.preferences import get_bool

    saved = get_bool("stt_enabled")
    if saved is not None:
        return saved
    return bool(settings.jarvis_stt_enabled)


@dataclass
class STTResult:
    success: bool
    text: str
    message: str


class STTAdapter(Protocol):
    def is_available(self) -> Tuple[bool, str]: ...
    def runtime_status(self) -> Tuple[bool, str]: ...
    def model_status(self) -> Tuple[bool, str]: ...
    def model_path(self) -> Optional[Path]: ...
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

    def model_status(self) -> Tuple[bool, str]:
        return self._available, ("fake model ready — test only" if self._available else "fake adapter forced unavailable")

    def runtime_status(self) -> Tuple[bool, str]:
        return self._available, ("fake runtime ready — test only" if self._available else "fake adapter forced unavailable")

    def model_path(self) -> Optional[Path]:
        return Path("/fake/model/path") if self._available else None

    def transcribe(self, audio_path: Path, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> STTResult:
        self.calls.append((audio_path, timeout_seconds))
        return STTResult(success=True, text=self._transcript, message="Transcribed (fake adapter — test only).")


class FasterWhisperAdapter:
    """Real local transcription via faster-whisper, CPU by default."""

    def __init__(self) -> None:
        self._model = None
        self._model_lock = threading.Lock()

    def is_available(self) -> Tuple[bool, str]:
        return self.runtime_status()

    def runtime_status(self) -> Tuple[bool, str]:
        """Whether the speech engine itself is present, ignoring the
        on/off switch.

        Separate from is_available() so the Voice diagnostics panel can
        say "the runtime is installed, you have simply switched voice
        input off" — which is a different problem from a broken install,
        and the two were previously indistinguishable.
        """
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            # No pip instruction here on purpose: someone running the
            # installed JARVIS.exe has no terminal and no source tree to
            # run pip in. The speech engine ships with the installer, so
            # if it's genuinely missing, that's a broken install to
            # report — not a step to hand the user.
            return False, (
                "The local speech engine isn't available in this installation. "
                "Reinstalling JARVIS should restore it."
            )
        except Exception as exc:  # noqa: BLE001 — a broken native dependency
            # ctranslate2 is a compiled extension; on a machine missing a
            # VC++ runtime it raises OSError, not ImportError, and an
            # ImportError-only check reported the engine as present right
            # up until the first transcription failed.
            logger.warning("The speech engine failed to load: %s", type(exc).__name__)
            return False, (
                "The local speech engine is installed but could not be loaded on this "
                "machine. Reinstalling JARVIS usually fixes this."
            )
        return True, "Speech engine ready."

    def _guided_install_dir(self) -> Path:
        """Where app/voice/model_installer.py's "Install local speech
        model" action puts a model. Checking this here — not just
        JARVIS_STT_MODEL_PATH — is what makes a guided install usable
        immediately: no .env edit, no restart, matching CLAUDE.md's
        first-run requirement."""
        from app.voice.model_installer import default_install_dir
        return default_install_dir()

    def model_path(self) -> Optional[Path]:
        """Where the model actually is, or None. Reported by the Voice
        diagnostics panel: "no model" and "a model in a folder JARVIS
        isn't looking at" are different problems, and a status line that
        only says "Not ready" cannot tell them apart."""
        from app.config import settings

        configured = settings.jarvis_stt_model_path
        if configured:
            path = Path(configured)
            return path if path.exists() else None
        guided_dir = self._guided_install_dir()
        return guided_dir if guided_dir.exists() else None

    def model_status(self) -> Tuple[bool, str]:
        """Whether a usable model is ready *right now* — never loads the
        model or touches the network. Distinct from is_available(),
        which only checks the feature is enabled and the package is
        installed; a model can still be missing even when both are
        true, which is exactly the case the first-run onboarding
        checklist (and the guided model-install flow) needs to detect."""
        from app.config import settings

        if settings.jarvis_stt_model_path:
            if Path(settings.jarvis_stt_model_path).exists():
                return True, f"Local model ready at {settings.jarvis_stt_model_path}"
            return False, f"JARVIS_STT_MODEL_PATH is set but does not exist: {settings.jarvis_stt_model_path}"
        guided_dir = self._guided_install_dir()
        if guided_dir.exists():
            return True, f"Local model ready at {guided_dir}"
        if settings.jarvis_stt_allow_download:
            return False, "No local model yet — will download on first use (JARVIS_STT_ALLOW_DOWNLOAD=true)."
        return False, "No speech model installed yet — install one from the Voice page."

    def _get_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            from app.config import settings
            from faster_whisper import WhisperModel

            guided_dir = self._guided_install_dir()
            if settings.jarvis_stt_model_path:
                model_ref = settings.jarvis_stt_model_path  # a local path never triggers a download
            elif guided_dir.exists():
                model_ref = str(guided_dir)  # installed via the setup page's guided download
            elif settings.jarvis_stt_allow_download:
                model_ref = settings.jarvis_stt_model_size  # explicit opt-in to fetch from HF Hub
            else:
                raise RuntimeError(
                    f"No local model at JARVIS_STT_MODEL_PATH, no guided-install model at "
                    f"{guided_dir}, and JARVIS_STT_ALLOW_DOWNLOAD is not set — refusing to "
                    f"silently download '{settings.jarvis_stt_model_size}'. Install a model from "
                    "the Setup page, set JARVIS_STT_MODEL_PATH to an already-downloaded local "
                    "model, or set JARVIS_STT_ALLOW_DOWNLOAD=true to allow fetching it."
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
        """Usable right now — which the on/off switch is part of.

        Checked here rather than inside an adapter: whether the feature is
        offered is policy about the product, not a property of any
        particular speech engine, and putting it in one adapter meant the
        switch was silently ignored by every other one.
        """
        if not input_enabled():
            return False, "Voice input is turned off. Turn it on from the Voice page to use push-to-talk."
        return self._adapter().is_available()

    def runtime_status(self) -> Tuple[bool, str]:
        return self._adapter().runtime_status()

    def model_status(self) -> Tuple[bool, str]:
        return self._adapter().model_status()

    def model_path(self) -> Optional[Path]:
        return self._adapter().model_path()

    def transcribe(self, audio_path: Path, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> STTResult:
        available, reason = self.is_available()
        if not available:
            return STTResult(success=False, text="", message=reason)
        return self._adapter().transcribe(audio_path, timeout_seconds)


# Module-level singleton, matching tts_service's own pattern.
stt_service = STTService()
