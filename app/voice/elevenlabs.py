"""ElevenLabs as an optional cloud voice — never the default, never automatic.

The local neural voice (Kokoro) remains the default and the
privacy-preserving one. This tier exists because a cloud model can sound
better than anything that runs offline on a laptop, and the product owner
wants that available to somebody willing to pay for it and send their
reply text to a third party. Both of those are real costs, so choosing
this engine is always an explicit act with both costs stated first.

**What crosses the network.** The text of the reply, and nothing else.
Not the conversation, not the user's name, not a memory, not audio from
the microphone. The key travels in a header, never in a URL or on a
command line.

**Security boundary.** One pinned host over HTTPS, no redirects followed
anywhere, connect and read timeouts on every call, a hard cap on the
response body, and a content-type check before a single byte is treated
as audio. There is deliberately no endpoint that takes a URL and plays
what is at the other end of it: an AI model that could name an audio URL
and have JARVIS fetch it would be a request-forgery primitive wearing a
voice.

**Why WAV rather than MP3.** Playback is `winsound` (app/voice/audio.py),
which plays PCM WAV and nothing else. ElevenLabs documents `wav_24000`
among its output formats, and 24 kHz is what Kokoro already produces, so
the bytes go straight into the existing player. `wav_44100` is documented
as requiring a Pro subscription; asking for it by default would turn a
working setup into a 401 for anyone below that tier.

API surface used, from ElevenLabs' own documentation:

  POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}
       header `xi-api-key`; body `text`, `model_id`, `output_format`,
       `voice_settings`
  GET  https://api.elevenlabs.io/v2/voices
  GET  https://api.elevenlabs.io/v1/user/subscription   (key validation)

`voice_settings` accepts `stability`, `similarity_boost`, `style`,
`use_speaker_boost` and `speed`. `speed` is documented as 0.7–1.2 and is
**not supported by the Eleven v3 models**, which is why the default model
here is `eleven_multilingual_v2` and why speed is omitted from the
request when a v3 model is selected rather than sent and silently
ignored.
"""

import json
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from app.logging_config import get_logger

logger = get_logger("voice.elevenlabs")

# The only host this module will ever talk to. Not a setting: a
# configurable API host is how a local-first product quietly becomes one
# that ships its user's words to whatever was typed into a text box.
API_HOST = "api.elevenlabs.io"
API_BASE = f"https://{API_HOST}"

TTS_PATH = "/v1/text-to-speech/{voice_id}"
VOICES_PATH = "/v2/voices"
SUBSCRIPTION_PATH = "/v1/user/subscription"

AUTH_HEADER = "xi-api-key"

# Documented default, and the one that supports `speed`.
DEFAULT_MODEL = "eleven_multilingual_v2"
# Models that do not accept `speed`, per ElevenLabs' speed-control docs.
MODELS_WITHOUT_SPEED = ("eleven_v3", "eleven_v3_conversational")

# 24 kHz WAV: playable by winsound as-is, same rate as Kokoro, and not
# behind the Pro tier that 44.1 kHz sits behind.
OUTPUT_FORMAT = "wav_24000"

CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 30.0
# A spoken reply is seconds of audio, not minutes. 24 kHz 16-bit mono is
# ~48 KB/s, so this is roughly three minutes of speech — generous for a
# reply, and a hard stop long before memory is a concern.
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024

# Text is bounded before it is sent: a runaway reply would be somebody's
# credits, not just a slow request.
MAX_TEXT_CHARS = 2000

DEFAULT_SETTINGS = {
    "stability": 0.50,
    "similarity_boost": 0.75,
    "style": 0.00,
    "use_speaker_boost": True,
    "speed": 0.92,
}

SETTING_RANGES = {
    "stability": (0.0, 1.0),
    "similarity_boost": (0.0, 1.0),
    "style": (0.0, 1.0),
    # Documented range. Values outside it are rejected by the API, so
    # clamping here turns a 422 into a working request.
    "speed": (0.7, 1.2),
}

