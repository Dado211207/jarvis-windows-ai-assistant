"""What this computer actually has, for deciding what to put on it.

Local AI setup asks a user to spend several gigabytes of disk and a
noticeable amount of memory. Recommending a model without looking at the
machine first is how somebody with 8 GB of RAM ends up being told to
install a model that will make their computer unusable, and how somebody
with 4 GB of free disk starts a 5 GB download that fails at the end.

Everything here is **read-only and local**. No process is inspected, no
usage is sampled over time, nothing is stored — CLAUDE.md's Safety rules
forbid the monitoring that would be, and this is a snapshot taken when
somebody presses a button.

Every reading is optional and reports `None` when it cannot be taken.
A `None` is shown to the user as "could not be determined", which is
honest; a guess dressed as a measurement is not.
"""

import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from app.logging_config import get_logger

logger = get_logger("core.machine")

# Where Ollama keeps its models, and therefore the volume whose free
# space actually matters. Ollama's own default on every platform.
_OLLAMA_MODELS_ENV = "OLLAMA_MODELS"

# Adapters whose names mean "no dedicated graphics card", so that
# "a GPU was detected" never gets reported for a basic display adapter.
_NOT_A_REAL_GPU = ("microsoft basic display", "remote display", "virtual display")


@dataclass(frozen=True)
class Machine:
    """A snapshot of the hardware relevant to running a model locally."""

    memory_gb: Optional[float]
    cpu_cores: Optional[int]
    cpu_name: str
    gpus: List[str]
    free_disk_gb: Optional[float]
    models_path: str

    @property
    def has_dedicated_gpu(self) -> bool:
        return bool(self.gpus)

    def can_fit(self, download_gb: float) -> Optional[bool]:
        """Whether *download_gb* plus a working margin fits on disk.

        None when free space could not be read — the caller must not turn
        an unknown into a "yes, plenty of room".
        """
        if self.free_disk_gb is None:
            return None
        return self.free_disk_gb >= download_gb + DISK_HEADROOM_GB


# Models are unpacked and Ollama keeps working files alongside them, so
# "exactly enough space for the download" is not enough space.
DISK_HEADROOM_GB = 2.0


def memory_gb() -> Optional[float]:
    try:
        import psutil

        return psutil.virtual_memory().total / (1024 ** 3)
    except Exception:  # noqa: BLE001
        logger.debug("Could not read total system memory.", exc_info=True)
        return None


def cpu_cores() -> Optional[int]:
    try:
        return os.cpu_count()
    except Exception:  # noqa: BLE001
        return None


def cpu_name() -> str:
    """A readable processor name, or "" — never a fabricated one."""
    try:
        name = platform.processor() or ""
        if not name.strip():
            name = os.environ.get("PROCESSOR_IDENTIFIER", "")
        return name.strip()
    except Exception:  # noqa: BLE001
        return ""


def models_dir() -> Path:
    """Where Ollama will put models on this machine.

    Read from Ollama's own environment variable when it is set, because
    a user who has moved their models to another drive has moved the
    volume whose free space this needs to measure.
    """
    configured = os.environ.get(_OLLAMA_MODELS_ENV, "").strip()
    if configured:
        return Path(configured)
    return Path.home() / ".ollama" / "models"


def free_disk_gb(path: Optional[Path] = None) -> Optional[float]:
    """Free space on the volume that would hold the model.

    Walks up to the nearest directory that exists: the models directory
    itself does not exist before Ollama's first run, and asking about a
    path that is not there yet would report nothing rather than the
    volume it is going to be created on.
    """
    candidate = (path or models_dir()).resolve()
    for parent in [candidate, *candidate.parents]:
        try:
            if parent.exists():
                return shutil.disk_usage(parent).free / (1024 ** 3)
        except OSError:
            continue
    logger.debug("Could not read free disk space for %s", candidate)
    return None


def gpus() -> List[str]:
    """Dedicated display adapters, by name.

    Read from the Windows driver registry rather than by running a
    command: no subprocess, no shell, nothing that could be handed a
    string. Returns [] anywhere that cannot be read, including every
    non-Windows machine, which is honest — this reports what it found,
    not what is there.
    """
    if sys.platform != "win32":
        return []

    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows only
        return []

    key_path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    found: List[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as root:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                if not subkey_name.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, subkey_name) as subkey:
                        description, _ = winreg.QueryValueEx(subkey, "DriverDesc")
                except OSError:
                    continue
                name = str(description).strip()
                if not name or any(marker in name.lower() for marker in _NOT_A_REAL_GPU):
                    continue
                if name not in found:
                    found.append(name)
    except OSError:
        logger.debug("Could not enumerate display adapters.", exc_info=True)
        return []
    return found


def inspect() -> Machine:
    """One snapshot. Never raises."""
    path = models_dir()
    return Machine(
        memory_gb=memory_gb(),
        cpu_cores=cpu_cores(),
        cpu_name=cpu_name(),
        gpus=gpus(),
        free_disk_gb=free_disk_gb(path),
        models_path=str(path),
    )


def describe(machine: Optional[Machine] = None) -> str:
    """The snapshot in one readable line, for the consent screen.

    Anything that could not be read says so. "8 GB of memory, graphics
    could not be determined" is a sentence somebody can act on; silently
    omitting the part that failed is not.
    """
    state = machine or inspect()
    parts: List[str] = []
    parts.append(f"{state.memory_gb:.0f} GB memory" if state.memory_gb else "memory could not be read")
    if state.cpu_cores:
        parts.append(f"{state.cpu_cores} CPU cores")
    parts.append(", ".join(state.gpus) if state.gpus else "no dedicated graphics detected")
    parts.append(
        f"{state.free_disk_gb:.0f} GB free on {state.models_path}"
        if state.free_disk_gb is not None
        else "free disk space could not be read"
    )
    return "; ".join(parts) + "."
