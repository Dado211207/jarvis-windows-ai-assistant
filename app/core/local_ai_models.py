"""Downloading a model into Ollama, with the user watching.

Several gigabytes on somebody's connection and somebody's disk, so:

  * **Nothing starts without being asked.** The caller shows the model,
    its approximate size and this machine's free space first; this
    module only acts when told to.
  * **Cancellable at any point.** Closing the stream stops the transfer.
    Ollama keeps whatever it had already verified, so cancelling costs
    nothing but the time already spent.
  * **Resumable.** A cancelled or failed pull, retried, continues from
    the blobs already on disk — Ollama's own behaviour, which is why
    retry is offered as a first response to a failure rather than a
    last one.
  * **Self-checking.** Ollama verifies each layer's digest and reports
    the failure; a corrupted layer is reported as corrupted and fixed by
    pulling again, not silently used.
  * **Not finished until it answers.** "Downloaded" is not "working". A
    completed pull is followed by a real generation
    (`local_ai.verify_with_real_inference`) and only reports complete if
    the model actually produced text. A model that downloads perfectly
    and then fails to load is a real outcome, and without this it would
    first appear when somebody tried to have a conversation.

This calls Ollama's `/api/pull` on the loopback address only. Until this
release JARVIS never called it at all, and `CLAUDE.md` said so as a
non-negotiable rule; the product's owner has since decided that a person
who wants local AI should be able to get it from a button, and the rule
and its tests are updated rather than quietly worked around.
"""

import json
import threading
from dataclasses import dataclass
from typing import Optional

import httpx

from app.logging_config import get_logger

logger = get_logger("core.local_ai_models")

# No overall timeout: a multi-gigabyte pull legitimately takes as long as
# it takes. The read timeout is what protects against a server that has
# stopped talking, which is the failure that actually needs catching.
PULL_CONNECT_TIMEOUT_SECONDS = 10.0
PULL_READ_TIMEOUT_SECONDS = 120.0

IDLE = "idle"
DOWNLOADING = "downloading"
VERIFYING = "verifying"
COMPLETE = "complete"
ERROR = "error"
CANCELLED = "cancelled"

# Ollama's own status strings, mapped to the state this reports. Anything
# it says that is not listed here is shown verbatim rather than guessed
# at, so a new Ollama status never turns into a wrong one.
_VERIFYING_MARKERS = ("verifying", "writing manifest", "removing")


@dataclass
class PullState:
    status: str = IDLE
    model: str = ""
    detail: str = ""
    message: str = ""
    bytes_downloaded: int = 0
    bytes_total: int = 0

    @property
    def percent(self) -> int:
        if self.bytes_total <= 0:
            return 0
        return min(100, int(self.bytes_downloaded * 100 / self.bytes_total))

    @property
    def running(self) -> bool:
        return self.status in (DOWNLOADING, VERIFYING)


