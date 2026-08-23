"""The optional cloud voice, tested against a mock transport.

**No test here reaches the real ElevenLabs API**, and none may ever be
allowed to: a live call would need somebody's key, spend their credits,
and make the suite depend on a third party's uptime. What is mocked is
the *transport* — `httpx.MockTransport` — not the client. The real
`httpx.Client` is constructed by the real `_client()`, so
`follow_redirects=False`, the base URL, the headers and the timeouts are
all the ones the product uses. Mocking `_call()` instead would have
tested nothing.

Every key in this file is synthetic and obviously so.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.voice import elevenlabs
from tests.conftest import prime_session

FAKE_KEY = "sk_FAKE0000NOTREAL1111EXAMPLE2222abcdefabcdefabcdefabcdef"
VOICE_ID = "FAKEvoiceid0000NOTREAL"

# A minimal but real RIFF/WAV header, so the "is this actually audio"
# check is exercised with something that genuinely is.
WAV_BYTES = (
    b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt "
    + (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + (1).to_bytes(2, "little")
    + (24000).to_bytes(4, "little") + (48000).to_bytes(4, "little")
    + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    + b"data" + (0).to_bytes(4, "little")
)


def _transport(handler):
    """Install a mock transport into the real client factory."""
    def _factory(timeout_read):
        return httpx.Client(
            base_url=elevenlabs.API_BASE,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_read, connect=elevenlabs.CONNECT_TIMEOUT_SECONDS),
            transport=httpx.MockTransport(handler),
        )
    return _factory


@pytest.fixture
def mock_api(monkeypatch):
    """Returns a setter: give it a handler, get the recorded requests."""
    recorded = []

    def install(handler):
        def _recording(request):
            recorded.append(request)
            return handler(request)
        monkeypatch.setattr(elevenlabs, "_client", _transport(_recording))
        return recorded

    return install


def _ok_audio(request):
    return httpx.Response(200, content=WAV_BYTES, headers={"content-type": "audio/wav"})


# ---------------------------------------------------------------------------
# The request that goes out
# ---------------------------------------------------------------------------

def test_a_synthesis_request_goes_to_the_pinned_host_over_https(mock_api):
    recorded = mock_api(_ok_audio)

    elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY)

    request = recorded[0]
    assert request.url.host == elevenlabs.API_HOST
    assert request.url.scheme == "https"
    assert request.url.path == f"/v1/text-to-speech/{VOICE_ID}"


def test_the_key_travels_in_a_header_and_never_in_the_url(mock_api):
    recorded = mock_api(_ok_audio)

    elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY)

    request = recorded[0]
    assert request.headers[elevenlabs.AUTH_HEADER] == FAKE_KEY
    assert FAKE_KEY not in str(request.url)
    assert FAKE_KEY not in request.url.query.decode()


def test_the_request_asks_for_wav_because_that_is_what_playback_accepts(mock_api):
    """winsound plays PCM WAV and nothing else. Asking for MP3 would
    produce bytes nothing in this application can play."""
    recorded = mock_api(_ok_audio)

    elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY)

    body = json.loads(recorded[0].content)
    assert body["output_format"] == "wav_24000"
    assert body["output_format"].startswith("wav_")


def test_the_default_output_format_is_not_one_that_needs_a_paid_tier():
    """ElevenLabs documents 44.1 kHz PCM and WAV as Pro-tier only.
    Defaulting to one would turn a working account into a 401."""
    assert "44100" not in elevenlabs.OUTPUT_FORMAT


def test_speed_is_sent_for_a_model_that_supports_it(mock_api):
    recorded = mock_api(_ok_audio)

    elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY)

    settings = json.loads(recorded[0].content)["voice_settings"]
    assert "speed" in settings


@pytest.mark.parametrize("model", elevenlabs.MODELS_WITHOUT_SPEED)
def test_speed_is_omitted_for_models_that_do_not_accept_it(mock_api, model):
    """Documented: speed is not available for Eleven v3. Sending it to be
    ignored would be a claim the product cannot keep."""
    recorded = mock_api(_ok_audio)

    elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY, model_id=model)

    assert "speed" not in json.loads(recorded[0].content)["voice_settings"]


# ---------------------------------------------------------------------------
# Network security
# ---------------------------------------------------------------------------

def test_a_redirect_is_refused_and_never_followed(mock_api):
    """A pinned host that follows a redirect is not a pinned host."""
    recorded = mock_api(lambda request: httpx.Response(
        302, headers={"location": "https://evil.example.com/audio.wav"},
    ))

    with pytest.raises(elevenlabs.ElevenLabsError) as caught:
        elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY)

    assert caught.value.category == elevenlabs.BAD_RESPONSE
    assert len(recorded) == 1, "the redirect was followed"
    assert all(r.url.host == elevenlabs.API_HOST for r in recorded)


def test_a_non_audio_content_type_is_refused(mock_api):
    """Otherwise an HTML error page would be handed to the audio player."""
    mock_api(lambda request: httpx.Response(
        200, content=b"<html>not audio</html>", headers={"content-type": "text/html"},
    ))

    with pytest.raises(elevenlabs.ElevenLabsError) as caught:
        elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY)
    assert caught.value.category == elevenlabs.BAD_RESPONSE


def test_audio_that_is_not_actually_a_wav_is_refused(mock_api):
    mock_api(lambda request: httpx.Response(
        200, content=b"ID3 this is an mp3", headers={"content-type": "audio/mpeg"},
    ))

    with pytest.raises(elevenlabs.ElevenLabsError) as caught:
        elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY)
    assert caught.value.category == elevenlabs.BAD_RESPONSE


def test_an_oversized_response_is_cut_off_rather_than_read(mock_api, monkeypatch):
    monkeypatch.setattr(elevenlabs, "MAX_AUDIO_BYTES", 1024)
    mock_api(lambda request: httpx.Response(
        200, content=b"RIFF" + b"x" * 5000, headers={"content-type": "audio/wav"},
    ))

    with pytest.raises(elevenlabs.ElevenLabsError) as caught:
        elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY)
    assert caught.value.category == elevenlabs.BAD_RESPONSE


def test_cloud_audio_never_reaches_the_disk():
    """"Are the temporary files cleaned up on success, error,
    cancellation and shutdown?" has a better answer than "yes": there are
    none.

    Synthesised audio is bytes in memory from the HTTP response to
    `winsound.PlaySound(..., SND_MEMORY)`. Nothing writes a WAV anywhere,
    so there is no file to leave behind when a request fails, a person
    presses Stop, or the process is killed mid-utterance.

    `app/voice/audio.py` does contain a `write_wav()`, but nothing on the
    speech path calls it — it exists for the launcher self-test, which
    deliberately leaves a sample file behind for somebody to listen to.
    So the assertion is scoped to the two things that carry a spoken
    reply: the ElevenLabs client, and the `Player`.
    """
    import inspect

    from app.voice import audio

    sources = {
        "app.voice.elevenlabs": inspect.getsource(elevenlabs),
        "app.voice.audio.Player": inspect.getsource(audio.Player),
    }
    for name, source in sources.items():
        for forbidden in ("tempfile", "NamedTemporaryFile", "mkstemp", "wave.open",
                          "write_bytes", "os.remove", ".unlink("):
            assert forbidden not in source, (
                f"{name} touches the filesystem via {forbidden!r}"
            )

    assert "SND_MEMORY" in sources["app.voice.audio.Player"]

    # And nothing on the speech path reaches the one helper that does
    # write a file.
    from app.voice import engines
    assert "write_wav" not in inspect.getsource(engines)


def test_there_is_no_endpoint_that_fetches_an_arbitrary_audio_url():
    """An AI model that could name a URL and have JARVIS fetch it would
    be a request-forgery primitive wearing a voice."""
    import inspect

    source = inspect.getsource(elevenlabs)
    assert "API_BASE" in source
    # Every request goes through _call(), which only ever takes a path.
    assert "def _call(method: str, path: str" in source
    assert "base_url=API_BASE" in source


# ---------------------------------------------------------------------------
# Error classification — four problems, four fixes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    (401, elevenlabs.INVALID_KEY),
    (403, elevenlabs.FORBIDDEN),
    (429, elevenlabs.RATE_LIMITED),
    (500, elevenlabs.PROVIDER_ERROR),
])
def test_each_status_becomes_its_own_category(mock_api, status, expected):
    mock_api(lambda request: httpx.Response(
        status, json={"detail": "nope"}, headers={"content-type": "application/json"},
    ))

    with pytest.raises(elevenlabs.ElevenLabsError) as caught:
        elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY)
    assert caught.value.category == expected


def test_quota_exhaustion_is_told_apart_from_a_generic_failure(mock_api):
    """Reported through a validation-shaped error, which would otherwise
    read as "could not produce that audio" — true, and useless to
    somebody who has simply run out."""
    mock_api(lambda request: httpx.Response(
        422, json={"detail": "This request exceeds your quota"},
        headers={"content-type": "application/json"},
    ))

    with pytest.raises(elevenlabs.ElevenLabsError) as caught:
        elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY)
    assert caught.value.category == elevenlabs.QUOTA
    assert "quota" in caught.value.message.lower()


def test_a_timeout_is_a_timeout_and_not_an_outage(mock_api):
    def _timeout(request):
        raise httpx.ReadTimeout("too slow", request=request)

    mock_api(_timeout)
    with pytest.raises(elevenlabs.ElevenLabsError) as caught:
        elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY)
    assert caught.value.category == elevenlabs.TIMEOUT


def test_being_offline_says_so(mock_api):
    def _down(request):
        raise httpx.ConnectError("no route", request=request)

    mock_api(_down)
    with pytest.raises(elevenlabs.ElevenLabsError) as caught:
        elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY)
    assert caught.value.category == elevenlabs.OFFLINE
    assert "internet" in caught.value.message.lower()


def test_no_key_is_its_own_answer():
    with pytest.raises(elevenlabs.ElevenLabsError) as caught:
        elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key="")
    assert caught.value.category == elevenlabs.NOT_CONFIGURED


def test_every_category_has_a_message_that_says_what_to_do():
    for category, message in elevenlabs._MESSAGES.items():
        assert len(message) > 30, f"{category} has no real explanation"
        assert message[0].isupper()


# ---------------------------------------------------------------------------
# The key never leaks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403, 429, 422, 500])
def test_no_error_message_ever_contains_the_key(mock_api, status):
    mock_api(lambda request: httpx.Response(
        status, json={"detail": f"key {FAKE_KEY} rejected"},
        headers={"content-type": "application/json"},
    ))

    with pytest.raises(elevenlabs.ElevenLabsError) as caught:
        elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY)
    assert FAKE_KEY not in caught.value.message
    assert FAKE_KEY not in str(caught.value)


def test_an_upstream_error_body_is_never_echoed(mock_api):
    """The upstream payload is somebody else's text; repeating it is how
    an error message becomes an injection surface."""
    mock_api(lambda request: httpx.Response(
        400, json={"detail": "SOMETHING-FROM-UPSTREAM-VERBATIM"},
        headers={"content-type": "application/json"},
    ))

    with pytest.raises(elevenlabs.ElevenLabsError) as caught:
        elevenlabs.synthesise_wav("hello", voice_id=VOICE_ID, api_key=FAKE_KEY)
    assert "SOMETHING-FROM-UPSTREAM-VERBATIM" not in caught.value.message


def test_the_elevenlabs_key_shape_is_refused_by_the_memory_guard():
    from app.core.secret_guard import find_secret

    assert find_secret(f"my elevenlabs key is {FAKE_KEY}") is not None


def test_an_ordinary_md5_looking_string_is_not_mistaken_for_a_key():
    """The older bare-hex key form is deliberately not matched: it is
    indistinguishable from any checksum."""
    from app.core.secret_guard import find_secret

    assert find_secret("the file checksum was d41d8cd98f00b204e9800998ecf8427e") is None


# ---------------------------------------------------------------------------
# Text handling
# ---------------------------------------------------------------------------

def test_text_is_bounded_before_it_is_sent(mock_api):
    """Every character sent is a character billed."""
    recorded = mock_api(_ok_audio)

    elevenlabs.synthesise_wav("word. " * 5000, voice_id=VOICE_ID, api_key=FAKE_KEY)

    sent = json.loads(recorded[0].content)["text"]
    assert len(sent) <= elevenlabs.MAX_TEXT_CHARS


def test_a_bounded_reply_is_cut_at_a_sentence_end():
    out = elevenlabs.normalise_text("This is a sentence. " * 500)
    assert out.endswith(". ") or out.endswith(".")


def test_whitespace_is_collapsed():
    assert elevenlabs.normalise_text("  hello \n\n  world  ") == "hello world"


def test_empty_text_is_refused_without_a_request(mock_api):
    recorded = mock_api(_ok_audio)
    with pytest.raises(elevenlabs.ElevenLabsError):
        elevenlabs.synthesise_wav("   ", voice_id=VOICE_ID, api_key=FAKE_KEY)
    assert recorded == [], "an empty request should never be sent"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_settings_are_clamped_to_the_documented_ranges():
    out = elevenlabs.clamp_settings({"stability": 5.0, "speed": 9.9, "style": -3})
    assert out["stability"] == 1.0
    assert out["speed"] == 1.2      # documented maximum
    assert out["style"] == 0.0


def test_the_documented_speed_range_is_what_is_enforced():
    assert elevenlabs.SETTING_RANGES["speed"] == (0.7, 1.2)


def test_unknown_settings_are_dropped_rather_than_forwarded():
    """This object becomes a request body; passing through whatever
    arrived would make the endpoint a way to set unreviewed fields."""
    out = elevenlabs.clamp_settings({"stability": 0.5, "wildcard": "anything"})
    assert "wildcard" not in out


def test_garbage_values_fall_back_to_the_default():
    out = elevenlabs.clamp_settings({"stability": "not a number"})
    assert out["stability"] == elevenlabs.DEFAULT_SETTINGS["stability"]


def test_the_recommended_defaults_are_the_ones_the_owner_asked_for():
    assert elevenlabs.DEFAULT_SETTINGS["stability"] == 0.50
    assert elevenlabs.DEFAULT_SETTINGS["similarity_boost"] == 0.75
    assert elevenlabs.DEFAULT_SETTINGS["style"] == 0.00
    assert elevenlabs.DEFAULT_SETTINGS["use_speaker_boost"] is True
    assert elevenlabs.DEFAULT_SETTINGS["speed"] == 0.92


def test_the_test_phrase_is_the_agreed_one():
    assert elevenlabs.TEST_PHRASE == "Good evening, sir. All systems are online and ready."


# ---------------------------------------------------------------------------
# Voices
# ---------------------------------------------------------------------------

def _voices_payload(request):
    return httpx.Response(200, json={"voices": [
        {"voice_id": "a1", "name": "Reginald", "category": "premade",
         "description": "A calm British narrator", "labels": {"accent": "british"}},
        {"voice_id": "b2", "name": "Other", "category": "cloned"},
        {"voice_id": "", "name": "broken"},
        "not a dict",
    ]}, headers={"content-type": "application/json"})


def test_voices_are_listed_with_names_not_only_ids(mock_api):
    mock_api(_voices_payload)

    voices = elevenlabs.list_voices(FAKE_KEY)

    assert [v.name for v in voices] == ["Reginald", "Other"]
    assert voices[0].voice_id == "a1"


def test_malformed_voice_entries_are_skipped_not_fatal(mock_api):
    mock_api(_voices_payload)
    assert len(elevenlabs.list_voices(FAKE_KEY)) == 2


def test_the_voices_request_uses_the_documented_path(mock_api):
    recorded = mock_api(_voices_payload)
    elevenlabs.list_voices(FAKE_KEY)
    assert recorded[0].url.path == "/v2/voices"


def test_validation_uses_the_endpoint_that_spends_no_credits(mock_api):
    recorded = mock_api(lambda request: httpx.Response(
        200, json={"tier": "creator", "character_count": 100, "character_limit": 1000},
        headers={"content-type": "application/json"},
    ))

    ok, message = elevenlabs.validate_key(FAKE_KEY)

    assert ok is True
    assert recorded[0].url.path == "/v1/user/subscription"
    assert "creator" in message
    assert "900" in message


def test_validation_reports_a_bad_key_without_raising(mock_api):
    mock_api(lambda request: httpx.Response(
        401, json={"detail": "bad"}, headers={"content-type": "application/json"},
    ))

    ok, message = elevenlabs.validate_key(FAKE_KEY)

    assert ok is False
    assert FAKE_KEY not in message


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from app.api.server import app
    with TestClient(app) as test_client:
        yield prime_session(test_client)


@pytest.fixture(autouse=True)
def _no_stored_key(monkeypatch):
    """No test may read or write the developer's real credential store."""
    store = {}
    monkeypatch.setattr("app.core.credentials.get_elevenlabs_key", lambda: store.get("key", ""))
    monkeypatch.setattr("app.core.credentials.set_elevenlabs_key",
                        lambda value: store.__setitem__("key", value) or True)
    monkeypatch.setattr("app.core.credentials.clear_elevenlabs_key",
                        lambda: store.pop("key", None) is not None or True)
    monkeypatch.setattr("app.core.credentials.has_elevenlabs_key", lambda: bool(store.get("key")))
    yield store


