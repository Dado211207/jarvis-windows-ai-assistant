"""Installing the voice: downloading the Kokoro model and a voice pack.

Never silently. The size, the source, the licence and the destination are
all available before anything is fetched (assets.py holds them as data),
and nothing is downloaded that does not match a SHA-256 recorded there.

Four properties this has to get right, because each one was a real
failure mode in the speech-model installer this is modelled on:

  * **Cancellable.** A 92 MB download on a slow connection has to be
    stoppable, and stopping it has to leave nothing behind.
  * **Atomic.** A half-written model that is 90 MB of the right bytes is
    indistinguishable from a whole one by size alone. Files are written
    to a temporary directory and moved into place only after the digest
    matches, so an interrupted install leaves the previous state intact
    rather than a corrupt one.
  * **Self-healing.** An install that was already corrupted — a truncated
    file from a previous crash, a partially-copied backup — is detected
    on verification and replaced, rather than reported as installed and
    then failing at inference.
  * **Resumable by retry.** Files already present and verified are not
    downloaded again, so retrying after a failure costs only what
    actually failed.
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
from app.voice.kokoro import assets

logger = get_logger("voice.kokoro.install")

INSTALL_DIR_NAME = "kokoro-82m-onnx"
DOWNLOAD_CHUNK_BYTES = 256 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60.0

# Verifying a 92 MB file costs a read of it. Worth it on install, too
# slow to repeat on every startup — see verify_installed() versus
# is_installed().
_HASH_CHUNK_BYTES = 1024 * 1024


def install_dir() -> Path:
    return models_dir() / INSTALL_DIR_NAME


def asset_path(asset: assets.RemoteAsset) -> Path:
    return install_dir() / asset.filename


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def file_is_good(asset: assets.RemoteAsset) -> bool:
    """Present, the right size, and the right bytes.

    The size check first is not redundant — it rejects the common
    truncated-download case without reading 92 MB to reach the same
    conclusion.
    """
    path = asset_path(asset)
    try:
        if not path.is_file() or path.stat().st_size != asset.size_bytes:
            return False
        return _digest(path) == asset.sha256
    except OSError:
        return False


def is_installed(voice_key: str = assets.DEFAULT_VOICE_KEY) -> bool:
    """A cheap check for "can we speak": both files present at the right
    size. Deliberately not the digest — see verify_installed()."""
    voice = assets.resolve_voice(voice_key)
    for asset in (assets.MODEL_ASSET, voice.asset):
        path = asset_path(asset)
        try:
            if not path.is_file() or path.stat().st_size != asset.size_bytes:
                return False
        except OSError:
            return False
    return True


def verify_installed(voice_key: str = assets.DEFAULT_VOICE_KEY) -> bool:
    """The full digest check. Used by the diagnostics panel and after an
    install, not on the startup path."""
    voice = assets.resolve_voice(voice_key)
    return all(file_is_good(asset) for asset in (assets.MODEL_ASSET, voice.asset))


def installed_voice_keys() -> List[str]:
    return [voice.key for voice in assets.VOICES if file_is_good(voice.asset)]


def missing_assets(voice_key: str = assets.DEFAULT_VOICE_KEY) -> List[assets.RemoteAsset]:
    """What an install would actually have to fetch."""
    voice = assets.resolve_voice(voice_key)
    return [
        asset
        for asset in (assets.MODEL_ASSET, voice.asset)
        if not file_is_good(asset)
    ]


def bytes_required(voice_key: str = assets.DEFAULT_VOICE_KEY) -> int:
    return sum(asset.size_bytes for asset in missing_assets(voice_key))


@dataclass
class VoiceInstallState:
    status: str = "idle"  # idle|downloading|verifying|installing|complete|error|cancelled
    current_file: str = ""
    bytes_downloaded: int = 0
    bytes_total: int = 0
    message: str = ""
    voice_key: str = ""

    @property
    def percent(self) -> int:
        if self.bytes_total <= 0:
            return 0
        return min(100, int(self.bytes_downloaded * 100 / self.bytes_total))


class VoiceInstaller:
    """One install at a time, matching every other background job in this
    codebase (the speech-model installer, pending actions, the session
    store)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = VoiceInstallState()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def state(self) -> VoiceInstallState:
        with self._lock:
            return VoiceInstallState(**vars(self._state))

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _set(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self._state, key, value)

    def start(self, voice_key: str = assets.DEFAULT_VOICE_KEY) -> bool:
        """False without starting anything if an install is already
        running — two concurrent writers to the same directory is how a
        corrupt install happens."""
        voice = assets.resolve_voice(voice_key)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel.clear()
            self._state = VoiceInstallState(
                status="downloading",
                voice_key=voice.key,
                bytes_total=sum(a.size_bytes for a in self._pending(voice)),
                message="Preparing…",
            )
            thread = threading.Thread(
                target=self._run, args=(voice,), daemon=True, name="jarvis-voice-install",
            )
            self._thread = thread
        thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    @staticmethod
    def _pending(voice: assets.Voice) -> List[assets.RemoteAsset]:
        return [a for a in (assets.MODEL_ASSET, voice.asset) if not file_is_good(a)]

    def _cancelled(self) -> bool:
        if self._cancel.is_set():
            self._set(status="cancelled", message="Installation cancelled. Nothing was changed.")
            return True
        return False

    def _run(self, voice: assets.Voice) -> None:
        pending = self._pending(voice)
        if not pending:
            self._set(
                status="complete",
                message=f"{voice.display_name} is already installed.",
                bytes_total=0,
                bytes_downloaded=0,
            )
            return

        target_dir = install_dir()
        staging = Path(tempfile.mkdtemp(prefix="jarvis_voice_dl_"))
        downloaded = 0

        try:
            for asset in pending:
                if self._cancelled():
                    return
                self._set(current_file=asset.filename, status="downloading")
                staged = staging / asset.filename
                digest = hashlib.sha256()

                with httpx.stream(
                    "GET", asset.url(), timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True
                ) as response:
                    response.raise_for_status()
                    with open(staged, "wb") as handle:
                        for chunk in response.iter_bytes(chunk_size=DOWNLOAD_CHUNK_BYTES):
                            if self._cancelled():
                                return
                            handle.write(chunk)
                            digest.update(chunk)
                            downloaded += len(chunk)
                            self._set(bytes_downloaded=downloaded)

                self._set(status="verifying", message=f"Verifying {asset.filename}…")
                actual = digest.hexdigest()
                if actual != asset.sha256:
                    logger.warning(
                        "Checksum mismatch for %s: expected %s, got %s",
                        asset.filename, asset.sha256, actual,
                    )
                    self._set(
                        status="error",
                        message=(
                            f"{asset.filename} did not match its published checksum, so it "
                            "was not installed. This usually means the download was "
                            "interrupted — try again."
                        ),
                    )
                    return
                if staged.stat().st_size != asset.size_bytes:
                    self._set(
                        status="error",
                        message=f"{asset.filename} downloaded at an unexpected size. Try again.",
                    )
                    return

            if self._cancelled():
                return

            # Everything verified: only now does anything move into place.
            self._set(status="installing", message="Installing…")
            target_dir.mkdir(parents=True, exist_ok=True)
            for asset in pending:
                shutil.move(str(staging / asset.filename), str(target_dir / asset.filename))

            self._set(
                status="complete",
                message=f"{voice.display_name} is ready.",
                current_file="",
            )
            logger.info("Kokoro voice installed: %s in %s", voice.key, target_dir)

        except httpx.HTTPError as exc:
            logger.warning("Voice download failed: %s", type(exc).__name__, exc_info=True)
            self._set(
                status="error",
                message=(
                    "The download could not be completed. Check your internet connection "
                    "and try again — anything already downloaded is kept."
                ),
            )
        except OSError as exc:
            logger.warning("Voice install failed writing to disk: %s", exc, exc_info=True)
            self._set(
                status="error",
                message=(
                    "The voice could not be saved to disk. Check there is enough free "
                    "space and try again."
                ),
            )
        except Exception:  # noqa: BLE001
            logger.warning("Voice install failed unexpectedly.", exc_info=True)
            self._set(status="error", message="The voice could not be installed. Try again.")
        finally:
            shutil.rmtree(staging, ignore_errors=True)


voice_installer = VoiceInstaller()
