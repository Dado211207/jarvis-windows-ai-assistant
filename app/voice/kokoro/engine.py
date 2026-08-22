"""Running Kokoro locally: phonemes in, audio out.

Sentence by sentence, on purpose. On the machine this was measured on a
reply takes longer to synthesise than it does to say, so waiting for a
whole paragraph before the first sound would put a visible pause between
asking and hearing. Synthesising one sentence at a time lets playback
start after the first and continue while the rest is still being made,
and it gives cancellation somewhere to take effect — a Stop that only
applied between whole replies would not be a stop.

The model's interface was read from the model, not from documentation:

    input_ids  int64   [1, sequence_length]
    style      float32 [1, 256]
    speed      float32 [1]
    waveform   float32 [1, num_samples]   at 24 kHz

The style pack holds 510 vectors and the one used is chosen by the token
count of the sequence being spoken, which is also why 510 tokens is a
hard ceiling rather than a tuning choice (tokens.MAX_TOKENS).

Nothing here raises past its own boundary. A missing model, a missing
runtime and a failed inference are three different problems with three
different things a user can do about them, so they are three different
messages — never one "speech failed".
"""

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional

from app.logging_config import get_logger
from app.voice.kokoro import assets, g2p, install, normalise, tokens

logger = get_logger("voice.kokoro.engine")

SAMPLE_RATE = 24000

# Kokoro accepts a speed multiplier. Bounded because the model does not
# check, and 0 produces a division by zero deep inside the graph.
MIN_SPEED = 0.5
MAX_SPEED = 2.0
DEFAULT_SPEED = 1.0


class EngineUnavailable(Exception):
    """Raised only inside this module, and always carrying a sentence
    that can be shown to a person unchanged."""


@dataclass
class SynthesisChunk:
    """One sentence of audio, with what it came from — the text is kept
    for the diagnostics panel, where "what did it try to say" is the
    first question worth answering."""

    samples: object          # numpy.ndarray, float32, mono
    text: str
    phonemes: str
    seconds: float


@dataclass
class SynthesisReport:
    """What happened, for the diagnostics panel and the tests."""

    chunks: int = 0
    seconds: float = 0.0
    cancelled: bool = False
    spelled_words: List[str] = field(default_factory=list)


def clamp_speed(speed: float) -> float:
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return DEFAULT_SPEED
    return max(MIN_SPEED, min(MAX_SPEED, value))


# The imported ONNX Runtime module, held deliberately.
#
# Not an optimisation. onnxruntime refuses to load twice in one process
# ("cannot load module more than once per process"), so anything that
# drops it from sys.modules — including, as it turns out, an ordinary
# unittest.mock.patch.dict of sys.modules — makes every later import of
# it fail permanently. Probing availability with a bare `import` inside
# a function therefore has a failure mode where the neural voice dies
# for the rest of the process and never comes back. Keeping our own
# reference means the answer is decided once, by the only attempt that
# can be made.
_runtime = None
_runtime_checked = False


def runtime_available() -> bool:
    """Whether ONNX Runtime is usable in this process."""
    return _load_runtime() is not None


def _load_runtime():
    global _runtime, _runtime_checked

    if _runtime_checked:
        return _runtime
    try:
        import onnxruntime

        _runtime = onnxruntime
    except Exception:  # noqa: BLE001
        logger.info("ONNX Runtime is not available; the neural voice cannot run.")
        _runtime = None
    _runtime_checked = True
    return _runtime


def split_for_model(phonemes: str) -> List[str]:
    """Break a phoneme string into pieces the model can actually take.

    Sentences are already short enough almost always; this is the
    backstop for the one that is not, and it breaks on a space so a word
    is never cut in half.
    """
    budget = tokens.MAX_TOKENS - 2  # the two pad tokens
    if len(phonemes) <= budget:
        return [phonemes] if phonemes else []

    pieces: List[str] = []
    remaining = phonemes
    while len(remaining) > budget:
        cut = remaining.rfind(" ", 0, budget)
        if cut <= 0:
            cut = budget
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return [piece for piece in pieces if piece]