# What the Voice page's "test this voice" button says. Chosen to exercise
# the qualities the voice is being judged on — a greeting, a status
# report, and the pacing between them.
TEST_PHRASE = "Good evening, sir. All systems are online and ready."


# ---------------------------------------------------------------------------
# Errors, classified. "It didn't work" is not a diagnosis, and the four
# things that actually go wrong here have four different fixes.
# ---------------------------------------------------------------------------

INVALID_KEY = "invalid_key"
FORBIDDEN = "forbidden"
QUOTA = "quota"
RATE_LIMITED = "rate_limited"
TIMEOUT = "timeout"
OFFLINE = "offline"
BAD_RESPONSE = "bad_response"
NOT_CONFIGURED = "not_configured"
PROVIDER_ERROR = "provider_error"


@dataclass
class ElevenLabsError(Exception):
    """Carries a category and a sentence a person can act on.

    Never carries the key, the request body or a raw upstream payload:
    this message is rendered in the UI, returned by the API and written
    to the log.
    """

    category: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


_MESSAGES = {
    INVALID_KEY: (
        "ElevenLabs rejected the API key. Check it in the Voice page — a key is "
        "revoked the moment it is regenerated."
    ),
    FORBIDDEN: (
        "That ElevenLabs key is valid but not allowed to do this. It may be "
        "restricted, or the voice may not belong to this account."
    ),
    QUOTA: (
        "This ElevenLabs account has no character quota left this period. "
        "JARVIS's local voice still works and costs nothing."
    ),
    RATE_LIMITED: (
        "ElevenLabs is rate-limiting this account. Wait a moment and try again."
    ),
    TIMEOUT: (
        "ElevenLabs did not answer in time. The local voice is unaffected."
    ),
    OFFLINE: (
        "Could not reach ElevenLabs. Check the internet connection — the cloud "
        "voice needs one, and the local voice does not."
    ),
    BAD_RESPONSE: (
        "ElevenLabs returned something that was not audio. Nothing was played."
    ),
    NOT_CONFIGURED: (
        "No ElevenLabs API key is saved. Add one on the Voice page, or keep using "
        "the local voice."
    ),
    PROVIDER_ERROR: (
        "ElevenLabs could not produce that audio. Nothing was played."
    ),
}


def _fail(category: str) -> ElevenLabsError:
    return ElevenLabsError(category=category, message=_MESSAGES[category])


def _classify_status(status: int) -> str:
    if status == 401:
        return INVALID_KEY
    if status == 403:
        return FORBIDDEN
    if status == 429:
        return RATE_LIMITED
    # 422 is ElevenLabs' validation error, and quota exhaustion is
    # reported through it as well as through 401 on some plans.
    if status in (400, 422):
        return PROVIDER_ERROR
    return PROVIDER_ERROR


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def clamp_settings(values: Optional[dict]) -> dict:
    """Bring a settings dict into the documented ranges.

    Unknown keys are dropped rather than forwarded: this object goes into
    a request body, and passing through whatever arrived would make the
    endpoint a way to set fields nobody reviewed.
    """
    result = dict(DEFAULT_SETTINGS)
    for key, value in (values or {}).items():
        if key not in DEFAULT_SETTINGS:
            continue
        if key == "use_speaker_boost":
            result[key] = bool(value)
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        low, high = SETTING_RANGES[key]
        result[key] = max(low, min(high, number))
    return result


def _request_settings(settings: dict, model_id: str) -> dict:
    """The settings actually sent, with `speed` removed for models that do
    not accept it — omitting it is honest, sending it to be ignored is
    not."""
    payload = dict(settings)
    if model_id in MODELS_WITHOUT_SPEED:
        payload.pop("speed", None)
    return payload


# ---------------------------------------------------------------------------
# HTTP, on a short leash
# ---------------------------------------------------------------------------

