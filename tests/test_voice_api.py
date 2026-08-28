"""The voice endpoints: status, selection, installation, testing, and
custom pronunciations.

Nothing here downloads anything or plays anything. The installer is
driven through its state object rather than the network, and speaking is
patched at the engine chain — the boundary below which the tests in
test_kokoro_engine.py already cover the real thing.
"""

from unittest.mock import patch

import pytest


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session

    with TestClient(jarvis_app, raise_server_exceptions=True) as test_client:
        yield prime_session(test_client)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def test_engine_status_names_every_tier_and_marks_at_most_one_active(client):
    body = client.get("/voice/engine-status").json()

    assert [tier["key"] for tier in body["engines"]] == ["kokoro", "windows", "sapi5"]
    assert sum(1 for tier in body["engines"] if tier["active"]) <= 1
    for tier in body["engines"]:
        assert tier["detail"].strip(), f"{tier['key']} gave no reason"


def test_engine_status_lists_the_selectable_voices(client):
    body = client.get("/voice/engine-status").json()

    assert len(body["voices"]) >= 1
    assert all("installed" in voice for voice in body["voices"])
    assert body["voice_key"] in {voice["key"] for voice in body["voices"]}


def test_voice_status_reports_the_engine_that_is_actually_speaking(client):
    """It used to report a fixed configured string, which would have
    named the robotic engine while the neural voice was talking."""
    from app.voice import engines

    body = client.get("/voice/status").json()

    assert body["tts_engine"] in engines.DISPLAY_NAMES.values()


# ---------------------------------------------------------------------------
# Choosing
# ---------------------------------------------------------------------------

def test_selecting_a_voice_is_remembered(client):
    body = client.post("/voice/select", json={"voice_key": "bm_daniel"}).json()

    assert body["voice_key"] == "bm_daniel"
    assert client.get("/voice/engine-status").json()["voice_key"] == "bm_daniel"


def test_selecting_an_unknown_voice_changes_nothing(client):
    client.post("/voice/select", json={"voice_key": "bm_lewis"})

    body = client.post("/voice/select", json={"voice_key": "../../etc/passwd"}).json()

    assert body["voice_key"] == "bm_lewis"


def test_speed_is_clamped_to_something_speakable(client):
    from app.voice.kokoro.engine import MAX_SPEED, MIN_SPEED

    assert client.post("/voice/speed", json={"speed": 99}).json()["speed"] == MAX_SPEED
    assert client.post("/voice/speed", json={"speed": 0}).json()["speed"] == MIN_SPEED
    client.post("/voice/speed", json={"speed": 1.0})


