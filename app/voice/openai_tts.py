"""A tightly bounded OpenAI Speech provider for optional cloud TTS.

Only POST https://api.openai.com/v1/audio/speech is reachable. The
destination is not configurable, redirects are refused, keys are sent only in
the Authorization header, and neither raw upstream bodies nor request headers
cross this module's error boundary.
"""

from dataclasses import dataclass
from threading import Event
from typing import Optional, Tuple

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

INVALID_KEY = "invalid_key"
QUOTA = "quota"
RATE_LIMITED = "rate_limited"
TIMEOUT = "timeout"
OFFLINE = "offline"
BAD_RESPONSE = "bad_response"
CANCELLED = "cancelled"
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


def validate_model(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned not in ALLOWED_MODELS:
        raise ValueError("Unsupported OpenAI speech model.")
    return cleaned


def validate_voice(value: str) -> str:
    cleaned = (value or "").strip().lower()
    if cleaned not in VOICES:
        raise ValueError("Unsupported OpenAI built-in voice.")
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
        timeout=httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT_SECONDS),
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


def _read_bounded(response, cancel: Optional[Event]) -> bytes:
    chunks = []
    total = 0
    for chunk in response.iter_bytes():
        if cancel is not None and cancel.is_set():
            raise _fail(CANCELLED)
        total += len(chunk)
        if total > MAX_AUDIO_BYTES:
            logger.warning("OpenAI Speech response exceeded the audio size limit; discarded.")
            raise _fail(BAD_RESPONSE)
        chunks.append(chunk)
    return b"".join(chunks)


def _call(api_key: str, payload: dict, *, cancel: Optional[Event] = None,
          read_timeout: float = READ_TIMEOUT_SECONDS) -> bytes:
    import httpx

    if not api_key or len(api_key) > MAX_API_KEY_CHARS:
        raise _fail(NOT_CONFIGURED)
    if cancel is not None and cancel.is_set():
        raise _fail(CANCELLED)

    try:
        with _client(read_timeout) as client:
            with client.stream(
                "POST", SPEECH_PATH, headers=_headers(api_key), json=payload,
            ) as response:
                body = _read_bounded(response, cancel)
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
                if not body.startswith(b"RIFF"):
                    logger.warning("OpenAI Speech returned audio that was not RIFF/WAV.")
                    raise _fail(BAD_RESPONSE)
                if cancel is not None and cancel.is_set():
                    raise _fail(CANCELLED)
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


def synthesise_wav(
    text: str,
    api_key: str,
    *,
    model: str = DEFAULT_MODEL,
    voice: str = DEFAULT_VOICE,
    speed: float = DEFAULT_SPEED,
    instructions: str = DEFAULT_INSTRUCTIONS,
    cancel: Optional[Event] = None,
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
    return _call(api_key, payload, cancel=cancel)


def validate_key(api_key: str) -> Tuple[bool, str]:
    """Explicit key check through Speech; the tiny generation may incur usage."""
    try:
        synthesise_wav(
            "Voice key check.",
            api_key,
            instructions="Speak clearly and briefly. Do not imitate any person.",
        )
    except OpenAITTSError as exc:
        return False, exc.message
    return True, "The key works. A brief Speech generation was used for this check."


TEST_PHRASE = AB_TEST_PHRASE