@pytest.fixture(autouse=True)
def _privacy_off():
    from app.core.privacy import privacy_mode
    privacy_mode.set(False)
    yield
    privacy_mode.set(False)


@pytest.mark.parametrize("path,payload", [
    ("/voice/cloud/key", {"api_key": FAKE_KEY}),
    ("/voice/cloud/key/delete", None),
    ("/voice/cloud/validate", None),
    ("/voice/cloud/voices", None),
    ("/voice/cloud/select-voice", {"voice_id": "a1", "voice_name": "Reginald"}),
    ("/voice/engine", {"engine": "elevenlabs"}),
    ("/voice/cloud/settings", {"settings": {}}),
    ("/voice/cloud/settings/reset", None),
    ("/voice/cloud/fallback", {"allowed": True}),
    ("/voice/cloud/test", None),
])
def test_every_cloud_endpoint_requires_the_session_token(path, payload):
    from app.api.server import app

    with TestClient(app) as bare:
        bare.get("/health")  # get a cookie but no header
        response = bare.post(path, json=payload if payload is not None else {})
    assert response.status_code == 403, f"{path} is not protected"


def test_the_status_endpoint_never_returns_the_key(client, _no_stored_key, isolated_preferences):
    client.post("/voice/cloud/key", json={"api_key": FAKE_KEY})

    body = client.get("/voice/cloud").text

    assert FAKE_KEY not in body
    assert json.loads(body)["key_configured"] is True


