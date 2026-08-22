"""Where voice input is up to, in one word plus what to do about it.

The reported defect, from the installed release candidate:

    Voice input — Not set up
    The local speech engine isn't available in this installation.
    Reinstalling JARVIS should restore it.

    Microphone permission   Not asked yet
    Input device            1 detected
    Speech runtime          Not installed
    Installed model         Not installed
    Model location          No model installed
    Last check              Not run yet

Every row was accurate. The packaging fault behind it is fixed
elsewhere; what this module fixes is that six accurate rows and a
sentence recommending a reinstall that would not have helped is not a
diagnosis. There are ten distinct situations here, each with a different
thing to do, and until they were told apart the answer to all of them
was "reinstall".

**Three of the ten are only knowable in the browser.** Whether the
microphone permission has been asked for, whether it was refused, and
whether the machine has an input device at all are facts about the page,
not the server. Their names live here anyway so that both halves speak
the same vocabulary and the panel can show one state rather than two
disagreeing ones.

**Nothing here opens a microphone or records anything.** It reads
whether the pieces are present. The one thing it remembers is the
*message* from the last failed transcription — never audio, never a
transcript, never anything that was said.
"""

import threading
from dataclasses import dataclass
from typing import Optional

from app.logging_config import get_logger

logger = get_logger("voice.input_state")

# The ten states. Named rather than numbered because a number in a log
# line is not a diagnosis either.
DISABLED = "disabled"                              # 1. switched off by the user
PERMISSION_NOT_REQUESTED = "permission_not_requested"  # 2. browser-side
PERMISSION_DENIED = "permission_denied"            # 3. browser-side
NO_INPUT_DEVICE = "no_input_device"                # 4. browser-side
RUNTIME_MISSING = "runtime_missing"                # 5. the engine did not load
MODEL_MISSING = "model_missing"                    # 6. no model downloaded
DOWNLOADING = "downloading"                        # 7. fetching one
VERIFYING = "verifying"                            # 8. checking it is intact
READY = "ready"                                    # 9. everything present
TRANSCRIPTION_FAILED = "transcription_failed"      # 10. it ran and did not work

ALL_STATES = (
    DISABLED, PERMISSION_NOT_REQUESTED, PERMISSION_DENIED, NO_INPUT_DEVICE,
    RUNTIME_MISSING, MODEL_MISSING, DOWNLOADING, VERIFYING, READY,
    TRANSCRIPTION_FAILED,
)

# The three the server cannot see. Kept as a set so the browser half and
# this half cannot drift into two vocabularies.
BROWSER_STATES = (PERMISSION_NOT_REQUESTED, PERMISSION_DENIED, NO_INPUT_DEVICE)


@dataclass(frozen=True)
class InputState:
    state: str
    headline: str
    detail: str
    next_step: str
    percent: int = 0

    @property
    def ready(self) -> bool:
        return self.state == READY

    @property
    def busy(self) -> bool:
        return self.state in (DOWNLOADING, VERIFYING)


class _LastFailure:
    """The message from the most recent failed transcription.

    Kept in memory only, and only the message. A panel that says "the
    last attempt failed because the model timed out" is the difference
    between a user knowing their setup is broken and a user believing
    push-to-talk simply does nothing.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._message = ""

    def record(self, message: str) -> None:
        with self._lock:
            self._message = (message or "").strip()[:300]

    def clear(self) -> None:
        with self._lock:
            self._message = ""

    def message(self) -> str:
        with self._lock:
            return self._message


last_failure = _LastFailure()


def describe() -> InputState:
    """The server's half of the answer. Never raises.

    The browser overlays its three states on top of this when they apply,
    because a refused microphone permission matters more than a model
    that is present and unused.
    """
    try:
        return _describe()
    except Exception:  # noqa: BLE001
        logger.warning("Voice input state could not be computed.", exc_info=True)
        return InputState(
            state=RUNTIME_MISSING,
            headline="Voice input could not be checked.",
            detail="Something went wrong reading the speech engine's state.",
            next_step="Press Run diagnostics again. Typing works normally either way.",
        )


def _percent(install) -> int:
    """Download progress, or 0 when the total is not known yet."""
    total = getattr(install, "bytes_total", 0) or 0
    done = getattr(install, "bytes_downloaded", 0) or 0
    if total <= 0:
        return 0
    return min(100, int(done * 100 / total))


def _describe() -> InputState:
    from app.voice.model_installer import model_installer
    from app.voice.stt import input_enabled, stt_service

    install = model_installer.state()
    progress = _percent(install)
    if install.status in ("checking", "downloading"):
        return InputState(
            state=DOWNLOADING,
            headline="Downloading the speech model…",
            detail=install.message or "The model is being downloaded.",
            next_step="You can cancel; nothing is installed until it has been checked.",
            percent=progress,
        )
    if install.status in ("verifying", "installing"):
        return InputState(
            state=VERIFYING,
            headline="Checking the speech model…",
            detail=install.message or "Verifying what was downloaded.",
            next_step="Almost done — nothing is installed until the checksums match.",
            percent=progress,
        )

    runtime_ready, runtime_detail = stt_service.runtime_status()
    if not runtime_ready:
        return InputState(
            state=RUNTIME_MISSING,
            headline="The speech engine is not available.",
            detail=runtime_detail,
            next_step=(
                "This part ships inside JARVIS, so this is a broken installation "
                "rather than something to switch on. Reinstalling JARVIS restores it."
            ),
        )

    model_ready, model_detail = stt_service.model_status()
    if not model_ready:
        return InputState(
            state=MODEL_MISSING,
            headline="The speech model has not been downloaded yet.",
            detail=model_detail,
            next_step="Press Download speech model. Its size and licence are shown first.",
        )

    if not input_enabled():
        return InputState(
            state=DISABLED,
            headline="Voice input is switched off.",
            detail="Everything it needs is installed; the switch is off.",
            next_step="Turn on “Allow voice input” to use the microphone button in Chat.",
        )

    failure = last_failure.message()
    if failure:
        return InputState(
            state=TRANSCRIPTION_FAILED,
            headline="The last recording could not be transcribed.",
            detail=failure,
            next_step=(
                "Try the microphone test below. If that works, the recording was "
                "probably too quiet or too short."
            ),
        )

    return InputState(
        state=READY,
        headline="Voice input is ready.",
        detail="The speech engine and model are installed and voice input is on.",
        next_step="Hold the microphone button in Chat, or press Alt+M, and speak.",
    )