def test_choosing_a_voice_requires_the_session_token(client):
    from app.api.session import HEADER_NAME

    response = client.post(
        "/voice/select", json={"voice_key": "bm_george"}, headers={HEADER_NAME: "wrong"},
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------

def test_the_install_preview_says_what_would_be_downloaded_before_anything_is(client):
    body = client.get("/voice/install-preview?voice_key=bm_george").json()

    assert body["licence"] == "Apache-2.0"
    assert body["source"].startswith("https://huggingface.co/")
    assert body["destination"]
    # Either it is installed, or the preview names the cost of installing it.
    assert body["already_installed"] or body["download_bytes"] > 0


def test_the_install_preview_names_a_licence_for_every_component(client):
    for entry in client.get("/voice/licences").json():
        assert entry["licence"].strip()
        assert entry["source"].startswith("http")


def test_the_cmu_entry_is_not_given_an_spdx_label(client):
    """Its licence is CMU's own text. Calling it BSD-2-Clause buys a
    small convenience with a factual misstatement."""
    entry = next(
        item for item in client.get("/voice/licences").json() if "CMU" in item["component"]
    )

    assert "BSD" not in entry["licence"]
    assert "Carnegie Mellon" in entry["acknowledgement"]


def test_install_status_is_reported_without_starting_one(client):
    body = client.get("/voice/install-status").json()

    assert body["status"] in {
        "idle", "downloading", "verifying", "installing", "complete", "error", "cancelled",
    }
    assert 0 <= body["percent"] <= 100


def test_cancelling_an_install_is_safe_when_none_is_running(client):
    assert client.post("/voice/install/cancel", json={}).status_code == 200


def test_installing_requires_the_session_token(client):
    from app.api.session import HEADER_NAME

    response = client.post(
        "/voice/install", json={"voice_key": "bm_george"}, headers={HEADER_NAME: "wrong"},
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Testing the voice
# ---------------------------------------------------------------------------

def test_test_voice_speaks_even_when_spoken_replies_are_off(client):
    """Pressing a button labelled Test Voice is a direct request to hear
    it. The reply gate exists to stop a stale page making JARVIS talk on
    its own, which is a different thing."""
    from app.voice import engines
    from app.voice.tts import tts_service

    tts_service.set_output_enabled(False)
    # `speak_local`, not `speak`: Test Voice compares the *local* voice, so
    # the cloud pass routed it past provider selection deliberately — a
    # button labelled Test Voice must never become a billable cloud
    # request. Patching `speak` as well proves that is what happens.
    with patch.object(
        engines, "speak_local",
        return_value=engines.SpeakOutcome(started=True, engine="kokoro", message="Speaking."),
    ) as spoken, patch.object(engines, "speak") as cloud_capable:
        body = client.post("/voice/test", json={}).json()

    assert body["success"] is True
    assert spoken.called
    cloud_capable.assert_not_called()


def test_test_voice_reports_the_engine_it_used(client):
    from app.voice import engines

    with patch.object(
        engines, "speak_local",
        return_value=engines.SpeakOutcome(started=True, engine="sapi5", message="Speaking."),
    ):
        assert client.post("/voice/test", json={}).json()["engine"] == "sapi5"


def test_a_failed_test_explains_itself(client):
    from app.voice import engines

    with patch.object(
        engines, "speak_local",
        return_value=engines.SpeakOutcome(started=False, engine="none", message="Not installed."),
    ):
        body = client.post("/voice/test", json={}).json()

    assert body["success"] is False
    assert body["message"] == "Not installed."


# ---------------------------------------------------------------------------
# Custom pronunciations
# ---------------------------------------------------------------------------

def test_a_pronunciation_can_be_previewed_before_it_is_saved(client):
    body = client.post(
        "/voice/pronunciations/preview", json={"word": "Dado", "spoken_as": "dah-doh"},
    ).json()

    assert body["success"] is True
    assert body["phonemes"]
    assert client.get("/voice/pronunciations").json()["entries"] == []


def test_a_pronunciation_is_saved_and_listed(client):
    client.post("/voice/pronunciations", json={"word": "Dado", "spoken_as": "dah-doh"})

    entries = client.get("/voice/pronunciations").json()["entries"]

    assert [entry["word"] for entry in entries] == ["dado"]
    assert entries[0]["input"] == "dah-doh"
    assert entries[0]["phonemes"]


def test_a_saved_pronunciation_is_what_the_synthesiser_uses():
    """The point of the feature: the stored entry has to reach the
    pronunciation path, not merely be persisted."""
    from app.voice import pronunciations

    pronunciations.save_entry("Dado", "dah-doh")
    dictionary = pronunciations.load()

    assert dictionary.get("dado")
    assert dictionary.get("dado") == pronunciations.get_entry("dado").phonemes


def test_a_pronunciation_can_be_removed(client):
    client.post("/voice/pronunciations", json={"word": "Dado", "spoken_as": "dah-doh"})
    client.post("/voice/pronunciations/remove", json={"word": "Dado", "spoken_as": ""})

    assert client.get("/voice/pronunciations").json()["entries"] == []


def test_a_pronunciation_with_unusable_symbols_is_refused_where_it_can_be_corrected(client):
    """Refused at entry, not at the point of speech — where a person can
    do something about it."""
    body = client.post(
        "/voice/pronunciations/preview", json={"word": "Dado", "spoken_as": "/dadøπ/"},
    ).json()

    assert body["success"] is False
    assert "cannot say" in body["message"]


def test_the_page_knows_whether_the_first_run_name_would_be_spelled_out(client):
    """Offered, not guessed. A name JARVIS cannot pronounce gets the
    setting surfaced; proposing a pronunciation nobody asked for would
    be inventing how someone's name sounds."""
    from app.core.preferences import store

    store("preferred_name", "Vukoje")
    client.post("/voice/pronunciations/remove", json={"word": "Vukoje", "spoken_as": ""})

    body = client.get("/voice/pronunciations").json()

    assert body["preferred_name"] == "Vukoje"
    assert body["name_needs_pronunciation"] is True

    client.post("/voice/pronunciations", json={"word": "Vukoje", "spoken_as": "voo-koy-eh"})
    assert client.get("/voice/pronunciations").json()["name_needs_pronunciation"] is False


def test_a_name_jarvis_already_says_correctly_is_not_asked_about(client):
    """The owner's own name is in the built-in dictionary, so the app
    must not offer to fix something that is not broken."""
    from app.core.preferences import store

    store("preferred_name", "Dado")
    client.post("/voice/pronunciations/remove", json={"word": "Dado", "spoken_as": ""})

    assert client.get("/voice/pronunciations").json()["name_needs_pronunciation"] is False


def test_the_pronunciation_store_is_isolated_from_the_real_one():
    """This suite's own guard rail, added because it was missing.

    Pronunciations are saved beside the preferences file in the user's
    real AppData. The autouse fixture in conftest redirected only the
    preferences module, which holds its own reference to config_dir — so
    these tests were writing to the developer's actual store and to each
    other's. Both halves of that were real: polluted tests, and a test
    run that edited real user data.
    """
    from app.core.app_paths import config_dir
    from app.voice import pronunciations

    assert pronunciations.store_path().parent != config_dir()


def test_a_failed_save_leaves_no_scratch_file_behind():
    """The rename is what makes the write atomic. When it does not
    happen, the half-written .json.tmp is litter in the user's config
    directory that nothing will ever come back for."""
    from unittest.mock import patch

    from app.voice import pronunciations

    store = pronunciations.store_path()
    temporary = store.with_suffix(".json.tmp")

    with patch.object(type(temporary), "replace", side_effect=OSError("disk full")):
        assert pronunciations._write_raw({"dado": {"spoken_as": "dah-doh"}}) is False

    assert not temporary.exists()


def test_saving_a_pronunciation_requires_the_session_token(client):
    from app.api.session import HEADER_NAME

    response = client.post(
        "/voice/pronunciations",
        json={"word": "x", "spoken_as": "eks"},
        headers={HEADER_NAME: "wrong"},
    )

    assert response.status_code == 403
