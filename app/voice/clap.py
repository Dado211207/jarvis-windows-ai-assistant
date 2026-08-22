"""Double-clap activation: the narrowest hands-free trigger that can exist.

The product owner asked for one specific convenience — clap twice and
JARVIS comes to the front — and CLAUDE.md's Safety rules ban continuous
listening. Those are not actually in conflict, but only because of how
narrow this is, so the boundary is written down here rather than left to
be re-derived later.

**What listens, and where.** Nothing in Python listens. The detector is
an `AudioWorkletProcessor` (app/ui/static/clap-processor.js) running in
the same WebView2 page that already draws the dashboard, on the same
`getUserMedia` stream the microphone level meter already uses. JARVIS
has never had a native audio-input dependency and this did not add one:
no PortAudio, no PyAudio, no sounddevice. Playback is still stdlib
`winsound` and still output-only.

**What the detector computes.** Per 128-sample block: root-mean-square
and peak amplitude. That is the whole feature set. It looks for a sharp
onset that decays back to the noise floor within ~160 ms, twice, within
a bounded gap. It is a transient counter. It cannot tell a clap from a
slammed book, and deliberately has no idea what either of them means.

**What leaves the worklet.** One message with no payload:
`{type: "clap-pair"}`. No samples, no levels, no timestamps, no audio.
There is nothing in that message to log, redact or transmit, because
there is nothing in it.

**What is not here, and must never be.** No wake word. No speech
recognition. No transcription. No recording to disk. No buffer of raw
audio kept for any length of time — the worklet's state is six floats.
No audio sent anywhere. And no command execution: a clap can only
*show the window*, using the same zero-data marker file that a second
launch of the Start-menu shortcut already uses
(app/launcher/attention.py). A clap cannot run a tool, and the path it
takes physically has no way to name one.

**Off by default**, and gated three times on the server:

1. the stored preference, which starts false;
2. privacy mode — app/core/privacy.py's own docstring already said any
   future listener must check it, and this is that listener;
3. a refractory interval, so a burst of transients is one activation.

The gates are server-side for the same reason the speech gate is: a page
left open before the feature was switched off somewhere else must not
still be able to act.

**The greeting is not a second speech switch.** It is spoken through the
ordinary path and only when `tts_service.output_enabled` is already on.
Somebody who has turned speech off gets a window, silently.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Dict

from app.logging_config import get_logger

logger = get_logger("voice.clap")

# Two activations closer together than this are one activation. Chosen to
# be comfortably longer than the worklet's own refractory so that even a
# page with a stale, wrong configuration cannot produce a stream of them.
MIN_INTERVAL_SECONDS = 2.0

# The greeting, if speech is already on. Short on purpose: this plays
# when somebody has just clapped and is waiting for a window.
DEFAULT_GREETING = "At your service."
MAX_GREETING_CHARS = 120

# Sensitivity is three named choices rather than a slider of raw numbers,
# because "0.035" is not a decision anybody can make about their own
# living room. Each profile is the same algorithm with different bounds.
#
#   floor_ratio   how far above the measured background a block must rise
#   abs_min       an absolute floor, so a silent room cannot make the
#                 detector infinitely sensitive
#   attack_fall   how far *below* threshold the preceding block must be —
#                 this is what separates a clap from speech, which ramps
#                 over tens of milliseconds instead of one block
#   max_transient the longest a clap may last before it is something else
#   min_gap/max_gap   the window the second clap must land in
#   refractory    the worklet's own quiet period after firing
#
# The defaults were not guessed: they were measured against synthesised
# claps, single claps, mistimed claps, amplitude-modulated speech, a
# sustained tone and near-silence, played through a real Chromium
# microphone. See tests/test_clap_detection.py, which runs exactly that.
SENSITIVITY_PROFILES: Dict[str, Dict[str, float]] = {
    "low": {
        "floorRatio": 9.0,
        "absMin": 0.070,
        "attackFall": 0.30,
        "maxTransient": 0.140,
        "minGap": 0.120,
        "maxGap": 0.600,
        "refractory": 1.5,
    },
    "normal": {
        "floorRatio": 6.0,
        "absMin": 0.035,
        "attackFall": 0.35,
        "maxTransient": 0.160,
        "minGap": 0.120,
        "maxGap": 0.700,
        "refractory": 1.5,
    },
    "high": {
        "floorRatio": 4.0,
        "absMin": 0.018,
        "attackFall": 0.45,
        "maxTransient": 0.200,
        "minGap": 0.100,
        "maxGap": 0.800,
        "refractory": 1.5,
    },
}
DEFAULT_SENSITIVITY = "normal"


@dataclass
class Activation:
    """What happened when a page reported a clap pair.

    `accepted` is the only thing the browser needs; the rest is what the
    Voice page shows so a feature that did nothing says why.
    """

    accepted: bool
    reason: str
    window_shown: bool = False
    greeted: bool = False


@dataclass
class _State:
    last_activation: float = 0.0
    activations: int = 0
    last_reason: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


_state = _State()


# ---------------------------------------------------------------------------
# Settings. Stored in preferences (never a credential, never audio).
# ---------------------------------------------------------------------------

def _text(key: str, default: str) -> str:
    from app.core import preferences
    value = preferences.get(key)
    return default if value is None else value


def _flag(key: str, default: bool) -> bool:
    from app.core import preferences
    value = preferences.get_bool(key)
    return default if value is None else value


def _store(key: str, value) -> None:
    from app.core import preferences
    if isinstance(value, bool):
        value = "true" if value else "false"
    preferences.store(key, value)


def enabled() -> bool:
    return _flag("clap_enabled", False)


def set_enabled(value: bool) -> bool:
    _store("clap_enabled", bool(value))
    if not value:
        # Forget the history too. "Last activated at" is meaningless once
        # the feature is off, and leaving it on screen implies it is
        # still listening.
        with _state.lock:
            _state.last_activation = 0.0
            _state.last_reason = ""
    logger.info("Double-clap activation %s.", "enabled" if value else "disabled")
    return bool(value)


def sensitivity() -> str:
    value = _text("clap_sensitivity", DEFAULT_SENSITIVITY)
    return value if value in SENSITIVITY_PROFILES else DEFAULT_SENSITIVITY


def set_sensitivity(value: str) -> str:
    chosen = value if value in SENSITIVITY_PROFILES else DEFAULT_SENSITIVITY
    _store("clap_sensitivity", chosen)
    return chosen


def greet_enabled() -> bool:
    """Whether to say anything at all on activation.

    A separate flag from the text rather than "an empty greeting means
    silence", because `preferences.store()` treats a blank value as
    "unset" and unset has to mean "use the default". Two explicit
    controls beat one control with a hidden second meaning.
    """
    return _flag("clap_greet", True)


def set_greet_enabled(value: bool) -> bool:
    _store("clap_greet", bool(value))
    return bool(value)


def greeting() -> str:
    return _text("clap_greeting", DEFAULT_GREETING)[:MAX_GREETING_CHARS]


def set_greeting(value: str) -> str:
    text = (value or "").strip()[:MAX_GREETING_CHARS]
    _store("clap_greeting", text)
    return greeting()


def detector_settings() -> Dict[str, float]:
    """The tuning the worklet runs with.

    Served to the page rather than hardcoded in the JavaScript so there
    is one place these numbers live, and so a sensitivity change takes
    effect on the next start of the listener rather than the next
    release.
    """
    return dict(SENSITIVITY_PROFILES[sensitivity()])


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

def _speak_greeting() -> bool:
    """Speak the greeting if — and only if — JARVIS is already speaking
    replies. Never raises: a failure here must not turn a working window
    into a failed activation."""
    if not greet_enabled():
        return False
    text = greeting()
    if not text:
        return False
    try:
        from app.voice.tts import tts_service
        if not tts_service.output_enabled:
            return False
        result = tts_service.speak(text)
        return bool(getattr(result, "success", False))
    except Exception:
        logger.warning("The clap greeting could not be spoken.", exc_info=True)
        return False


def _show_window() -> bool:
    """Ask the running instance to bring its window forward.

    Uses app/launcher/attention.py, whose entire message is the existence
    of a file. There is deliberately no richer channel here: a clap must
    not be able to say anything more specific than "show yourself".
    """
    try:
        from app.launcher import attention
        return bool(attention.request())
    except Exception:
        logger.warning("Could not signal the window to show.", exc_info=True)
        return False


def activate() -> Activation:
    """A page reported a clap pair. Decide whether to act on it.

    Never raises. Called from a request handler, and a listener that
    takes the server down is worse than one that does nothing.
    """
    if not enabled():
        return _record(Activation(False, "disabled"))

    try:
        from app.core.privacy import privacy_mode
        if privacy_mode.active:
            return _record(Activation(False, "privacy_mode"))
    except Exception:  # pragma: no cover - import guard only
        logger.warning("Could not read privacy mode; refusing activation.", exc_info=True)
        return _record(Activation(False, "privacy_mode"))

    # The claim and the refusal both have to happen outside the lock:
    # `_record()` takes it too, and threading.Lock is not reentrant. This
    # deadlocked a request handler until tests/test_clap.py caught it.
    now = time.monotonic()
    with _state.lock:
        too_soon = now - _state.last_activation < MIN_INTERVAL_SECONDS
        if not too_soon:
            _state.last_activation = now
            _state.activations += 1
    if too_soon:
        return _record(Activation(False, "too_soon"))

    shown = _show_window()
    greeted = _speak_greeting()
    logger.info("Double-clap activation accepted (window_shown=%s, greeted=%s).", shown, greeted)
    return _record(Activation(True, "activated", window_shown=shown, greeted=greeted))


def _record(activation: Activation) -> Activation:
    with _state.lock:
        _state.last_reason = activation.reason
    return activation


def status() -> dict:
    """What the Voice page shows. No audio, no levels, no timestamps of
    anything but the last activation."""
    with _state.lock:
        activations = _state.activations
        last_reason = _state.last_reason
        last = _state.last_activation
    seconds_since = None
    if last:
        seconds_since = max(0.0, round(time.monotonic() - last, 1))

    privacy_blocking = False
    try:
        from app.core.privacy import privacy_mode
        privacy_blocking = bool(privacy_mode.active)
    except Exception:  # pragma: no cover
        privacy_blocking = True

    return {
        "enabled": enabled(),
        "sensitivity": sensitivity(),
        "sensitivities": sorted(SENSITIVITY_PROFILES),
        "greet": greet_enabled(),
        "greeting": greeting(),
        "detector": detector_settings(),
        "privacy_blocking": privacy_blocking,
        "activations": activations,
        "last_reason": last_reason,
        "seconds_since_activation": seconds_since,
        "min_interval_seconds": MIN_INTERVAL_SECONDS,
    }


def reset_for_tests() -> None:
    """Clear the in-memory activation history. Tests only."""
    with _state.lock:
        _state.last_activation = 0.0
        _state.activations = 0
        _state.last_reason = ""
