"""Guided local speech-model download — the "Install local speech
model" action on the setup page. Never runs without an explicit user
action (no silent download, matching CLAUDE.md's STT rules): the exact
model, source, license, size, destination, and per-file checksums are
shown before anything is fetched.

model.bin's SHA256 is fetched live from Hugging Face's API at
install/preview time, never hardcoded here — this stays correct if the
model repository is ever updated, rather than silently failing every
future install against a stale value baked into this file. The three
small supporting files (config.json, tokenizer.json, vocabulary.txt)
aren't LFS-tracked and don't carry a content hash from the API, so
their integrity check is a downloaded-byte-count match instead of a
SHA256 — stated plainly here rather than silently treating all four
files as equally verified.

Runs as a background thread with cooperative cancellation and a polled
progress object — the same "an external call must never block or be
allowed to silently outlive the request that started it" shape already
used for tool execution, STT transcription, and the OS credential store
(app/core/tool_registry.py, FasterWhisperAdapter.transcribe(),
app/core/credentials.py).
"""

import hashlib
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import httpx

from app.core.app_paths import models_dir
from app.logging_config import get_logger

logger = get_logger("voice.model_installer")

HF_API_URL = "https://huggingface.co/api/models/{repo}?blobs=true"
HF_RESOLVE_URL = "https://huggingface.co/{repo}/resolve/main/{filename}"

MODEL_REPO = "Systran/faster-whisper-tiny"
MODEL_DISPLAY_NAME = "Whisper tiny (multilingual, CTranslate2)"
MODEL_LICENSE_FALLBACK = "MIT"
MODEL_SOURCE_URL = f"https://huggingface.co/{MODEL_REPO}"
INSTALL_DIR_NAME = "faster-whisper-tiny"
REQUIRED_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")

# Honest, not a marketing claim: Whisper's own published language list is
# used verbatim; nothing here asserts JARVIS has verified quality for any
# specific language, and Montenegrin is named explicitly because it is
# NOT a separate language Whisper was trained on (distinct from, but
# often confused with, Serbian/Croatian/Bosnian, which share close
# linguistic roots but are not interchangeable for transcription purposes).
LANGUAGE_NOTE = (
    "Whisper tiny's training data covers about 99 languages, including Serbian, per "
    "OpenAI's original whisper-tiny model card. JARVIS has not independently verified "
    "transcription quality for any specific language. Montenegrin is not a separate "
    "language Whisper was trained on."
)

DOWNLOAD_CHUNK_BYTES = 256 * 1024


def default_install_dir() -> Path:
    return models_dir() / INSTALL_DIR_NAME


@dataclass
class ModelFileInfo:
    name: str
    size: int
    sha256: Optional[str] = None  # None means "verified by size only" — see module docstring


@dataclass
class ModelInfo:
    repo: str
    display_name: str
    license: str
    source_url: str
    destination: str
    language_note: str
    files: List[ModelFileInfo]
    total_size: int


def fetch_model_info(timeout_seconds: float = 10.0) -> ModelInfo:
    """Live query to Hugging Face's API. Raises on failure — callers
    (the /onboarding/speech-model/info route, and the installer's own
    background thread) turn that into a safe, honest error rather than
    ever falling back to guessed or cached values."""
    response = httpx.get(HF_API_URL.format(repo=MODEL_REPO), timeout=timeout_seconds)
    response.raise_for_status()
    data = response.json()

    by_name = {}
    for sibling in data.get("siblings", []):
        name = sibling.get("rfilename")
        if name not in REQUIRED_FILES:
            continue
        by_name[name] = ModelFileInfo(
            name=name,
            size=sibling.get("size", 0),
            sha256=(sibling.get("lfs") or {}).get("sha256"),
        )

    files = [by_name[name] for name in REQUIRED_FILES if name in by_name]
    return ModelInfo(
        repo=MODEL_REPO,
        display_name=MODEL_DISPLAY_NAME,
        license=(data.get("cardData") or {}).get("license") or MODEL_LICENSE_FALLBACK,
        source_url=MODEL_SOURCE_URL,
        destination=str(default_install_dir()),
        language_note=LANGUAGE_NOTE,
        files=files,
        total_size=sum(f.size for f in files),
    )


