"""Local AI, explained in terms a person can act on.

The reported defect was "Local AI mode does not work", and the honest
diagnosis was that JARVIS could only tell you *that* it did not work. Two
completely different situations — Ollama not installed at all, and Ollama
installed but not currently running — produced the same sentence, and the
fix it offered was a shell command.

**What this module will not do, deliberately.** It never downloads a
model and never installs the Ollama runtime. That is `CLAUDE.md`'s rule,
it is enforced by a test that walks the AST of every module under `app/`,
and `docs/THREAT_MODEL.md` explains why: a multi-gigabyte download
started on someone's behalf is exactly the kind of large, slow,
irreversible thing a local-first assistant should not do without being
asked. The defect report asked for automatic installation; that request
and this rule are in direct conflict, and the rule wins until its owner
says otherwise.

**What it does instead**, which covers most of the same ground:

  * Tells the four states apart — not installed, installed but not
    running, running with nothing installed, ready — because each has a
    different next step and only one of them is a problem.
  * **Starts Ollama when it is already installed.** Launching a program
    the user chose to install is not downloading one, and "it is
    installed but not running" is otherwise a dead end for someone who
    does not know it is a background service.
  * Recommends a model that suits *this* machine, computed from its
    actual memory rather than a fixed suggestion.
  * Reports Ready only after real generated text has come back, so the
    word means something.
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

# The states, in the order a user passes through them.
NOT_INSTALLED = "not_installed"
INSTALLED_NOT_RUNNING = "installed_not_running"
RUNNING_NO_MODELS = "running_no_models"
READY = "ready"

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

    None rather than a guess: a recommendation built on a made-up number
    is worse than saying the machine could not be measured.
    """
    try:
        import psutil

        return psutil.virtual_memory().total / (1024 ** 3)
    except Exception:  # noqa: BLE001
        logger.debug("Could not read total system memory.", exc_info=True)
        return None


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

    @property
    def usable(self) -> bool:
        return self.status == READY


def describe(http_client=None) -> LocalAIState:
    """The current state of local AI on this machine.

    Computed on every call rather than cached: Ollama can be installed,
    started or stopped while JARVIS is running, and a remembered answer
    would be wrong exactly when somebody is trying to work out why it
    stopped working.
    """
    from app.core.providers import ollama_status, selected_ollama_model

    memory = total_memory_gb()
    suggestion = recommend_model(memory)
    installed = is_installed()
    status = ollama_status(http_client=http_client)

    common = {
        "models": list(status.models),
        "selected_model": selected_ollama_model(),
        "recommended_model": suggestion.name,
        "recommended_download": suggestion.approximate_download,
        "recommended_why": suggestion.why,
        "memory_gb": round(memory, 1) if memory is not None else None,
        "installed": installed,
        "download_url": OLLAMA_DOWNLOAD_URL,
    }

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
                "installed. JARVIS does not download models for you — that is "
                "deliberate, so nothing multi-gigabyte ever starts without you "
                "asking for it."
            ),
            next_step=(
                f"In Ollama, install {suggestion.name}. {suggestion.why}"
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
            "internet connection once it is set up. It needs a free program "
            "called Ollama, which JARVIS does not install for you."
        ),
        next_step=(
            f"Install Ollama, then add the {suggestion.name} model "
            f"({suggestion.approximate_download}). {suggestion.why}"
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
