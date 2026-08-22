"""AI provider discovery — what is *actually* usable right now.

The onboarding wizard and the Settings page both need to answer one
question honestly: which providers can this machine really use? A
provider is only ever reported available when something real was
checked — a key present in the OS credential store, or a live local
Ollama instance that answered — never because the code supports it in
principle. CLAUDE.md's onboarding rule is explicit that a capability
must not be claimed unless detected, and this module is where that rule
is enforced for providers.

Two providers exist today:

  anthropic  cloud. Available when a key is configured (env var or the
             Windows credential store — see app/config.py's
             effective_api_key). Never echoes the key back.
  ollama     local. Available only when an Ollama server answers on its
             loopback API. Model choices come from what that instance
             actually reports; this module never invents a model list
             and never triggers a download — pulling a multi-gigabyte
             model is a decision for the user, made in Ollama itself.

Detection never raises and never blocks for long: an unreachable local
server is the normal case for most users, so it must be an ordinary
"not detected" result returned quickly, not an error or a stall.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from app.logging_config import get_logger

logger = get_logger("core.providers")

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OLLAMA = "ollama"
KNOWN_PROVIDERS = (PROVIDER_ANTHROPIC, PROVIDER_OLLAMA)

# Ollama's documented default loopback endpoint. Loopback only, matching
# the rest of this application's network posture.
OLLAMA_DEFAULT_HOST = "127.0.0.1"
OLLAMA_DEFAULT_PORT = 11434
OLLAMA_PROBE_TIMEOUT_SECONDS = 1.5


@dataclass
class ProviderStatus:
    """What the UI renders. *detail* is always safe to display: it
    describes availability, never credentials."""

    name: str
    display_name: str
    kind: str  # "cloud" | "local"
    available: bool
    detail: str
    models: List[str] = field(default_factory=list)
    requires_credentials: bool = False


def anthropic_status() -> ProviderStatus:
    """Available iff a key is actually configured. The key itself is
    never read into the result — only the boolean."""
    from app.config import settings

    configured = settings.has_anthropic_key
    return ProviderStatus(
        name=PROVIDER_ANTHROPIC,
        display_name="Anthropic (Claude)",
        kind="cloud",
        available=configured,
        detail=(
            "API key configured — natural-language chat is available."
            if configured
            else "No API key configured yet. JARVIS still works without one; "
                 "deterministic commands do not need a provider."
        ),
        requires_credentials=True,
    )


def _ollama_base_url() -> str:
    return f"http://{OLLAMA_DEFAULT_HOST}:{OLLAMA_DEFAULT_PORT}"


def ollama_status(http_client=None) -> ProviderStatus:
    """Probes a real local Ollama instance. *http_client* is an injection
    seam for tests; production passes none and uses httpx directly.

    An unreachable server is the common case, not a failure: it means
    Ollama simply is not installed or running, which is reported plainly
    rather than logged as an error."""
    unavailable = ProviderStatus(
        name=PROVIDER_OLLAMA,
        display_name="Ollama (local models)",
        kind="local",
        available=False,
        detail=(
            "No local Ollama server detected. Set up local AI from Settings and "
            "JARVIS will install it for you, after showing you what it downloads."
        ),
    )

    try:
        if http_client is None:
            import httpx
            response = httpx.get(f"{_ollama_base_url()}/api/tags", timeout=OLLAMA_PROBE_TIMEOUT_SECONDS)
        else:
            response = http_client.get(f"{_ollama_base_url()}/api/tags", timeout=OLLAMA_PROBE_TIMEOUT_SECONDS)
    except Exception:
        # Connection refused/DNS/timeout — Ollama is not running. Normal.
        return unavailable

    if getattr(response, "status_code", None) != 200:
        return unavailable

    try:
        payload = response.json()
    except Exception:
        return unavailable

    models = _model_names(payload)
    if not models:
        return ProviderStatus(
            name=PROVIDER_OLLAMA,
            display_name="Ollama (local models)",
            kind="local",
            available=False,
            detail=(
                "Ollama is running but has no models installed. Download the "
                "recommended one from Settings — nothing starts until you press it."
            ),
        )

    return ProviderStatus(
        name=PROVIDER_OLLAMA,
        display_name="Ollama (local models)",
        kind="local",
        available=True,
        detail=f"Ollama is running with {len(models)} model(s) installed.",
        models=models,
    )


def _model_names(payload) -> List[str]:
    """Ollama's /api/tags returns {"models": [{"name": "llama3:latest", ...}]}.
    Anything that does not match that shape yields no models rather than
    a guess — a fabricated model list would be exactly the kind of false
    capability claim this module exists to prevent."""
    if not isinstance(payload, dict):
        return []
    entries = payload.get("models")
    if not isinstance(entries, list):
        return []
    names = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"]:
            names.append(entry["name"])
    return names


def detect_all(http_client=None) -> List[ProviderStatus]:
    return [anthropic_status(), ollama_status(http_client=http_client)]


def is_valid_provider(name: str) -> bool:
    return name in KNOWN_PROVIDERS


def normalise_provider(value) -> str:
    """Map any configured value onto a known provider name. Pure and
    total: an unrecognised value (including None or a non-string, which
    a mocked settings object can produce) falls back to anthropic — the
    historical default — rather than leaving the app in an unknown
    state or raising on a config typo."""
    try:
        name = str(value or "").strip().lower()
    except Exception:  # noqa: BLE001 — a __str__ that raises is still just "unrecognised"
        return PROVIDER_ANTHROPIC
    return name if is_valid_provider(name) else PROVIDER_ANTHROPIC


def selected_provider() -> str:
    """The provider actually in effect, normalised.

    A choice saved in Settings wins over the configured default — see
    app/core/preferences.py for why that precedence and not the other
    one. This is the single answer to "which provider?"; nothing should
    read settings.jarvis_ai_provider directly and reach a different one.
    """
    from app.config import settings
    from app.core.preferences import get as get_preference

    return normalise_provider(get_preference("ai_provider") or settings.jarvis_ai_provider)


def selected_ollama_model() -> str:
    """The chosen local model: the saved preference, else the configured
    default, else "" meaning "whatever the local instance reports first"."""
    from app.config import settings
    from app.core.preferences import get as get_preference

    return get_preference("ollama_model") or (getattr(settings, "jarvis_ollama_model", "") or "")
