"""Turning samples into sound, and stopping when asked.

Playback is `winsound`, which is in the standard library on Windows and
therefore adds nothing to the installer and nothing to the licence
manifest. It plays a WAV file synchronously, which sounds like a
limitation and is actually what this needs: sentences are played one
after another on a worker thread, so "play the next one when this one
finishes" is the loop, not a scheduling problem.

`winsound.PlaySound(None, 0)` stops whatever is playing immediately, and
that is what Stop is. The cancel event stops synthesis; this stops the
sound already leaving the speakers. Both are needed — cancelling
synthesis alone would leave the last sentence playing to the end.

**One voice at a time.** Starting a new utterance stops the previous one
rather than mixing with it. Two overlapping voices is not a feature that
was missing, it is the bug that happens when nothing owns playback.

There is no playback backend off Windows. That is reported honestly
rather than emulated: this is a Windows product, and the tests here work
on the encoded bytes, which is where the correctness actually lives.
"""

import io
import struct
import sys
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from app.logging_config import get_logger

logger = get_logger("voice.audio")

SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM
CHANNELS = 1


def playback_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winsound  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def to_pcm16(samples) -> bytes:
    """float32 in roughly [-1, 1] to little-endian 16-bit PCM.

    Clipped rather than scaled to fit: a model that occasionally exceeds
    1.0 on a plosive should not make the whole utterance quieter.
    """
    try:
        import numpy

        array = numpy.asarray(samples, dtype=numpy.float32)
        clipped = numpy.clip(array, -1.0, 1.0)
        return (clipped * 32767.0).astype("<i2").tobytes()
    except ImportError:
        values = [max(-1.0, min(1.0, float(value))) for value in samples]
        return struct.pack("<%dh" % len(values), *[int(v * 32767.0) for v in values])


def encode_wav(samples, sample_rate: int) -> bytes:
    """A complete RIFF/WAVE file in memory."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH_BYTES)
        handle.setframerate(sample_rate)
        handle.writeframes(to_pcm16(samples))
    return buffer.getvalue()


def write_wav(path: Path, samples, sample_rate: int) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_wav(samples, sample_rate))
    return path


@dataclass
class _RawWav:
    """An utterance that arrives already encoded — from an engine that
    produces a whole WAV rather than sample arrays. Playback, stopping
    and the one-voice-at-a-time rule are shared with everything else;
    only the encoding step is skipped."""

    wav_bytes: bytes
    seconds: float = 0.0


@dataclass
class PlaybackState:
    playing: bool = False
    chunks_played: int = 0
    seconds_played: float = 0.0
    stopped: bool = False


class Player:
    """Sequential playback of one utterance at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state = PlaybackState()

    # --- state ---

    def state(self) -> PlaybackState:
        with self._lock:
            return PlaybackState(**vars(self._state))

    def is_playing(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _set(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self._state, key, value)

    # --- control ---

    def stop(self) -> bool:
        """Silence now: stop the producer, and cut the sound already
        playing. False only if the sound could not be cut — reporting a
        stop that did not happen is worse than reporting the failure."""
        self._cancel.set()
        stopped = True
        if sys.platform == "win32":
            try:
                import winsound

                winsound.PlaySound(None, 0)
            except Exception:  # noqa: BLE001
                logger.warning("Could not stop playback.", exc_info=True)
                stopped = False
        self._set(stopped=True, playing=False)
        return stopped

    def cancel_event(self) -> threading.Event:
        """The event a producer should check so it stops making audio
        nobody is going to hear."""
        return self._cancel

    def begin_utterance(self) -> threading.Event:
        """Reserve playback for a synthesis operation that may block.

        Cloud synthesis can take seconds before it yields WAV bytes.  Its
        cancellation token therefore has to exist *before* the request, not
        only when playback begins.  Stop (or a newer utterance) sets this
        token; delayed bytes carrying an old token are then discarded.
        """
        self.stop()
        self.wait(timeout=2.0)
        with self._lock:
            self._cancel = threading.Event()
            return self._cancel

    def is_current(self, cancel: threading.Event) -> bool:
        """Whether *cancel* still owns the right to start playback."""
        with self._lock:
            return cancel is self._cancel and not cancel.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    # --- playing ---

    def play_stream(self, chunks: Iterable, sample_rate: int) -> threading.Event:
        """Play already-created chunks on a worker thread, returning at once."""
        return self.play_cancelable_stream(lambda _cancel: chunks, sample_rate)

    def play_cancelable_stream(
        self,
        make_chunks: Callable[[threading.Event], Iterable],
        sample_rate: int,
    ) -> threading.Event:
        """Build and play a lazy stream with the cancellation token it owns.

        The token is created *after* the previous utterance is stopped.
        A lazy synthesiser must not be handed the previous token: stop()
        sets that token, and doing so made a newly-created Kokoro stream
        see cancellation before its first inference and emit no audio.

        The worker checks cancellation before asking the producer for its
        next chunk. A stop during an uninterruptible inference still has
        to wait for that inference to return, but it will neither start a
        second inference nor play the chunk that just finished.
        """
        cancel = self.begin_utterance()
        chunks = make_chunks(cancel)
        self._start_stream(chunks, sample_rate, cancel)
        return cancel

    def _start_stream(
        self, chunks: Iterable, sample_rate: int, cancel: threading.Event,
    ) -> None:
        """Start prepared chunks only while their reservation is current."""
        if not self.is_current(cancel):
            return
        self._set(playing=True, chunks_played=0, seconds_played=0.0, stopped=False)

        def _run() -> None:
            try:
                iterator = iter(chunks)
                while not cancel.is_set():
                    try:
                        chunk = next(iterator)
                    except StopIteration:
                        break
                    if cancel.is_set():
                        break
                    seconds = getattr(chunk, "seconds", 0.0)
                    if isinstance(chunk, _RawWav):
                        self._play_one(chunk.wav_bytes)
                    else:
                        self._play_one(encode_wav(getattr(chunk, "samples", chunk), sample_rate))
                    with self._lock:
                        self._state.chunks_played += 1
                        self._state.seconds_played += seconds
            except Exception:  # noqa: BLE001
                # Speech failing must never take the app with it.
                logger.warning("Playback stopped on an error.", exc_info=True)
            finally:
                self._set(playing=False)

        thread = threading.Thread(target=_run, daemon=True, name="jarvis-audio")
        with self._lock:
            self._thread = thread
        thread.start()

    def play_wav_bytes(self, wav_bytes: bytes) -> None:
        """Play one complete WAV, for an engine that produces a whole
        utterance at once rather than a stream of sentences."""
        self.play_stream([_RawWav(wav_bytes)], sample_rate=0)

    def play_wav_bytes_if_current(
        self, wav_bytes: bytes, cancel: threading.Event,
    ) -> bool:
        """Play delayed WAV bytes only if Stop has not superseded them."""
        if not self.is_current(cancel):
            return False
        self._start_stream([_RawWav(wav_bytes)], sample_rate=0, cancel=cancel)
        return self.is_current(cancel)

    def _play_one(self, wav_bytes: bytes) -> None:
        if sys.platform != "win32":
            return
        try:
            import winsound

            # SND_MEMORY plays the bytes directly — no temporary file to
            # write, to clean up, or to leave behind on a crash.
            winsound.PlaySound(wav_bytes, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except Exception:  # noqa: BLE001
            logger.warning("Could not play audio.", exc_info=True)


player = Player()
