"""The installed application proving its own runtime.

The release candidate shipped with no speech input while every automated
check passed, because every check imported `faster_whisper` in the
source tree — where pip had installed it and it imports fine. The frozen
build raised ImportError and told the user to reinstall the identical
file.

These tests cover the diagnostic that closes that gap: that it reports
rather than repairs, that a required failure is fatal and an optional one
is not, and that it cannot itself crash while reporting.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.launcher import selftest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_a_failing_required_capability_is_fatal():
    result = selftest._probe("thing", required=True, check=lambda: (_ for _ in ()).throw(ImportError("no")))

    assert result.ok is False
    assert result.blocking is True


def test_a_failing_optional_capability_is_not_fatal():
    """The classic speech tier and Windows natural voices genuinely
    depend on the machine. Treating those as build failures would make
    the check cry wolf."""
    result = selftest._probe("thing", required=False, check=lambda: (_ for _ in ()).throw(ImportError("no")))

    assert result.ok is False
    assert result.blocking is False


def test_the_probe_survives_a_failure_that_is_not_an_exception():
    """A native extension that cannot find its DLLs can fail in ways that
    are not Exception subclasses — keyring's Rust binding raises
    PanicException. A diagnostic that dies while diagnosing is worse
    than none."""
    class _NotAnException(BaseException):
        pass

    result = selftest._probe(
        "thing", required=True, check=lambda: (_ for _ in ()).throw(_NotAnException("boom")),
    )

    assert result.ok is False
    assert result.error_type == "_NotAnException"


def test_the_error_type_is_recorded_so_import_and_dll_failures_differ():
    """ImportError means the module was not bundled. OSError usually
    means it was, and its native dependency was not. Those need
    different fixes, so the type is kept."""
    result = selftest._probe("thing", required=True, check=lambda: (_ for _ in ()).throw(OSError("dll")))

    assert result.error_type == "OSError"


def test_speech_recognition_imports_the_submodule_that_needs_the_audio_decoder():
    """`import faster_whisper` alone would not have caught this: the
    package imports `faster_whisper.audio`, and that is what pulls in
    `av`. Checking only the top-level name would reproduce the original
    blind spot."""
    import inspect

    source = inspect.getsource(selftest._check_speech_recognition)

    assert "faster_whisper.audio" in source


def test_every_capability_the_product_claims_unconditionally_is_required():
    """Anything the UI presents as simply present must be required here,
    or the self-test cannot catch it going missing."""
    required = {name for name, is_required, _ in selftest._CHECKS if is_required}

    for expected in (
        "Speech recognition (faster-whisper)",
        "Audio decoding (av)",
        "Speech inference (ctranslate2)",
        "Neural voice runtime (onnxruntime)",
        "Voice pronunciation lexicon",
        "Native window (pywebview)",
    ):
        assert expected in required, f"{expected} must be a required capability"


def test_it_runs_as_a_subcommand_of_the_real_entry_point():
    """It has to be reachable as `JARVIS.exe --selftest`, because the
    installed executable is the only thing whose runtime is in question."""
    entry = (REPO_ROOT / "run_jarvis.py").read_text(encoding="utf-8")

    assert "--selftest" in entry
    assert "selftest" in entry


def test_running_it_prints_machine_readable_output_and_never_raises():
    """The clean-install test parses this. It must produce a result even
    when — especially when — capabilities are missing, which is exactly
    the situation on this Linux source tree."""
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "run_jarvis.py"), "--selftest"],
        capture_output=True, text=True, timeout=180,
    )
    output = completed.stdout

    assert "SELFTEST_JSON " in output
    payload = json.loads(output.split("SELFTEST_JSON ", 1)[1].splitlines()[0])
    assert isinstance(payload["capabilities"], list)
    assert payload["capabilities"], "expected at least one capability to be reported"
    # Exit code must agree with the summary rather than being independent.
    assert (completed.returncode == 0) is bool(payload["ok"])


def test_the_clean_install_test_runs_the_selftest_against_the_installed_exe():
    """A self-test nothing runs is a comment."""
    script = (REPO_ROOT / "scripts" / "test_clean_install.py").read_text(encoding="utf-8")

    assert "--selftest" in script
    assert "phase_f_installed_runtime_selftest" in script
    assert "SELF-TEST PASSED" in script, (
        "an exit code alone is not enough — an ambiguous pass must be treated as failure"
    )


# ---------------------------------------------------------------------------
# The deep pass: real audio out, real words back
# ---------------------------------------------------------------------------

def test_the_deep_checks_run_the_real_model_not_a_stage_of_it():
    """Every voice test before this one measured the text normaliser or
    the grapheme-to-phoneme conversion. Both can be perfect while the
    part that makes the sound does nothing."""
    import inspect

    source = inspect.getsource(selftest._check_neural_speech_produces_audio)

    assert "_synthesise_to_wav" in source
    assert "silent" in source, "silence must be caught, not counted as audio"


def test_silence_is_a_failure_not_a_pass():
    assert selftest._MIN_PEAK > 0
    assert selftest._MIN_SECONDS > 0


def test_the_transcription_check_reads_back_what_this_build_just_spoke():
    """Using a fixture committed to the repository would prove the
    recogniser works. Using the audio this build just generated proves
    the two halves work together in the installed artifact."""
    import inspect

    source = inspect.getsource(selftest._check_transcription_of_real_audio)

    assert "_synthesise_to_wav" in source
    assert "transcribe" in source


def test_both_deep_checks_are_required_when_they_run():
    for _name, required, _check in selftest._DEEP_CHECKS:
        assert required is True


def test_the_deep_checks_are_not_run_by_default_and_say_so():
    """A check nobody ran must never be mistaken later for a check that
    passed."""
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "run_jarvis.py"), "--selftest"],
        capture_output=True, text=True, timeout=180,
    )
    payload = json.loads(completed.stdout.split("SELFTEST_JSON ", 1)[1].splitlines()[0])

    assert payload["deep"] is False
    assert len(payload["skipped"]) == len(selftest._DEEP_CHECKS)
    assert "skipped" in completed.stdout


def test_the_neural_voice_really_produces_audio_here():
    """Run for real, in this process, against the real Kokoro model.

    Skipped only when the model is not installed on the machine running
    the tests — a genuine absence, not a failure being converted into
    one. The same check runs against the installed .exe in CI, where the
    model is downloaded first through the app's own screens.
    """
    from app.voice.kokoro import assets, install

    if not install.is_installed(assets.DEFAULT_VOICE_KEY):
        pytest.skip("the neural voice model is not installed on this machine")

    detail = selftest._check_neural_speech_produces_audio()

    assert "peak" in detail
    assert "bytes of WAV" in detail


def test_no_test_module_uses_pytest_without_importing_it():
    """A guard for the defect that produced this test's own CI failure.

    `test_the_neural_voice_really_produces_audio_here` calls
    `pytest.skip()` when the neural voice model is absent. On the
    development machine the model is installed, so that branch never
    ran; on a clean runner it ran and raised `NameError: name 'pytest'
    is not defined` instead of skipping — turning a legitimate skip into
    a red suite and a failed installer build.

    The general shape is what matters: a branch only reachable in an
    environment nobody develops in. This is the cheapest possible check
    for the specific instance of it, run across every test module rather
    than only the one that was caught.
    """
    import re

    offenders = []
    for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        uses = re.search(r"^\s*(?!#).*\bpytest\.", source, re.MULTILINE)
        imports = re.search(r"^\s*(?:import pytest\b|from pytest import)", source, re.MULTILINE)
        if uses and not imports:
            offenders.append(path.name)

    assert offenders == [], f"these modules use pytest.* without importing it: {offenders}"


def test_the_clean_install_test_runs_the_deep_pass_against_the_installed_exe():
    """The whole point: the audio has to be produced by the artifact the
    user was sent, not by the source tree."""
    script = (REPO_ROOT / "scripts" / "test_clean_install.py").read_text(encoding="utf-8")

    assert "phase_g_real_voice_through_the_installed_product" in script
    assert "deep=True" in script
    assert "/voice/install" in script, "the models must be installed through the app's own screens"
    assert "/onboarding/speech-model/install" in script
