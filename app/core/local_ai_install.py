"""Installing Ollama, from inside JARVIS, with the user's consent.

**This reverses an earlier rule, deliberately and on the owner's
instruction.** Until now `app/core/local_ai.py` refused to install
anything and told the user to go and do it themselves. That was the
right default for an assistant nobody had asked, and the wrong one for a
product whose owner has now decided that a person who wants local AI
should be able to get it from a button. `CLAUDE.md`, `docs/THREAT_MODEL.md`
and the tests that enforced the old rule are updated to match.

What has *not* changed is that nothing happens without being asked.
`plan()` describes exactly what would be downloaded, from where, how
large it is, whose software it is and what it will do — and returns
without touching anything. Only `start()` acts, and only a person can
call it.

**Running a downloaded executable is the most dangerous thing in this
product, so it is the most checked.** Before the installer is executed:

  * it came from Ollama's own HTTPS download URL, and every redirect is
    checked against `ALLOWED_SOURCES` *before* it is followed — not
    after the chain has finished, which is what lets a detour through
    somebody else's host go unnoticed;
  * its Authenticode signature is verified against Windows' own trust
    store, and the signing certificate must name Ollama
    (`app/core/authenticode.py`);
  * its SHA-256 is computed and shown, so the file can be checked
    independently.

A failure at any of those points deletes the file and stops. There is no
"continue anyway".

**Ollama's own installer runs visibly**, not silently. The user sees
whose software is being installed, agrees to Ollama's own prompts, and
Windows shows its own elevation dialog. A silent install of somebody
else's software is exactly the thing this product should not do, and it
would also depend on installer flags JARVIS cannot verify from here.

**JARVIS does not take ownership of Ollama.** An Ollama that was already
on the machine is used as it is and never touched. When JARVIS is the
one that installed it, that fact is recorded so the uninstaller can ask
about it — see `installed_by_jarvis()`.
"""

import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from app.core import safe_fetch
from app.logging_config import get_logger

logger = get_logger("core.local_ai_install")

# Ollama's official Windows download. Host-pinned: a redirect anywhere
# else is refused rather than followed, because "we downloaded an .exe
# from wherever that URL ended up pointing" is not a security story.
INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"

# Every host the download is permitted to touch, and — where the host is
# a shared namespace anybody can publish under — the path prefix as well.
#
# `github.com` on its own would allow a redirect to any repository on
# GitHub, which is not a pin at all: it is "somewhere on a site with
# millions of publishers". Ollama's releases live under one path, so
# that is what is allowed. The two githubusercontent hosts are GitHub's
# own opaque asset CDNs, whose paths carry signed, expiring tokens
# rather than a stable namespace, so they are pinned by host alone.
ALLOWED_SOURCES = (
    ("ollama.com", ""),
    ("www.ollama.com", ""),
    ("github.com", "/ollama/ollama/"),
    ("objects.githubusercontent.com", ""),
    ("release-assets.githubusercontent.com", ""),
)

MAX_REDIRECTS = safe_fetch.MAX_REDIRECTS

# The name that must appear in the signing certificate.
EXPECTED_PUBLISHER = "Ollama"

# What the consent screen says before anything is fetched. The size is
# approximate and labelled as such — it is Ollama's file, and it changes
# with their releases.
APPROXIMATE_SIZE = "around 700 MB"
PUBLISHER_URL = "https://ollama.com"
LICENCE = "MIT"
LICENCE_URL = "https://github.com/ollama/ollama/blob/main/LICENSE"

DOWNLOAD_CHUNK_BYTES = 256 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 120.0

# How long to wait for the person to finish Ollama's installer. Long,
# because it includes reading a dialog and a UAC prompt, and a timeout
# here does not cancel their install — it only stops JARVIS waiting.
INSTALL_TIMEOUT_SECONDS = 900.0

_INSTALLED_BY_JARVIS_KEY = "ollama_installed_by_jarvis"

# The states this reports, in the order they occur.
IDLE = "idle"
DOWNLOADING = "downloading"
VERIFYING = "verifying"
INSTALLING = "installing"
STARTING = "starting"
COMPLETE = "complete"
ERROR = "error"
CANCELLED = "cancelled"