def _client(timeout_read: float):
    """An httpx client pinned to one host with redirects switched off.

    `follow_redirects=False` is the important line. A redirect from the
    pinned host to anywhere else is exactly how a pinned host stops being
    pinned, and the API has no legitimate reason to issue one.
    """
    import httpx

    return httpx.Client(
        base_url=API_BASE,
        follow_redirects=False,
        timeout=httpx.Timeout(timeout_read, connect=CONNECT_TIMEOUT_SECONDS),
    )


def _headers(api_key: str) -> dict:
    return {"Accept": "*/*", AUTH_HEADER: api_key}


def _guard_response(response, expect_audio: bool) -> None:
    """Status, redirect and content-type checks, before anything is used."""
    import httpx

    if response.is_redirect:
        # Never followed, and never quietly treated as a failure to retry:
        # the pinned host redirecting is a fact worth refusing loudly.
        logger.warning("ElevenLabs returned a redirect; refusing to follow it.")
        raise _fail(BAD_RESPONSE)

    if response.status_code >= 400:
        category = _classify_status(response.status_code)
        if category == PROVIDER_ERROR and _looks_like_quota(response):
            category = QUOTA
        raise _fail(category)

    content_type = (response.headers.get("content-type") or "").lower()
    if expect_audio:
        if not content_type.startswith("audio/"):
            logger.warning("ElevenLabs sent content-type %r where audio was expected.", content_type[:64])
            raise _fail(BAD_RESPONSE)
    elif "json" not in content_type:
        logger.warning("ElevenLabs sent content-type %r where JSON was expected.", content_type[:64])
        raise _fail(BAD_RESPONSE)

    assert isinstance(response, httpx.Response)  # noqa: S101 - type narrowing only


def _looks_like_quota(response) -> bool:
    """Whether an error body is about credits.

    Read from the *status* body only, never echoed. ElevenLabs reports
    quota exhaustion through a validation-shaped error, which would
    otherwise be reported as "could not produce that audio" — true, but
    useless to somebody who needs to know they have run out.
    """
    try:
        body = response.content[:2048].decode("utf-8", errors="replace").lower()
    except Exception:  # noqa: BLE001
        return False
    return "quota" in body or "credits" in body or "exceeds" in body


def _read_bounded(response, limit: int) -> bytes:
    """Read at most *limit* bytes, then stop. A response that keeps going
    is a response that stops being read."""
    chunks = []
    total = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            logger.warning("ElevenLabs response exceeded %d bytes; discarded.", limit)
            raise _fail(BAD_RESPONSE)
    return b"".join(chunks)


def _call(method: str, path: str, api_key: str, *, json_body: Optional[dict] = None,
          expect_audio: bool = False, read_timeout: float = READ_TIMEOUT_SECONDS) -> bytes:
    """One request, fully guarded. Returns the raw body.

    Every exception out of here is an ElevenLabsError. httpx exceptions
    are translated rather than propagated, because an httpx error string
    can contain the full request URL and this project's rule is that a
    provider never raises a raw SDK exception past its own boundary.
    """
    import httpx

    if not api_key:
        raise _fail(NOT_CONFIGURED)

    try:
        with _client(read_timeout) as client:
            with client.stream(
                method, path, headers=_headers(api_key),
                json=json_body if json_body is not None else None,
            ) as response:
                limit = MAX_AUDIO_BYTES if expect_audio else MAX_JSON_BYTES
                body = _read_bounded(response, limit)
                # Guarded after reading so an error body can be inspected
                # for a quota signal, and before a single byte is used.
                response._content = body  # noqa: SLF001 - so .content works below
                _guard_response(response, expect_audio=expect_audio)
                return body
    except ElevenLabsError:
        raise
    except httpx.TimeoutException:
        raise _fail(TIMEOUT) from None
    except httpx.TransportError:
        raise _fail(OFFLINE) from None
    except Exception:  # noqa: BLE001
        logger.warning("ElevenLabs request failed.", exc_info=False)
        raise _fail(PROVIDER_ERROR) from None


