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
that changed state would be a worse diagnostic. Nothing here touches the
network, loads a model, or opens an audio device.
"""

import json
import platform
import sys
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional


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


def _check_windows_natural_voices() -> str:
    from app.voice import winrt_voices

    if not winrt_voices.projection_available():
        raise RuntimeError("the WinRT speech projection is not available in this build")
    return f"{len(winrt_voices.list_voices())} Windows voice(s)"


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
    ("Classic speech (pyttsx3)", False, _check_classic_speech),
    ("Windows natural voices (WinRT)", False, _check_windows_natural_voices),
)


def run(argv: Optional[List[str]] = None) -> int:
    results = [_probe(name, required, check) for name, required, check in _CHECKS]
    blocking = [result for result in results if result.blocking]

    frozen = getattr(sys, "frozen", False)
    print(f"JARVIS self-test  (frozen={frozen}, {platform.system()} {platform.machine()})")
    print("-" * 72)
    for result in results:
        if result.ok:
            mark = "OK      "
        elif result.required:
            mark = "FAILED  "
        else:
            mark = "absent  "
        print(f"  {mark} {result.name}: {result.detail}")
    print("-" * 72)

    summary = {
        "frozen": bool(frozen),
        "ok": not blocking,
        "capabilities": [asdict(result) for result in results],
    }
    print("SELFTEST_JSON " + json.dumps(summary, separators=(",", ":")))

    if blocking:
        print(
            f"\nSELF-TEST FAILED: {len(blocking)} required capability/capabilities could not "
            "load in this build.",
        )
        for result in blocking:
            print(f"  - {result.name}: [{result.error_type}] {result.detail}")
        return 1

    print("\nSELF-TEST PASSED: every required capability loaded.")
    return 0
