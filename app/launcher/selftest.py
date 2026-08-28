"""What the installed application can actually do, asked of itself.

Run as `JARVIS.exe --selftest`. It imports each runtime the product
claims to have, inside the frozen process, and prints one line per
capability plus a JSON summary. Exit code 0 only if every *required*
capability loaded.

**Why this exists.** The release candidate shipped with no speech input
at all. Every automated check passed, because every automated check ran
against the source tree, where `import faster_whisper` naturally works —
the package is installed in the dev environment. In the frozen build it
raised ImportError, because a hard dependency (`av`) had been declared
optional in the PyInstaller spec and its collection was silently
skipped. The product told the user to reinstall the identical artifact.

A source-tree import proves the dependency exists on the build machine.
It proves nothing about the thing the user runs. This asks the actual
`.exe`.

Deliberately reports rather than repairs: it is a diagnostic, and one
that changed state would be a worse diagnostic. Nothing in the default
run touches the network, loads a model, or opens an audio device.

**`--deep` goes one step further, and is the one that matters.** Loading
`onnxruntime` proves the runtime is bundled; it does not prove the voice
makes a sound. The deep pass runs the real model in the frozen process:
it synthesises a sentence to a real WAV and checks the samples are not
silence, then feeds that WAV to the real speech recogniser and checks the
words come back. Both models have to be installed first, which the
clean-install test does through the installed app's own download screens
— so what the deep pass verifies is the whole chain a user would use,
end to end, in the artifact they were sent.

Without `--deep` those two checks are skipped and say so. A skipped check
is never reported as a pass.
"""

import json
import platform
import sys
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional

from app.launcher.safe_output import say


@dataclass
class CapabilityResult:
    name: str
    required: bool
    ok: bool
    detail: str
    error_type: str = ""

    @property
    def blocking(self) -> bool:
        return self.required and not self.ok


def _probe(name: str, required: bool, check: Callable[[], str]) -> CapabilityResult:
    """Run one check, converting any failure into a reported result.

    Catches BaseException deliberately: a native extension that cannot
    find its DLLs can fail in ways that are not Exception subclasses, and
    a self-test that dies while reporting is worse than useless.
    """
    try:
        detail = check()
    except BaseException as exc:  # noqa: BLE001 — a diagnostic must never crash
        return CapabilityResult(
            name=name, required=required, ok=False,
            detail=str(exc)[:300] or exc.__class__.__name__,
            error_type=exc.__class__.__name__,
        )
    return CapabilityResult(name=name, required=required, ok=True, detail=detail)


def _check_speech_recognition() -> str:
    """The one that was broken. Imports the package *and* the submodule
    that pulls in the native audio decoder, because `faster_whisper`
    importing is not the same as `faster_whisper.audio` importing."""
    import faster_whisper
    import faster_whisper.audio  # noqa: F401 — this is the one that needs av

    return f"faster-whisper {getattr(faster_whisper, '__version__', 'unknown')}"


def _check_audio_decoder() -> str:
    import av

    return f"av {getattr(av, '__version__', 'unknown')}"


def _check_inference_engine() -> str:
    import ctranslate2

    return f"ctranslate2 {getattr(ctranslate2, '__version__', 'unknown')}"


def _check_tokenizers() -> str:
    import tokenizers

    return f"tokenizers {getattr(tokenizers, '__version__', 'unknown')}"


def _check_neural_voice_runtime() -> str:
    import onnxruntime

    providers = onnxruntime.get_available_providers()
    return f"onnxruntime {onnxruntime.__version__} ({', '.join(providers)})"


def _check_voice_pronunciation() -> str:
    """The bundled lexicon is app-owned package data, which PyInstaller
    does not collect automatically — a missing one leaves the voice able
    to speak only spelled-out letters."""
    from app.voice.kokoro import lexicon

    size = lexicon.size()
    if size <= 0:
        raise RuntimeError(f"pronunciation lexicon is empty or missing at {lexicon.data_path()}")
    return f"{size} words"