# ---------------------------------------------------------------------------
# The three things this module does
# ---------------------------------------------------------------------------

@dataclass
class Voice:
    voice_id: str
    name: str
    category: str = ""
    description: str = ""
    labels: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "voice_id": self.voice_id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "labels": self.labels,
        }


def validate_key(api_key: str) -> Tuple[bool, str]:
    """Ask ElevenLabs whether this key works, only when somebody asks.

    Uses the subscription endpoint because it is the cheapest authenticated
    call that exists — it generates no audio and spends no credits.
    Returns (ok, message); never raises.
    """
    try:
        body = _call("GET", SUBSCRIPTION_PATH, api_key, read_timeout=15.0)
    except ElevenLabsError as exc:
        return False, exc.message

    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return True, "The key works."

    tier = str(data.get("tier") or "").strip()
    used = data.get("character_count")
    limit = data.get("character_limit")
    if isinstance(used, int) and isinstance(limit, int) and limit > 0:
        remaining = max(0, limit - used)
        suffix = f" {remaining:,} of {limit:,} characters left this period."
    else:
        suffix = ""
    return True, (f"The key works{f' ({tier} plan)' if tier else ''}.{suffix}")


def list_voices(api_key: str) -> List[Voice]:
    """The voices this account can actually use.

    Only what is needed to show a human-readable picker is kept. The rest
    of ElevenLabs' voice object — sharing state, verification records,
    sample lists — is somebody else's data model, not ours to store.
    """
    body = _call("GET", VOICES_PATH, api_key, read_timeout=20.0)
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        raise _fail(BAD_RESPONSE) from None

    entries = data.get("voices")
    if not isinstance(entries, list):
        raise _fail(BAD_RESPONSE)

    voices = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        voice_id = str(entry.get("voice_id") or "").strip()
        name = str(entry.get("name") or "").strip()
        if not voice_id or not name:
            continue
        labels = entry.get("labels")
        voices.append(Voice(
            voice_id=voice_id,
            name=name,
            category=str(entry.get("category") or ""),
            description=str(entry.get("description") or "")[:300],
            labels=labels if isinstance(labels, dict) else {},
        ))
    return voices


def synthesise_wav(
    text: str,
    voice_id: str,
    api_key: str,
    settings: Optional[dict] = None,
    model_id: str = DEFAULT_MODEL,
) -> bytes:
    """Turn *text* into WAV bytes. Raises ElevenLabsError, never anything else.

    The bytes are returned rather than written anywhere. Nothing about a
    spoken reply needs to exist as a file, and a file is a thing that has
    to be deleted afterwards, on every path, including the ones that
    fail.
    """
    cleaned = normalise_text(text)
    if not cleaned:
        raise _fail(PROVIDER_ERROR)
    if not voice_id:
        raise _fail(NOT_CONFIGURED)

    payload = {
        "text": cleaned,
        "model_id": model_id,
        "output_format": OUTPUT_FORMAT,
        "voice_settings": _request_settings(clamp_settings(settings), model_id),
    }
    body = _call(
        "POST", TTS_PATH.format(voice_id=voice_id), api_key,
        json_body=payload, expect_audio=True,
    )
    if not body.startswith(b"RIFF"):
        logger.warning("ElevenLabs audio was not a RIFF/WAV payload.")
        raise _fail(BAD_RESPONSE)
    return body


def normalise_text(text: str) -> str:
    """Collapse whitespace and bound the length.

    Bounding is a cost control as much as a technical one: every
    character sent is a character billed, and a reply that ran away
    should not quietly become somebody's invoice.
    """
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= MAX_TEXT_CHARS:
        return collapsed
    # Cut at a sentence end where possible, so the audio does not stop
    # mid-word.
    window = collapsed[:MAX_TEXT_CHARS]
    for terminator in (". ", "! ", "? "):
        cut = window.rfind(terminator)
        if cut > MAX_TEXT_CHARS // 2:
            return window[: cut + 1]
    return window
