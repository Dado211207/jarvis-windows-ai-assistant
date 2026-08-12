"""Ten situations, ten things to do about them.

The reported diagnostics panel from the installed release candidate:

    Microphone permission   Not asked yet
    Input device            1 detected
    Speech runtime          Not installed
    Installed model         Not installed
    Model location          No model installed
    Last check              Not run yet

Every row was true. The only advice anywhere on the page was
"Reinstalling JARVIS should restore it", which would not have helped,
because the fault was in what the installer bundled. Six accurate rows
and no diagnosis is not a diagnosis.

Nothing here opens a microphone or plays audio.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.voice import input_state


@pytest.fixture(autouse=True)
def no_remembered_failure():
    input_state.last_failure.clear()
    yield
    input_state.last_failure.clear()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session

    with TestClient(jarvis_app, raise_server_exceptions=True) as test_client:
        yield prime_session(test_client)


def _installer(status="idle", downloaded=0, total=0, message=""):
    state = MagicMock()
    state.status = status
    state.bytes_downloaded = downloaded
    state.bytes_total = total
    state.message = message
    return patch("app.voice.model_installer.model_installer.state", return_value=state)


def _stt(runtime=(True, "ok"), model=(True, "ok"), enabled=True):
    from app.voice.stt import stt_service

    return (
        patch.object(stt_service, "runtime_status", return_value=runtime),
        patch.object(stt_service, "model_status", return_value=model),
        patch("app.voice.stt.input_enabled", return_value=enabled),
    )


def _describe(**kwargs):
    runtime_patch, model_patch, enabled_patch = _stt(**kwargs)
    with runtime_patch, model_patch, enabled_patch:
        return input_state.describe()


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

def test_there_are_exactly_ten_states():
    assert len(input_state.ALL_STATES) == 10
    assert len(set(input_state.ALL_STATES)) == 10


def test_the_three_the_server_cannot_see_are_named_as_such():
    """A refused microphone permission is a fact about the page, not the
    machine. Naming them here keeps the two halves speaking one
    vocabulary instead of two that drift."""
    for state in input_state.BROWSER_STATES:
        assert state in input_state.ALL_STATES

    assert input_state.PERMISSION_DENIED in input_state.BROWSER_STATES
    assert input_state.NO_INPUT_DEVICE in input_state.BROWSER_STATES


# ---------------------------------------------------------------------------
# Each state, and the fact that each has something to do about it
# ---------------------------------------------------------------------------

def test_a_missing_runtime_is_a_broken_install_not_a_switch():
    """The exact sentence the release candidate showed, now with the
    honest reading attached: this part ships inside JARVIS, so it is not
    something a user can turn on."""
    with _installer():
        state = _describe(runtime=(False, "The local speech engine isn't available in this installation."))

    assert state.state == input_state.RUNTIME_MISSING
    assert "broken installation" in state.next_step


def test_a_missing_model_points_at_the_download_not_a_reinstall():
    """The distinction the panel never drew. A model is missing because
    nobody has downloaded it yet, which reinstalling does not change."""
    with _installer():
        state = _describe(model=(False, "No speech model installed."))

    assert state.state == input_state.MODEL_MISSING
    assert "Download" in state.next_step
    assert "einstall" not in state.next_step


def test_a_download_in_progress_is_its_own_state_with_progress():
    with _installer(status="downloading", downloaded=25, total=100, message="Downloading model.bin…"):
        state = _describe()

    assert state.state == input_state.DOWNLOADING
    assert state.percent == 25
    assert state.busy is True


def test_verifying_is_visible_rather_than_looking_like_a_stall():
    with _installer(status="verifying", downloaded=100, total=100, message="Verifying model.bin…"):
        state = _describe()

    assert state.state == input_state.VERIFYING
    assert "checksums" in state.next_step


def test_everything_installed_but_switched_off_is_not_a_fault():
    with _installer():
        state = _describe(enabled=False)

    assert state.state == input_state.DISABLED
    assert "switch is off" in state.detail


def test_a_failed_transcription_is_remembered_and_named():
    """Otherwise push-to-talk looks like a button that simply does
    nothing."""
    input_state.last_failure.record("Transcription timed out after 30 seconds.")
    with _installer():
        state = _describe()

    assert state.state == input_state.TRANSCRIPTION_FAILED
    assert "timed out" in state.detail


def test_a_successful_recording_clears_the_remembered_failure():
    input_state.last_failure.record("something went wrong")
    input_state.last_failure.clear()
    with _installer():
        state = _describe()

    assert state.state == input_state.READY


def test_ready_says_how_to_use_it():
    with _installer():
        state = _describe()

    assert state.state == input_state.READY
    assert state.ready is True
    assert "microphone button" in state.next_step


def test_every_state_offers_something_to_do():
    """The whole point. A state with nothing to do about it is what the
    release candidate shipped."""
    cases = [
        dict(runtime=(False, "gone")),
        dict(model=(False, "gone")),
        dict(enabled=False),
        {},
    ]
    for case in cases:
        with _installer():
            state = _describe(**case)
        assert state.headline.strip()
        assert state.next_step.strip(), f"{state.state} offered no next step"


def test_the_state_is_never_an_exception():
    """This is read on every Voice page render. It failing would take the
    page with it."""
    with patch.object(input_state, "_describe", side_effect=OSError("boom")):
        state = input_state.describe()

    assert state.state in input_state.ALL_STATES
    assert state.next_step.strip()


# ---------------------------------------------------------------------------
# Only the message is remembered — never what was said
# ---------------------------------------------------------------------------

def test_only_a_failure_message_is_kept_never_audio_or_a_transcript():
    import inspect

    source = inspect.getsource(input_state._LastFailure)

    for forbidden in ("text", "transcript", "audio", "path"):
        assert forbidden not in source.split('"""')[2], (
            f"the remembered failure must not carry {forbidden}"
        )