def _check_credential_store() -> str:
    import keyring

    backend = keyring.get_keyring()
    return type(backend).__name__


def _check_native_window() -> str:
    import webview  # noqa: F401

    return f"pywebview {getattr(webview, '__version__', 'unknown')}"


def _check_classic_speech() -> str:
    import pyttsx3  # noqa: F401

    return "pyttsx3 present"


def _check_cloud_voice_transport() -> str:
    import httpcore
    import httpx

    from app.voice import openai_tts

    if openai_tts.API_BASE != "https://api.openai.com":
        raise RuntimeError("OpenAI Speech destination is not pinned")
    return (
        f"httpx {getattr(httpx, '__version__', 'unknown')}; "
        f"httpcore {getattr(httpcore, '__version__', 'unknown')}; openai_tts"
    )


def _check_windows_wav_playback() -> str:
    import winsound  # noqa: F401

    return "winsound present"


def _check_windows_natural_voices() -> str:
    from app.voice import winrt_voices

    if not winrt_voices.projection_available():
        raise RuntimeError("the WinRT speech projection is not available in this build")
    return f"{len(winrt_voices.list_voices())} Windows voice(s)"


# ---------------------------------------------------------------------------
# The deep checks: the real model, in the frozen process, making a real
# sound and reading it back.
# ---------------------------------------------------------------------------

# What the voice is asked to say. Short enough to synthesise in a second
# or two, and made of words a small speech model transcribes reliably —
# the check is that the chain works, not that the model is clever.
DEEP_PHRASE = "The system is online and ready."
DEEP_EXPECTED_WORDS = ("system", "online", "ready")

# A synthesised sentence of this length is silence or a stub, not speech.
_MIN_SECONDS = 0.4
# float32 samples: a peak below this is an audible nothing.
_MIN_PEAK = 0.01


def _synthesise_to_wav(path) -> tuple:
    """Run the real Kokoro model and write a real WAV. Returns
    (seconds, peak, bytes_written)."""
    from app.voice import audio
    from app.voice.kokoro import assets, engine, install

    voice_key = assets.DEFAULT_VOICE_KEY
    if not install.is_installed(voice_key):
        raise RuntimeError(
            "the neural voice model is not installed on this machine, so no audio "
            "could be produced (install it from the Voice page first)"
        )

    samples = engine.engine.synthesise_all(DEEP_PHRASE, voice_key)
    count = int(getattr(samples, "size", 0))
    if count == 0:
        raise RuntimeError("the model produced no samples at all")

    seconds = count / float(engine.SAMPLE_RATE)
    peak = float(abs(samples).max())
    audio.write_wav(path, samples, engine.SAMPLE_RATE)
    written = path.stat().st_size
    return seconds, peak, written