@dataclass(frozen=True)
class InstallPlan:
    """Everything a person needs to decide, before anything is fetched."""

    url: str = INSTALLER_URL
    host: str = "ollama.com"
    publisher: str = "Ollama"
    publisher_url: str = PUBLISHER_URL
    licence: str = LICENCE
    licence_url: str = LICENCE_URL
    approximate_size: str = APPROXIMATE_SIZE
    verification: str = (
        "JARVIS checks the downloaded file's Windows code signature against Ollama "
        "before running it, and shows its SHA-256."
    )
    installs: str = (
        "Ollama's own installer runs and you approve it yourself. Ollama is separate "
        "software owned by its own publisher; JARVIS only starts it."
    )
    already_installed: bool = False


@dataclass
class InstallState:
    status: str = IDLE
    message: str = ""
    bytes_downloaded: int = 0
    bytes_total: int = 0
    sha256: str = ""
    signer: str = ""

    @property
    def percent(self) -> int:
        if self.bytes_total <= 0:
            return 0
        return min(100, int(self.bytes_downloaded * 100 / self.bytes_total))

    @property
    def running(self) -> bool:
        return self.status in (DOWNLOADING, VERIFYING, INSTALLING, STARTING)


def plan() -> InstallPlan:
    """What would happen. Touches nothing."""
    from app.core import local_ai

    return InstallPlan(already_installed=local_ai.is_installed())


def installed_by_jarvis() -> bool:
    """Whether *this* product put Ollama on the machine.

    Recorded so the uninstaller can tell the two cases apart. Removing
    software somebody else installed, because JARVIS happened to be
    uninstalled, would be the wrong answer every time.
    """
    from app.core.preferences import get_bool

    return bool(get_bool(_INSTALLED_BY_JARVIS_KEY))


def _remember_we_installed_it() -> None:
    from app.core.preferences import store

    store(_INSTALLED_BY_JARVIS_KEY, "true")


def _host_is_allowed(url: str) -> bool:
    """Whether JARVIS may send a request to *url* at all."""
    return safe_fetch.is_allowed(url, ALLOWED_SOURCES)