class ModelPuller:
    """One pull at a time, on a background thread, cancellable."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = PullState()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def state(self) -> PullState:
        with self._lock:
            return PullState(**vars(self._state))

    def _set(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self._state, key, value)

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def cancel(self) -> None:
        self._cancel.set()

    def start(self, model: str) -> bool:
        """Begin pulling *model*. False if one is already running or the
        name is empty."""
        name = (model or "").strip()
        if not name:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel.clear()
            self._state = PullState(
                status=DOWNLOADING, model=name, message=f"Starting the download of {name}…",
            )
            thread = threading.Thread(
                target=self._run, args=(name,), daemon=True, name="jarvis-ollama-pull",
            )
            self._thread = thread
        thread.start()
        return True

    # --- the work ---

    def _run(self, model: str) -> None:
        from app.core.providers import _ollama_base_url

        url = f"{_ollama_base_url()}/api/pull"
        timeout = httpx.Timeout(
            PULL_READ_TIMEOUT_SECONDS, connect=PULL_CONNECT_TIMEOUT_SECONDS, write=None, pool=None,
        )
        try:
            with httpx.stream(
                "POST", url, json={"model": model, "stream": True}, timeout=timeout,
            ) as response:
                if response.status_code != 200:
                    response.read()
                    self._set(
                        status=ERROR,
                        message=(
                            f"Ollama refused to download “{model}”. Check the name is right "
                            "— it must be one Ollama publishes."
                        ),
                    )
                    return
                for line in response.iter_lines():
                    if self._cancel.is_set():
                        self._set(
                            status=CANCELLED,
                            message=(
                                "Download cancelled. What had already downloaded is kept, so "
                                "starting again continues from there."
                            ),
                        )
                        return
                    if not line.strip():
                        continue
                    if not self._consume(line, model):
                        # break, not return: a finished stream still has
                        # to reach the "prove it answers" step below.
                        break
        except httpx.HTTPError as exc:
            self._set(
                status=ERROR,
                message=(
                    f"The download stopped: {exc}. Press Retry — it continues from what "
                    "already downloaded rather than starting again."
                ),
            )
            return
        except Exception as exc:  # noqa: BLE001 — a background thread must never die silently
            logger.warning("Model pull failed.", exc_info=True)
            self._set(status=ERROR, message=f"The download could not finish: {exc}")
            return

        if self.state().status == COMPLETE:
            self._prove_it_answers(model)
            return

        # The stream ended without an explicit success line. Treat that as
        # unfinished rather than done: reporting a model as installed when
        # it might not be is the failure this is trying to avoid.
        if self.state().status == DOWNLOADING:
            self._set(
                status=ERROR,
                message=(
                    "Ollama stopped sending progress before the download finished. "
                    "Press Retry to continue."
                ),
            )

    def _prove_it_answers(self, model: str) -> None:
        """Downloaded is not working.

        A model can arrive intact and still fail to load — not enough
        memory, an unsupported quantisation, a broken runtime. Without
        this, that appears for the first time when somebody tries to have
        a conversation, which is the worst possible moment to find out.
        """
        from app.core import local_ai

        self._set(status=VERIFYING, message=f"Checking that “{model}” answers…")
        result = local_ai.verify_with_real_inference(model=model)
        if result.ok:
            self._set(status=COMPLETE, message=result.message)
        else:
            self._set(
                status=ERROR,
                message=(
                    f"“{model}” downloaded, but did not answer when asked. {result.message}"
                ),
            )

    def _consume(self, line: str, model: str) -> bool:
        """One NDJSON line from Ollama. False to stop reading."""
        try:
            payload = json.loads(line)
        except ValueError:
            return True  # a partial or unexpected line is not a reason to abandon a download

        error = payload.get("error")
        if error:
            self._set(status=ERROR, message=_explain(str(error), model))
            return False

        status = str(payload.get("status") or "").strip()
        total = payload.get("total")
        completed = payload.get("completed")
        if isinstance(total, int) and total > 0:
            self._set(bytes_total=total)
        if isinstance(completed, int) and completed >= 0:
            self._set(bytes_downloaded=completed)

        if status == "success":
            self._set(
                status=COMPLETE,
                detail=status,
                message=f"“{model}” downloaded. Testing that it answers…",
            )
            return False

        if any(marker in status.lower() for marker in _VERIFYING_MARKERS):
            self._set(status=VERIFYING, detail=status, message=f"Checking “{model}” is intact…")
        elif status:
            self._set(status=DOWNLOADING, detail=status, message=f"Downloading “{model}”…")
        return True


def _explain(error: str, model: str) -> str:
    """Ollama's error, in words that name the fix."""
    lowered = error.lower()
    if "no space" in lowered or "disk" in lowered:
        return (
            f"There is not enough free disk space to finish downloading “{model}”. "
            "Free some space and press Retry — what already downloaded is kept."
        )
    if "digest" in lowered or "checksum" in lowered or "corrupt" in lowered:
        return (
            f"Part of “{model}” arrived damaged and was rejected. Press Retry: the "
            "damaged part is downloaded again and the rest is kept."
        )
    if "not found" in lowered or "manifest" in lowered:
        return (
            f"Ollama does not publish a model called “{model}”. Check the name, or pick "
            "the recommended one."
        )
    if "connection" in lowered or "timeout" in lowered or "eof" in lowered:
        return (
            f"The connection to Ollama's model server dropped while downloading “{model}”. "
            "Press Retry to continue from where it stopped."
        )
    return f"Ollama could not download “{model}”: {error}"


# Module-level singleton, matching every other background job here.
model_puller = ModelPuller()