def _check_neural_speech_produces_audio() -> str:
    """A real WAV from the real model, in this process.

    The check the release candidate never had: every voice test up to
    this point measured the text normaliser and the grapheme-to-phoneme
    stage, both of which can be perfect while the thing that makes the
    sound does nothing.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="jarvis_selftest_") as directory:
        wav = Path(directory) / "selftest.wav"
        seconds, peak, written = _synthesise_to_wav(wav)

        if seconds < _MIN_SECONDS:
            raise RuntimeError(f"only {seconds:.2f}s of audio was produced")
        if peak < _MIN_PEAK:
            raise RuntimeError(f"the audio is silent (peak amplitude {peak:.5f})")
        if written < 1024:
            raise RuntimeError(f"the WAV file is {written} bytes, which is not audio")

        return f"{seconds:.2f}s, peak {peak:.3f}, {written} bytes of WAV"


def _check_transcription_of_real_audio() -> str:
    """Speak a sentence, then listen to it — both models, one pipeline.

    Uses the audio this build just generated rather than a fixture
    committed to the repository, so a passing result means the two halves
    of the voice system work *together* in the installed artifact.
    """
    import tempfile
    from pathlib import Path

    from app.voice.stt import stt_service

    runtime_ready, runtime_detail = stt_service.runtime_status()
    if not runtime_ready:
        raise RuntimeError(runtime_detail)
    model_ready, model_detail = stt_service.model_status()
    if not model_ready:
        raise RuntimeError(model_detail)

    with tempfile.TemporaryDirectory(prefix="jarvis_selftest_") as directory:
        wav = Path(directory) / "selftest.wav"
        _synthesise_to_wav(wav)

        result = stt_service.transcribe(wav, timeout_seconds=180.0)
        if not result.success:
            raise RuntimeError(result.message)

        heard = result.text.lower()
        missing = [word for word in DEEP_EXPECTED_WORDS if word not in heard]
        if missing:
            raise RuntimeError(
                f"transcribed {result.text!r}, which is missing {', '.join(missing)}"
            )
        return f"heard {result.text.strip()!r}"


_DEEP_CHECKS = (
    ("Neural voice produces real audio", True, _check_neural_speech_produces_audio),
    ("Real audio is transcribed back", True, _check_transcription_of_real_audio),
)


# required=True means "the product claims this capability unconditionally".
# The two optional entries are genuinely optional: the classic speech tier
# is a last resort, and Windows natural voices depend on the machine.
_CHECKS = (
    ("Speech recognition (faster-whisper)", True, _check_speech_recognition),
    ("Audio decoding (av)", True, _check_audio_decoder),
    ("Speech inference (ctranslate2)", True, _check_inference_engine),
    ("Tokenizers", True, _check_tokenizers),
    ("Neural voice runtime (onnxruntime)", True, _check_neural_voice_runtime),
    ("Voice pronunciation lexicon", True, _check_voice_pronunciation),
    ("Credential store (keyring)", True, _check_credential_store),
    ("Native window (pywebview)", True, _check_native_window),
    ("Cloud voice transport (httpx/httpcore/openai_tts)", True, _check_cloud_voice_transport),
    ("Windows WAV playback (winsound)", True, _check_windows_wav_playback),
    ("Classic speech (pyttsx3)", False, _check_classic_speech),
    ("Windows natural voices (WinRT)", False, _check_windows_natural_voices),
)


def run(argv: Optional[List[str]] = None) -> int:
    deep = "--deep" in (argv or [])

    checks = list(_CHECKS)
    if deep:
        checks += list(_DEEP_CHECKS)

    results = [_probe(name, required, check) for name, required, check in checks]
    blocking = [result for result in results if result.blocking]

    frozen = getattr(sys, "frozen", False)
    mode = "deep" if deep else "imports only"
    say(f"JARVIS self-test  ({mode}, frozen={frozen}, {platform.system()} {platform.machine()})")
    say("-" * 72)
    for result in results:
        if result.ok:
            mark = "OK      "
        elif result.required:
            mark = "FAILED  "
        else:
            mark = "absent  "
        say(f"  {mark} {result.name}: {result.detail}")
    if not deep:
        # Named rather than omitted: a check nobody ran must never be
        # mistaken later for a check that passed.
        for name, _required, _check in _DEEP_CHECKS:
            say(f"  skipped  {name}: not run (pass --deep, with both models installed)")
    say("-" * 72)

    summary = {
        "frozen": bool(frozen),
        "deep": deep,
        "ok": not blocking,
        "capabilities": [asdict(result) for result in results],
        "skipped": [] if deep else [name for name, _r, _c in _DEEP_CHECKS],
    }
    say("SELFTEST_JSON " + json.dumps(summary, separators=(",", ":")))

    if blocking:
        say(
            f"\nSELF-TEST FAILED: {len(blocking)} required capability/capabilities could not "
            "load in this build.",
        )
        for result in blocking:
            say(f"  - {result.name}: [{result.error_type}] {result.detail}")
        return 1

    say("\nSELF-TEST PASSED: every required capability loaded.")
    return 0
