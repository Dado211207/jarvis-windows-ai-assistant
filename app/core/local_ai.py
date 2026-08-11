"""Local AI, explained in terms a person can act on.

The reported defect was "Local AI mode does not work", and the honest
diagnosis was that JARVIS could only tell you *that* it did not work. Two
completely different situations — Ollama not installed at all, and Ollama
installed but not currently running — produced the same sentence, and the
fix it offered was a shell command.

**This module used to refuse to install anything.** It said so at
length, `CLAUDE.md` carried it as a non-negotiable rule, and a test
walked the AST of every module under `app/` to enforce it. The owner has
since decided the opposite: a person who wants local AI should be able
to get it from a button rather than a set of instructions. That decision
is implemented in `app/core/local_ai_install.py` (the runtime) and
`app/core/local_ai_models.py` (the model), and the rule, the threat
model and the tests are updated rather than quietly worked around.

**What has not changed is that nothing happens unasked.** Every download
is preceded by a screen naming the source, the publisher, the licence,
the size and this machine's free space, and starts only when a person
presses the button. Nothing is fetched in the background, on startup, or
as a side effect of anything else.

**What this module owns** is the answer to "where is local AI up to?":

  * The ten states, told apart because each has a different next step —
    not installed, detected, installing, stopped, starting, a model
    required, downloading it, verifying it, ready, and failed.
  * **Starting Ollama when it is installed.** "Installed but not
    running" is otherwise a dead end for someone who does not know it is
    a background service.
  * A model recommendation computed from this machine's real memory and
    free disk (`app/core/machine.py`), not a fixed suggestion.
  * Ready meaning real generated text has come back, so the word means
    something.

**Anthropic chat never depends on any of this.** Local AI failing, being
skipped, or never being set up leaves the rest of the product exactly as
it was.
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from app.logging_config import get_logger

logger = get_logger("core.local_ai")

OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"

# The ten states, in the order a user passes through them. Four of them
# are transient — they exist because a five-minute download with no
# visible state is indistinguishable from a hang.
NOT_INSTALLED = "not_installed"          # 1. nothing here yet
DETECTED = "detected"                    # 2. Ollama found, still being checked
INSTALLING = "installing"                # 3. its installer is running
INSTALLED_NOT_RUNNING = "installed_not_running"   # 4. runtime stopped
STARTING = "starting"                    # 5. being started
RUNNING_NO_MODELS = "running_no_models"  # 6. a model is required
DOWNLOADING_MODEL = "downloading_model"  # 7. pulling one
VERIFYING = "verifying"                  # 8. checking it is intact / that it answers
READY = "ready"                          # 9. proven by real generated text
FAILED = "failed"                        # 10. something went wrong, with the reason

ALL_STATES = (
    NOT_INSTALLED, DETECTED, INSTALLING, INSTALLED_NOT_RUNNING, STARTING,
    RUNNING_NO_MODELS, DOWNLOADING_MODEL, VERIFYING, READY, FAILED,
)

# How long to wait for a freshly started Ollama to answer.
START_TIMEOUT_SECONDS = 20.0
START_POLL_SECONDS = 0.5

# A prompt short enough to be nearly free and specific enough that an
# empty or echoed answer is obvious.
VERIFY_PROMPT = "Reply with the single word: ready"
VERIFY_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class ModelSuggestion:
    """A model that fits this machine.

    `approximate_download` is deliberately vague in wording as well as
    value: it is a guide for deciding, not a figure to verify a download
    against. Nothing in JARVIS ever checks it, because JARVIS never
    performs the download.
    """

    name: str
    approximate_download: str
    why: str


# Ordered from smallest requirement upward; the first entry whose
# `minimum_gb` the machine meets, scanning from the largest down, wins.
_MODELS_BY_MEMORY = (
    (32, ModelSuggestion(
        "llama3.1:8b", "around 5 GB",
        "This machine has plenty of memory, so the larger model is comfortable and gives the best answers.",
    )),
    (16, ModelSuggestion(
        "llama3.1:8b", "around 5 GB",
        "Suits this machine's memory well — the best balance of quality and speed here.",
    )),
    (8, ModelSuggestion(
        "llama3.2:3b", "around 2 GB",
        "A smaller model, chosen so it runs comfortably in this machine's memory.",
    )),
    (0, ModelSuggestion(
        "llama3.2:1b", "around 1.5 GB",
        "The smallest model, chosen because this machine has limited memory to spare.",
    )),
)


def total_memory_gb() -> Optional[float]:
    """Physical memory in GB, or None if it cannot be determined.

    Delegates to app/core/machine.py so there is one place that reads
    this machine's hardware. Two readers eventually disagree, and the one
    that decides which model to recommend disagreeing with the one shown
    on the consent screen would be a particularly confusing way to fail.

    None rather than a guess: a recommendation built on a made-up number
    is worse than saying the machine could not be measured.
    """
    from app.core import machine

    return machine.memory_gb()


def recommend_model(memory_gb: Optional[float] = None) -> ModelSuggestion:
    """The model this machine should run.

    Falls back to the smallest option when memory is unknown, because
    being conservative costs a little quality and being wrong the other
    way costs a machine that swaps itself to a standstill.
    """
    if memory_gb is None:
        memory_gb = total_memory_gb()
    if memory_gb is None:
        return _MODELS_BY_MEMORY[-1][1]

    for minimum_gb, suggestion in _MODELS_BY_MEMORY:
        if memory_gb >= minimum_gb:
            return suggestion
    return _MODELS_BY_MEMORY[-1][1]


def find_ollama_executable() -> Optional[Path]:
    """Where Ollama is installed, or None.

    Checked against real locations rather than assumed: Ollama's own
    Windows installer is per-user and does not always put itself on PATH
    for an already-running process, which is why "on PATH" alone is not
    the question.
    """
    found = shutil.which("ollama")
    if found:
        return Path(found)

    if sys.platform != "win32":
        return None

    candidates = []
    for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        root = os.environ.get(variable)
        if not root:
            continue
        candidates.append(Path(root) / "Programs" / "Ollama" / "ollama.exe")
        candidates.append(Path(root) / "Ollama" / "ollama.exe")

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def is_installed() -> bool:
    return find_ollama_executable() is not None


def start_ollama() -> bool:
    """Start an already-installed Ollama in the background.

    Not an install and not a download — this launches a program the user
    chose to put on their machine. Without it, "installed but not
    running" is a dead end for anyone who does not know Ollama is a
    background service they are expected to start themselves.

    An explicit argument list, never a shell string.
    """
    executable = find_ollama_executable()
    if executable is None:
        return False

    creation_flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    try:
        subprocess.Popen(
            [str(executable), "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except Exception:  # noqa: BLE001
        logger.warning("Could not start Ollama.", exc_info=True)
        return False

    logger.info("Started Ollama from %s", executable)
    return True


def wait_until_answering(timeout_seconds: float = START_TIMEOUT_SECONDS) -> bool:
    """Poll until the local server responds, or give up."""
    import time

    from app.core.providers import ollama_status

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = ollama_status()
        # Available, or running-but-empty: both mean the server answered.
        if status.available or "no models" in status.detail.lower():
            return True
        time.sleep(START_POLL_SECONDS)
    return False


@dataclass
class LocalAIState:
    """Everything the Local AI panel shows, computed fresh."""

    status: str
    headline: str
    detail: str
    next_step: str
    models: List[str] = field(default_factory=list)
    selected_model: str = ""
    recommended_model: str = ""
    recommended_download: str = ""
    recommended_why: str = ""
    memory_gb: Optional[float] = None
    installed: bool = False
    can_start: bool = False
    download_url: str = OLLAMA_DOWNLOAD_URL
    verified: bool = False
    # What the machine has, so the consent screen shows a person what
    # they are spending rather than only what they are getting.
    hardware: str = ""
    free_disk_gb: Optional[float] = None
    # Progress for the two transient states, 0 when neither is running.
    percent: int = 0
    # Whether JARVIS put Ollama here. Recorded for the uninstaller, which
    # must never remove software somebody else installed.
    installed_by_jarvis: bool = False

    @property
    def usable(self) -> bool:
        return self.status == READY

    @property
    def busy(self) -> bool:
        return self.status in (INSTALLING, STARTING, DOWNLOADING_MODEL, VERIFYING)


def _in_progress(common: dict) -> Optional[LocalAIState]:
    """The transient states, read from the two background jobs.

    Checked before anything is probed: while Ollama's installer is on
    screen, "not installed" is a true statement and a useless one.
    """
    from app.core import local_ai_install, local_ai_models

    install = local_ai_install.ollama_installer.state()
    if install.running:
        headline = {
            local_ai_install.DOWNLOADING: "Downloading Ollama…",
            local_ai_install.VERIFYING: "Checking the download is genuine…",
            local_ai_install.INSTALLING: "Installing Ollama…",
            local_ai_install.STARTING: "Starting Ollama…",
        }.get(install.status, "Setting up local AI…")
        return LocalAIState(
            status=STARTING if install.status == local_ai_install.STARTING else INSTALLING,
            headline=headline,
            detail=install.message,
            next_step="This can take a few minutes. You can carry on using JARVIS meanwhile.",
            percent=install.percent,
            can_start=False,
            **common,
        )
    if install.status == local_ai_install.ERROR:
        return LocalAIState(
            status=FAILED,
            headline="Local AI setup did not finish.",
            detail=install.message,
            next_step="Press Try again, or install Ollama yourself and press Re-check.",
            can_start=False,
            **common,
        )

    pull = local_ai_models.model_puller.state()
    if pull.running:
        return LocalAIState(
            status=DOWNLOADING_MODEL if pull.status == local_ai_models.DOWNLOADING else VERIFYING,
            headline=f"Downloading {pull.model}…" if pull.status == local_ai_models.DOWNLOADING
                     else f"Checking {pull.model} is intact…",
            detail=pull.message,
            next_step="You can cancel; what has already downloaded is kept.",
            percent=pull.percent,
            can_start=False,
            **common,
        )
    if pull.status == local_ai_models.ERROR:
        return LocalAIState(
            status=FAILED,
            headline="The model download did not finish.",
            detail=pull.message,
            next_step="Press Retry — it continues from what already downloaded.",
            can_start=False,
            **common,
        )
    return None


def describe(http_client=None) -> LocalAIState:
    """The current state of local AI on this machine.

    Computed on every call rather than cached: Ollama can be installed,
    started or stopped while JARVIS is running, and a remembered answer
    would be wrong exactly when somebody is trying to work out why it
    stopped working.
    """
    from app.core import machine as machine_info
    from app.core.local_ai_install import installed_by_jarvis
    from app.core.providers import ollama_status, selected_ollama_model

    hardware = machine_info.inspect()
    memory = hardware.memory_gb
    suggestion = recommend_model(memory)
    installed = is_installed()

    common = {
        "models": [],
        "selected_model": selected_ollama_model(),
        "recommended_model": suggestion.name,
        "recommended_download": suggestion.approximate_download,
        "recommended_why": suggestion.why,
        "memory_gb": round(memory, 1) if memory is not None else None,
        "installed": installed,
        "download_url": OLLAMA_DOWNLOAD_URL,
        "hardware": machine_info.describe(hardware),
        "free_disk_gb": round(hardware.free_disk_gb, 1) if hardware.free_disk_gb is not None else None,
        "installed_by_jarvis": installed_by_jarvis(),
    }

    in_progress = _in_progress(common)
    if in_progress is not None:
        return in_progress

    status = ollama_status(http_client=http_client)
    common["models"] = list(status.models)

    if status.available:
        return LocalAIState(
            status=READY,
            headline="Local AI is ready.",
            detail=(
                f"Ollama is running on this computer with {len(status.models)} "
                f"model{'s' if len(status.models) != 1 else ''} installed. "
                "Nothing you type in local mode leaves this machine."
            ),
            next_step="Choose a model and press Test to hear it answer for real.",
            can_start=False,
            **common,
        )

    if installed and "no models" in status.detail.lower():
        return LocalAIState(
            status=RUNNING_NO_MODELS,
            headline="Ollama is running, but has no models yet.",
            detail=(
                "A model is the part that actually does the thinking, and none is "
                f"installed. The recommended one for this computer is "
                f"{suggestion.name}, {suggestion.approximate_download}. "
                "Nothing downloads until you say so."
            ),
            next_step=(
                f"Press Download {suggestion.name}. {suggestion.why}"
            ),
            can_start=False,
            **common,
        )

    if installed:
        return LocalAIState(
            status=INSTALLED_NOT_RUNNING,
            headline="Ollama is installed, but not running.",
            detail=(
                "Ollama runs quietly in the background and is not started at the "
                "moment, so local AI has nothing to talk to."
                + (
                    ""
                    if common["installed_by_jarvis"]
                    else " It was already on this computer before JARVIS, and JARVIS "
                         "has not changed anything about it."
                )
            ),
            next_step="Press Start Ollama and JARVIS will launch it for you.",
            can_start=True,
            **common,
        )

    return LocalAIState(
        status=NOT_INSTALLED,
        headline="Local AI is not set up on this computer.",
        detail=(
            "Local AI runs entirely on your own machine, with no account and no "
            "internet connection once it is set up. It needs a free program called "
            "Ollama. JARVIS can download and install it for you, showing you the "
            "source, the publisher and the size first — nothing is fetched until you "
            "press the button."
        ),
        next_step=(
            f"Press Set up local AI. JARVIS installs Ollama, then downloads "
            f"{suggestion.name} ({suggestion.approximate_download}). {suggestion.why}"
        ),
        can_start=False,
        **common,
    )


@dataclass
class VerificationResult:
    """The answer to "does it actually work", not "is it plausibly set up"."""

    ok: bool
    message: str
    model: str = ""
    reply: str = ""


def verify_with_real_inference(model: str = "", http_client=None) -> VerificationResult:
    """Ask the local model a question and see whether it answers.

    This is what "Ready" is allowed to mean. Detecting a running server
    and a file on disk proves neither that the model loads nor that it
    generates, and both fail in ways that only appear on first use —
    which, without this, would be the first time the user tried to have
    a conversation.
    """
    from app.core.ai.base import Message, ProviderConfig
    from app.core.ai.ollama_provider import OllamaProvider
    from app.core.providers import ollama_status, selected_ollama_model

    status = ollama_status(http_client=http_client)
    if not status.available:
        return VerificationResult(ok=False, message=status.detail)

    chosen = (model or selected_ollama_model() or "").strip()
    if chosen and chosen not in status.models:
        return VerificationResult(
            ok=False,
            message=(
                f"“{chosen}” is not installed in Ollama. Installed: "
                + ", ".join(status.models)
            ),
        )
    if not chosen:
        chosen = status.models[0]

    try:
        provider = OllamaProvider(
            ProviderConfig(ollama_model=chosen, timeout_seconds=VERIFY_TIMEOUT_SECONDS)
        )
        reply = provider.generate([Message(role="user", content=VERIFY_PROMPT)], system="")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Local AI verification failed.", exc_info=True)
        return VerificationResult(
            ok=False,
            model=chosen,
            message=_verification_failure_message(exc),
        )

    text = (getattr(reply, "content", "") or "").strip()
    if not text:
        return VerificationResult(
            ok=False,
            model=chosen,
            message=(
                f"“{chosen}” is installed and running but produced no answer. "
                "Reinstalling that model in Ollama usually fixes it."
            ),
        )

    return VerificationResult(
        ok=True,
        model=chosen,
        reply=text[:200],
        message=f"“{chosen}” answered. Local AI is working on this computer.",
    )


def _verification_failure_message(error: Exception) -> str:
    """Name the failure rather than reporting that one happened.

    A provider raises a ProviderError carrying an ErrorCategory precisely
    so this can distinguish a timeout from an unreachable server; falling
    back to the exception text is the last resort, not the plan.
    """
    category = getattr(error, "category", None)
    name = getattr(category, "value", None) or getattr(category, "name", None) or ""
    lowered = str(name).lower()

    if "timeout" in lowered:
        return (
            "The model did not answer in time. The first answer after installing a "
            "model is usually the slowest — try once more."
        )
    if "unreachable" in lowered or "connection" in lowered:
        return "Ollama stopped responding partway through. Start it again and retry."
    return f"Local AI could not complete a test answer: {error}"