class KokoroEngine:
    """The loaded model. One instance, loaded once, shared by callers."""

    def __init__(self) -> None:
        self._session = None
        self._session_path: Optional[Path] = None
        self._styles: dict = {}
        self._lock = threading.Lock()

    # --- availability -----------------------------------------------------

    def is_ready(self, voice_key: str = assets.DEFAULT_VOICE_KEY) -> bool:
        return runtime_available() and install.is_installed(voice_key)

    def unavailable_reason(self, voice_key: str = assets.DEFAULT_VOICE_KEY) -> str:
        """Empty when ready. Otherwise the specific thing that is wrong,
        phrased as something a person can act on."""
        if not runtime_available():
            return (
                "The speech runtime (ONNX Runtime) is not available in this build, "
                "so the neural voice cannot run."
            )
        if not install.is_installed(voice_key):
            voice = assets.resolve_voice(voice_key)
            megabytes = install.bytes_required(voice.key) / (1024 * 1024)
            return (
                f"The {voice.display_name} voice is not installed yet "
                f"({megabytes:.0f} MB to download)."
            )
        return ""

    # --- loading ----------------------------------------------------------

    def _load_session(self):
        if self._session is not None:
            return self._session
        with self._lock:
            if self._session is not None:
                return self._session
            onnxruntime = _load_runtime()
            if onnxruntime is None:
                raise EngineUnavailable(
                    "The speech runtime (ONNX Runtime) is not available in this build."
                )

            path = install.asset_path(assets.MODEL_ASSET)
            if not path.is_file():
                raise EngineUnavailable("The voice model is not installed yet.")

            options = onnxruntime.SessionOptions()
            # The model is chatty at info level and none of it is
            # actionable; warnings and errors still come through.
            options.log_severity_level = 3
            try:
                session = onnxruntime.InferenceSession(
                    str(path), options, providers=["CPUExecutionProvider"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not load the Kokoro model: %s", exc, exc_info=True)
                raise EngineUnavailable(
                    "The voice model could not be loaded — the file may be damaged. "
                    "Reinstalling the voice will replace it."
                ) from exc

            self._session = session
            self._session_path = path
            logger.info("Kokoro model loaded from %s", path)
            return session

    def _load_style(self, voice_key: str):
        """The style pack for one voice, as (510, 1, 256) float32."""
        import numpy

        voice = assets.resolve_voice(voice_key)
        cached = self._styles.get(voice.key)
        if cached is not None:
            return cached

        path = install.asset_path(voice.asset)
        if not path.is_file():
            raise EngineUnavailable(f"The {voice.display_name} voice is not installed yet.")
        try:
            data = numpy.fromfile(str(path), dtype=numpy.float32).reshape(-1, 1, 256)
        except (OSError, ValueError) as exc:
            raise EngineUnavailable(
                f"The {voice.display_name} voice file is damaged. Reinstalling it will "
                "replace it."
            ) from exc
        self._styles[voice.key] = data
        return data

    def unload(self) -> None:
        """Release the session and the style packs. Used when switching
        voices is not enough — a reinstall has replaced the files under
        us and the loaded copy is the old one."""
        with self._lock:
            self._session = None
            self._session_path = None
            self._styles = {}

    # --- synthesis --------------------------------------------------------

    def synthesise_phonemes(self, phonemes: str, voice_key: str, speed: float = DEFAULT_SPEED):
        """One piece of already-validated phonemes to a mono float32 array."""
        import numpy

        session = self._load_session()
        styles = self._load_style(voice_key)

        ids = tokens.encode(phonemes)
        if len(ids) <= 2:
            return numpy.zeros(0, dtype=numpy.float32)

        index = min(len(ids), styles.shape[0] - 1)
        style = styles[index].astype(numpy.float32)

        waveform = session.run(
            None,
            {
                "input_ids": numpy.array([ids], dtype=numpy.int64),
                "style": style,
                "speed": numpy.array([clamp_speed(speed)], dtype=numpy.float32),
            },
        )[0]
        return numpy.asarray(waveform, dtype=numpy.float32).reshape(-1)

    def synthesise(
        self,
        text: str,
        voice_key: str = assets.DEFAULT_VOICE_KEY,
        speed: float = DEFAULT_SPEED,
        cancel: Optional[threading.Event] = None,
        report: Optional[SynthesisReport] = None,
    ) -> Iterator[SynthesisChunk]:
        """Text to audio, one sentence at a time.

        Yields as it goes so a caller can start playing the first
        sentence while the second is still being made. Checks *cancel*
        between sentences and before each inference, which is as often as
        it can be checked — a single inference is not interruptible from
        outside.
        """
        spoken = normalise.normalise(text)
        if not spoken.strip():
            return

        user_dictionary = _user_dictionary()

        for sentence in normalise.split_sentences(spoken):
            if cancel is not None and cancel.is_set():
                if report is not None:
                    report.cancelled = True
                return
            phonemes = g2p.phonemise(sentence, user_dictionary)
            valid, reason = g2p.validate_phonemes(phonemes)
            if not valid:
                # Never send the model something it cannot read: the
                # result would be noise, not an error. Dropping the
                # sentence is worse than saying it, so this is logged
                # loudly rather than passed over.
                logger.error(
                    "Refusing to synthesise a sentence with unsupported phonemes (%s): %r",
                    reason, sentence,
                )
                continue

            for piece in split_for_model(phonemes):
                if cancel is not None and cancel.is_set():
                    if report is not None:
                        report.cancelled = True
                    return
                samples = self.synthesise_phonemes(piece, voice_key, speed)
                if samples.size == 0:
                    continue
                seconds = samples.size / SAMPLE_RATE
                if report is not None:
                    report.chunks += 1
                    report.seconds += seconds
                yield SynthesisChunk(
                    samples=samples, text=sentence, phonemes=piece, seconds=seconds,
                )

    def synthesise_all(
        self,
        text: str,
        voice_key: str = assets.DEFAULT_VOICE_KEY,
        speed: float = DEFAULT_SPEED,
        cancel: Optional[threading.Event] = None,
    ):
        """Everything as one array. For writing a file, where there is no
        one waiting to hear the first sentence."""
        import numpy

        pieces = [chunk.samples for chunk in self.synthesise(text, voice_key, speed, cancel)]
        if not pieces:
            return numpy.zeros(0, dtype=numpy.float32)
        return numpy.concatenate(pieces)


def _user_dictionary():
    """The user's own pronunciations, if any. Never fatal: a broken
    dictionary file must not stop JARVIS speaking."""
    try:
        from app.voice import pronunciations
        return pronunciations.load()
    except Exception:  # noqa: BLE001
        logger.warning("Could not load custom pronunciations.", exc_info=True)
        return None


engine = KokoroEngine()