class OllamaInstaller:
    """One install at a time, on a background thread, cancellable.

    The same shape as every other long job in this codebase: a polled
    state object, a cancel flag checked between chunks, and no work that
    can outlive the request that started it without being visible.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = InstallState()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def state(self) -> InstallState:
        with self._lock:
            return InstallState(**vars(self._state))

    def _set(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self._state, key, value)

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def cancel(self) -> None:
        self._cancel.set()

    def start(self) -> bool:
        """Begin. False if one is already running, or if this is not
        Windows — there is no Ollama installer to run anywhere else, and
        pretending otherwise would fail halfway through."""
        if sys.platform != "win32":
            self._set(
                status=ERROR,
                message="Ollama can only be installed by JARVIS on Windows.",
            )
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel.clear()
            self._state = InstallState(status=DOWNLOADING, message="Contacting ollama.com…")
            thread = threading.Thread(target=self._run, daemon=True, name="jarvis-ollama-install")
            self._thread = thread
        thread.start()
        return True

    # --- the work ---

    def _cancelled(self) -> bool:
        if self._cancel.is_set():
            self._set(status=CANCELLED, message="Cancelled. Nothing was installed.")
            return True
        return False

    def _run(self) -> None:
        staging = Path(tempfile.mkdtemp(prefix="jarvis_ollama_"))
        installer = staging / "OllamaSetup.exe"
        try:
            if not self._download(installer):
                return
            if not self._verify(installer):
                return
            if not self._execute(installer):
                return
            self._start_and_confirm()
        except Exception as exc:  # noqa: BLE001 — a background thread must never die silently
            logger.warning("Ollama installation failed.", exc_info=True)
            self._set(status=ERROR, message=f"Local AI setup could not finish: {exc}")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _download(self, target: Path) -> bool:
        self._set(status=DOWNLOADING, message="Downloading Ollama's installer…")
        try:
            # safe_fetch follows redirects by hand and checks every hop
            # *before* contacting it. httpx's own follow_redirects=True
            # cannot: it walks the whole chain and reports only where it
            # ended up, so a chain that detours through somebody else's
            # host and returns to an allowed one passed a check on the
            # final URL. Reproduced here before it was fixed.
            with safe_fetch.stream(
                INSTALLER_URL, ALLOWED_SOURCES, DOWNLOAD_TIMEOUT_SECONDS
            ) as response:
                total = int(response.headers.get("content-length") or 0)
                self._set(bytes_total=total)
                downloaded = 0
                with open(target, "wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=DOWNLOAD_CHUNK_BYTES):
                        if self._cancelled():
                            return False
                        handle.write(chunk)
                        downloaded += len(chunk)
                        self._set(bytes_downloaded=downloaded)
        except safe_fetch.UntrustedRedirect as exc:
            self._set(status=ERROR, message=exc.message)
            return False
        except httpx.HTTPError as exc:
            self._set(
                status=ERROR,
                message=(
                    "Ollama's installer could not be downloaded — this is usually a "
                    f"connection problem rather than a fault with JARVIS ({exc})."
                ),
            )
            return False
        return not self._cancelled()

    def _verify(self, installer: Path) -> bool:
        from app.core import authenticode

        self._set(status=VERIFYING, message="Checking who signed the installer…")
        digest = authenticode.sha256(installer)
        self._set(sha256=digest or "")

        verdict = authenticode.verify(installer, expected_publisher=EXPECTED_PUBLISHER)
        self._set(signer=verdict.signer)
        if not verdict.trusted:
            logger.warning("Refusing to run an unverified installer: %s", verdict.detail)
            try:
                installer.unlink()
            except OSError:
                pass
            self._set(
                status=ERROR,
                message=(
                    f"JARVIS did not run the download because it could not confirm it came "
                    f"from Ollama. {verdict.detail} The file was deleted."
                ),
            )
            return False
        return not self._cancelled()

    def _execute(self, installer: Path) -> bool:
        """Run Ollama's installer visibly and wait for it to finish.

        Not silent, on purpose: the person sees whose software this is,
        approves Windows' own elevation prompt, and can stop. An explicit
        argument list, never a shell string.
        """
        self._set(
            status=INSTALLING,
            message=(
                "Ollama's installer is now open. Follow its prompts — Windows will ask "
                "you to approve it."
            ),
        )
        try:
            completed = subprocess.run(
                [str(installer)],
                timeout=INSTALL_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._set(
                status=ERROR,
                message=(
                    "JARVIS stopped waiting for Ollama's installer. If you finished it, "
                    "press Re-check; if it is still open, finish it and then press Re-check."
                ),
            )
            return False
        except OSError as exc:
            self._set(status=ERROR, message=f"Ollama's installer could not be started: {exc}")
            return False

        if completed.returncode != 0:
            self._set(
                status=ERROR,
                message=(
                    "Ollama's installer did not complete (it reported code "
                    f"{completed.returncode}). Nothing else was changed."
                ),
            )
            return False
        return True

    def _start_and_confirm(self) -> None:
        """Installed is not running, and running is not ready."""
        from app.core import local_ai

        self._set(status=STARTING, message="Starting Ollama…")

        # A per-user install does not appear on this already-running
        # process's PATH, so where it landed has to be looked for rather
        # than assumed to be resolvable.
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and not local_ai.is_installed():
            if self._cancelled():
                return
            time.sleep(1.0)

        if not local_ai.is_installed():
            self._set(
                status=ERROR,
                message=(
                    "Ollama's installer finished but JARVIS cannot find it yet. Signing "
                    "out and back in, or restarting JARVIS, usually makes it visible."
                ),
            )
            return

        _remember_we_installed_it()
        local_ai.start_ollama()
        if local_ai.wait_until_answering():
            self._set(status=COMPLETE, message="Ollama is installed and running.")
        else:
            self._set(
                status=COMPLETE,
                message=(
                    "Ollama is installed. It has not answered yet — a first start can "
                    "take a moment. Press Re-check."
                ),
            )


# Module-level singleton, matching every other installer in this codebase.
ollama_installer = OllamaInstaller()