@dataclass
class InstallState:
    status: str = "idle"  # idle | checking | downloading | verifying | installing | complete | error | cancelled
    current_file: str = ""
    bytes_downloaded: int = 0
    bytes_total: int = 0
    message: str = ""


class ModelInstaller:
    """One install at a time — matches this codebase's existing
    single-process, single-flight model (pending actions, the session
    token store, etc. all work the same way)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = InstallState()
        self._cancel_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def state(self) -> InstallState:
        with self._lock:
            return InstallState(**vars(self._state))

    def _set_state(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self._state, key, value)

    def start(self) -> bool:
        """Returns False without starting a second install if one is
        already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel_event.clear()
            self._state = InstallState(status="checking", message="Checking model info…")
            thread = threading.Thread(target=self._run, daemon=True, name="jarvis-model-install")
            self._thread = thread
        thread.start()
        return True

    def cancel(self) -> None:
        self._cancel_event.set()

    def _cancelled(self) -> bool:
        if self._cancel_event.is_set():
            self._set_state(status="cancelled", message="Cancelled.")
            return True
        return False

    def _run(self) -> None:
        try:
            info = fetch_model_info()
        except Exception as exc:
            logger.warning("Could not fetch speech model info: %s", type(exc).__name__, exc_info=True)
            self._set_state(status="error", message="Could not reach Hugging Face to check the model. Check your connection and try again.")
            return

        self._set_state(status="downloading", bytes_total=info.total_size, bytes_downloaded=0)
        downloaded_so_far = 0
        tmp_dir = Path(tempfile.mkdtemp(prefix="jarvis_model_dl_"))

        try:
            for file_info in info.files:
                if self._cancelled():
                    return

                self._set_state(current_file=file_info.name)
                target = tmp_dir / file_info.name
                hasher = hashlib.sha256()
                url = HF_RESOLVE_URL.format(repo=info.repo, filename=file_info.name)

                with httpx.stream("GET", url, timeout=30.0, follow_redirects=True) as response:
                    response.raise_for_status()
                    with open(target, "wb") as fh:
                        for chunk in response.iter_bytes(chunk_size=DOWNLOAD_CHUNK_BYTES):
                            if self._cancelled():
                                return
                            fh.write(chunk)
                            hasher.update(chunk)
                            downloaded_so_far += len(chunk)
                            self._set_state(bytes_downloaded=downloaded_so_far)

                if not self._verify_file(target, file_info, hasher):
                    return

            self._set_state(status="installing", message="Installing…")
            dest_dir = Path(info.destination)
            dest_dir.parent.mkdir(parents=True, exist_ok=True)
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            shutil.move(str(tmp_dir), str(dest_dir))

            self._set_state(status="complete", message=f"Speech model installed at {dest_dir}.")
            logger.info("Speech model installed at %s", dest_dir)

        except Exception as exc:
            logger.warning("Speech model install failed: %s", type(exc).__name__, exc_info=True)
            self._set_state(status="error", message="Download failed. Check your connection and try again.")
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _verify_file(self, target: Path, file_info: ModelFileInfo, hasher) -> bool:
        self._set_state(status="verifying", message=f"Verifying {file_info.name}…")
        if file_info.sha256:
            actual = hasher.hexdigest()
            if actual != file_info.sha256:
                self._set_state(status="error", message=f"Checksum mismatch for {file_info.name} — download may be corrupted. Try again.")
                return False
        else:
            actual_size = target.stat().st_size
            if actual_size != file_info.size:
                self._set_state(status="error", message=f"{file_info.name} downloaded with an unexpected size — try again.")
                return False
        self._set_state(status="downloading")  # more files may follow
        return True


model_installer = ModelInstaller()