def test_a_remembered_message_is_bounded():
    input_state.last_failure.record("x" * 5000)

    assert len(input_state.last_failure.message()) <= 300


# ---------------------------------------------------------------------------
# The endpoint and the page
# ---------------------------------------------------------------------------

def test_the_diagnostics_endpoint_carries_the_state_and_the_step(client):
    body = client.get("/voice/diagnostics").json()

    assert body["state"] in input_state.ALL_STATES
    assert body["headline"].strip()
    assert body["next_step"].strip()


def test_the_endpoint_still_carries_the_individual_rows(client):
    """The overall state is the answer; the rows are the working. Both
    are needed — the rows are what somebody reads out over the phone."""
    body = client.get("/voice/diagnostics").json()

    for field in ("runtime_ready", "runtime_detail", "model_ready", "model_detail", "model_path"):
        assert field in body


def test_a_failed_transcription_reaches_the_diagnostics_endpoint(client):
    input_state.last_failure.record("The recording was too quiet to transcribe.")

    body = client.get("/voice/diagnostics").json()

    assert body["last_failure"] == "The recording was too quiet to transcribe."


def test_the_page_shows_one_state_and_the_step(client):
    html = client.get("/ui/voice").text

    assert 'id="diag-state"' in html
    assert 'id="diag-next-step"' in html
    assert "Run diagnostics again" in html


def test_the_heading_reads_correctly(client):
    """The physical test reported it as "oice diagnostics". The template
    is correct; this pins it so a future edit cannot make the report
    true."""
    html = client.get("/ui/voice").text

    assert "Voice diagnostics" in html


def test_the_browser_half_uses_the_same_state_names():
    """Two vocabularies for one panel is how it ends up showing two
    disagreeing answers."""
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    labels = js.split("const VOICE_INPUT_LABELS = {", 1)[1].split("};", 1)[0]

    for state in input_state.ALL_STATES:
        assert state in labels, f"the page has no label for {state}"


def test_text_chat_is_never_blocked_by_voice_input(client):
    """Voice setup failing must leave typing exactly as it was."""
    from app.voice.stt import stt_service

    with patch.object(stt_service, "runtime_status", return_value=(False, "gone")), \
         patch.object(stt_service, "model_status", return_value=(False, "gone")):
        assert client.get("/voice/diagnostics").json()["state"] == input_state.RUNTIME_MISSING
        assert client.post("/command", json={"command": "status"}).status_code == 200
