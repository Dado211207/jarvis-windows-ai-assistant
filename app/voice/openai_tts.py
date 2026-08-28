"""A tightly bounded OpenAI Speech provider for optional cloud TTS.

Only POST https://api.openai.com/v1/audio/speech is reachable. The
destination is not configurable, redirects are refused, keys are sent only in
the Authorization header, and neither raw upstream bodies nor request headers
cross this module's error boundary.
"""

import concurrent.futures
from dataclasses import dataclass
from threading import Event
import time
from typing import Callable, Optional, Tuple

from app.logging_config import get_logger
from app.voice.samples import AB_TEST_PHRASE

logger = get_logger("voice.openai_tts")

API_HOST = "api.openai.com"
API_BASE = f"https://{API_HOST}"
SPEECH_PATH = "/v1/audio/speech"
DEFAULT_MODEL = "gpt-4o-mini-tts"
ALLOWED_MODELS = (DEFAULT_MODEL,)
VOICES = (
    "alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx",
    "sage", "shimmer", "verse", "marin", "cedar",
)
DEFAULT_VOICE = "cedar"
DEFAULT_SPEED = 1.0
SPEED_RANGE = (0.25, 4.0)
DEFAULT_INSTRUCTIONS = (
    "Use an original, calm and polished British male voice with a warm "
    "baritone. Keep the delivery restrained, highly intelligible and "
    "professional, with understated cinematic AI-assistant presence. "
    "Do not imitate any real person, actor, or fictional character."
)

MAX_TEXT_CHARS = 4096
MAX_INSTRUCTION_CHARS = 4096
MAX_API_KEY_CHARS = 512
MAX_AUDIO_BYTES = 8 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 30.0
TOTAL_TIMEOUT_SECONDS = 40.0

INVALID_KEY = "invalid_key"
QUOTA = "quota"
RATE_LIMITED = "rate_limited"
TIMEOUT = "timeout"
OFFLINE = "offline"
BAD_RESPONSE = "bad_response"
CANCELLED = "cancelled"
PRIVACY = "privacy"
NOT_CONFIGURED = "not_configured"
PROVIDER_ERROR = "provider_error"

_MESSAGES = {
    INVALID_KEY: "OpenAI rejected the voice API key. Replace it on the Voice page.",
    QUOTA: (
        "The OpenAI account has no available quota for this request. "
        "The local voice remains available without cloud usage."
    ),
    RATE_LIMITED: "OpenAI is rate-limiting voice requests. Wait a moment and try again.",
    TIMEOUT: "OpenAI Speech did not answer in time. Check the connection and try again.",
    OFFLINE: (
        "Could not reach OpenAI Speech. Cloud audio needs an internet connection; "
        "the local voice does not."
    ),
    BAD_RESPONSE: "OpenAI Speech returned invalid or oversized audio. Nothing was played.",
    CANCELLED: "OpenAI speech was stopped before playback.",
    PRIVACY: "Privacy mode is on, so no text was sent to OpenAI Speech.",
    NOT_CONFIGURED: "No OpenAI voice API key is saved. Add one on the Voice page.",
    PROVIDER_ERROR: "OpenAI Speech could not produce audio. Nothing was played.",
}


@dataclass
class OpenAITTSError(Exception):
    category: str
    message: str

    def __str__(self) -> str:  # pragma: no cover
        return self.message


def _fail(category: str) -> OpenAITTSError:
    return OpenAITTSError(category, _MESSAGES[category])


#: The only settings-rejection sentences that may reach a response.
#:
#: `str(exc)` must never be returned to a caller — tests/test_security_
#: invariants.py::test_no_endpoint_returns_raw_exception_text enforces
#: that, and it caught this route doing it. These messages are curated
#: and echo nothing the caller submitted, so the route can look one up
#: instead of forwarding whatever an exception happened to carry.
UNSUPPORTED_MODEL = "Unsupported OpenAI speech model."
UNSUPPORTED_VOICE = "Unsupported OpenAI built-in voice."
SETTINGS_REJECTED = "Those OpenAI voice settings were rejected."