def test_saving_a_key_does_not_contact_elevenlabs(client, monkeypatch, isolated_preferences):
    """A network call the user did not ask for is one they did not
    consent to — and a key saved while offline is still the key they
    meant to save."""
    def _explode(*args, **kwargs):
        raise AssertionError("saving a key must not make a request")

    monkeypatch.setattr(elevenlabs, "_client", _explode)
    response = client.post("/voice/cloud/key", json={"api_key": FAKE_KEY})
    assert response.json()["success"] is True


def test_privacy_mode_blocks_every_call_that_would_leave_the_machine(client, _no_stored_key, isolated_preferences):
    from app.core.privacy import privacy_mode

    _no_stored_key["key"] = FAKE_KEY
    privacy_mode.set(True)
    try:
        for path in ("/voice/cloud/validate", "/voice/cloud/voices", "/voice/cloud/test"):
            body = client.post(path, json={}).json()
            assert body["success"] is False, f"{path} ran while privacy mode was on"
            assert "privacy" in body["message"].lower()
    finally:
        privacy_mode.set(False)


def test_the_test_button_does_not_fall_back_to_the_local_voice(client, _no_stored_key, mock_api, isolated_preferences):
    """This button answers "does the cloud voice work". A local voice
    answering it would be the wrong answer to the question asked."""
    _no_stored_key["key"] = FAKE_KEY
    client.post("/voice/cloud/select-voice", json={"voice_id": VOICE_ID, "voice_name": "Reginald"})
    mock_api(lambda request: httpx.Response(
        401, json={"detail": "bad"}, headers={"content-type": "application/json"},
    ))

    body = client.post("/voice/cloud/test", json={}).json()

    assert body["success"] is False
    assert "key" in body["message"].lower()