_SAFE_SETTINGS_ERRORS = frozenset({UNSUPPORTED_MODEL, UNSUPPORTED_VOICE})


def safe_settings_error(exc: BaseException) -> str:
    """One of our own sentences, never the exception's own text.

    A ValueError raised somewhere new — inside a preference store, a
    library, a future validator — would otherwise start leaking its
    message through this route the day it appears, with nothing failing
    to say so.
    """
    message = str(exc)
    return message if message in _SAFE_SETTINGS_ERRORS else SETTINGS_REJECTED


def validate_model(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned not in ALLOWED_MODELS:
        raise ValueError(UNSUPPORTED_MODEL)
    return cleaned


def validate_voice(value: str) -> str:
    cleaned = (value or "").strip().lower()
    if cleaned not in VOICES:
        raise ValueError(UNSUPPORTED_VOICE)
    return cleaned


def clamp_speed(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = DEFAULT_SPEED
    low, high = SPEED_RANGE
    return max(low, min(high, number))


def normalise_instructions(value: str) -> str:
    return " ".join((value or "").split())[:MAX_INSTRUCTION_CHARS]


def normalise_text(value: str) -> str:
    cleaned = " ".join((value or "").split())
    if not cleaned:
        raise _fail(PROVIDER_ERROR)
    return cleaned[:MAX_TEXT_CHARS]


def _client(read_timeout: float):
    import httpx

    return httpx.Client(
        base_url=API_BASE,
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(
            read_timeout, connect=min(CONNECT_TIMEOUT_SECONDS, read_timeout),
        ),
    )


def _headers(api_key: str) -> dict:
    return {
        "Accept": "audio/wav",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _looks_like_quota(response) -> bool:
    """Inspect a small upstream error prefix, but never return or log it."""
    try:
        body = response.content[:2048].decode("utf-8", errors="replace").lower()
    except Exception:  # noqa: BLE001
        return False
    return "insufficient_quota" in body or "quota" in body or "billing" in body


def _classify_status(response) -> str:
    if response.status_code == 401:
        return INVALID_KEY
    if response.status_code == 429:
        return QUOTA if _looks_like_quota(response) else RATE_LIMITED
    return PROVIDER_ERROR


def new_deadline() -> float:
    return time.monotonic() + TOTAL_TIMEOUT_SECONDS


def _check_request_allowed(
    cancel: Optional[Event],
    deadline: float,
    privacy_guard: Optional[Callable[[], bool]],
) -> None:
    if cancel is not None and cancel.is_set():
        raise _fail(CANCELLED)
    if time.monotonic() >= deadline:
        raise _fail(TIMEOUT)
    if privacy_guard is not None and not privacy_guard():
        raise _fail(PRIVACY)


def _read_bounded(response, cancel: Optional[Event], deadline: float) -> bytes:
    chunks = []
    total = 0
    for chunk in response.iter_bytes():
        _check_request_allowed(cancel, deadline, None)
        total += len(chunk)
        if total > MAX_AUDIO_BYTES:
            logger.warning("OpenAI Speech response exceeded the audio size limit; discarded.")
            raise _fail(BAD_RESPONSE)
        chunks.append(chunk)
    return b"".join(chunks)


def _call_network(
    api_key: str,
    payload: dict,
    *,
    cancel: Optional[Event] = None,
    privacy_guard: Optional[Callable[[], bool]] = None,
    deadline: Optional[float] = None,
    read_timeout: float = READ_TIMEOUT_SECONDS,
) -> bytes:
    import httpx

    if not api_key or len(api_key) > MAX_API_KEY_CHARS:
        raise _fail(NOT_CONFIGURED)
    deadline = deadline if deadline is not None else new_deadline()
    _check_request_allowed(cancel, deadline, privacy_guard)

    try:
        remaining = max(0.001, deadline - time.monotonic())
        with _client(min(read_timeout, remaining)) as client:
            # This is the last instruction before httpx sends bytes. Privacy
            # may have changed while Credential Manager was being read.
            _check_request_allowed(cancel, deadline, privacy_guard)
            with client.stream(
                "POST", SPEECH_PATH, headers=_headers(api_key), json=payload,
            ) as response:
                body = _read_bounded(response, cancel, deadline)
                response._content = body  # noqa: SLF001
                if response.is_redirect:
                    logger.warning("OpenAI Speech returned a redirect; refusing it.")
                    raise _fail(BAD_RESPONSE)
                if response.status_code >= 400:
                    raise _fail(_classify_status(response))
                content_type = (response.headers.get("content-type") or "").lower()
                if not (
                    content_type.startswith("audio/")
                    or content_type.startswith("application/octet-stream")
                ):
                    logger.warning("OpenAI Speech returned a non-audio content type.")
                    raise _fail(BAD_RESPONSE)
                if len(body) < 12 or body[:4] != b"RIFF" or body[8:12] != b"WAVE":
                    logger.warning("OpenAI Speech returned audio that was not RIFF/WAV.")
                    raise _fail(BAD_RESPONSE)
                _check_request_allowed(cancel, deadline, privacy_guard)
                return body
    except OpenAITTSError:
        raise
    except httpx.TimeoutException:
        raise _fail(TIMEOUT) from None
    except httpx.TransportError:
        raise _fail(OFFLINE) from None
    except Exception:  # noqa: BLE001
        logger.warning("OpenAI Speech request failed.", exc_info=False)
        raise _fail(PROVIDER_ERROR) from None


def _call(
    api_key: str,
    payload: dict,
    *,
    cancel: Optional[Event] = None,
    privacy_guard: Optional[Callable[[], bool]] = None,
    deadline: Optional[float] = None,
    read_timeout: float = READ_TIMEOUT_SECONDS,
) -> bytes:
    """Apply a true wall-clock deadline around the bounded HTTP call.

    httpx timeouts bound individual socket operations, not the sum of a
    response made of many slow chunks. The outer Future makes the caller's
    deadline monotonic and total. A timed-out worker may finish later, but
    its bytes have no playback owner and are never returned to the engine.
    """
    deadline = deadline if deadline is not None else new_deadline()
    _check_request_allowed(cancel, deadline, privacy_guard)
    remaining = max(0.001, deadline - time.monotonic())
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="jarvis-openai-tts",
    )
    future = executor.submit(
        _call_network,
        api_key,
        payload,
        cancel=cancel,
        privacy_guard=privacy_guard,
        deadline=deadline,
        read_timeout=min(read_timeout, remaining),
    )
    try:
        return future.result(timeout=remaining)
    except concurrent.futures.TimeoutError:
        raise _fail(TIMEOUT) from None
    finally:
        executor.shutdown(wait=False)


def synthesise_wav(
    text: str,
    api_key: str,
    *,
    model: str = DEFAULT_MODEL,
    voice: str = DEFAULT_VOICE,
    speed: float = DEFAULT_SPEED,
    instructions: str = DEFAULT_INSTRUCTIONS,
    cancel: Optional[Event] = None,
    privacy_guard: Optional[Callable[[], bool]] = None,
    deadline: Optional[float] = None,
) -> bytes:
    """Generate bounded WAV audio. Only classified errors leave this module."""
    payload = {
        "model": validate_model(model),
        "input": normalise_text(text),
        "voice": validate_voice(voice),
        "instructions": normalise_instructions(instructions),
        "response_format": "wav",
        "speed": clamp_speed(speed),
    }
    return _call(
        api_key, payload, cancel=cancel, privacy_guard=privacy_guard, deadline=deadline,
    )


def validate_key(
    api_key: str, privacy_guard: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """Explicit key check through Speech; the tiny generation may incur usage."""
    try:
        synthesise_wav(
            "Voice key check.",
            api_key,
            instructions="Speak clearly and briefly. Do not imitate any person.",
            privacy_guard=privacy_guard,
        )
    except OpenAITTSError as exc:
        return False, exc.message
    return True, "The key works. A brief Speech generation was used for this check."


TEST_PHRASE = AB_TEST_PHRASE
