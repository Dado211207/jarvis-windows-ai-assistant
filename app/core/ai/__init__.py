"""AI provider pipeline — one contract, one place errors are classified.

Before this package existed, `Brain.generate_response()` constructed an
Anthropic client inline, and every failure produced the same message:
"AI responses aren't set up yet. Open Settings and add an API key." That
message is correct for exactly one cause and actively misleading for all
the others — a rate limit, an expired key, a dropped network, a local
Ollama server that isn't running. Telling a user with a perfectly good
key to go add a key is worse than saying nothing.

So the split here is deliberate:

  base.py               the contract: what a provider must do, the
                        cancellation token, and the typed failure
                        (ProviderError) that carries an ErrorCategory
                        rather than raw SDK text.
  anthropic_provider.py the cloud provider.
  ollama_provider.py    the local provider. Only ever offers models the
                        running instance actually reports, and never
                        triggers a download.

app/core/providers.py stays what it was — *detection* for the UI ("what
could this machine use?"). This package is *use* ("send this and get an
answer"). They are separate because detection has to be fast, total and
side-effect-free for a page render, while generation is slow, fallible
and cancellable.
"""

from app.core.ai.base import (
    AIProvider,
    Availability,
    CancellationToken,
    GenerationCancelled,
    Message,
    ProviderConfig,
    ProviderError,
    ProviderReply,
)

# Message and Availability are part of the contract a caller needs, not
# internals: stream() cannot be called without constructing Message, and
# availability() cannot be handled without reading Availability. They
# were reachable only as app.core.ai.base imports before, which made the
# package's public surface narrower than the interface it defines.
__all__ = [
    "AIProvider",
    "Availability",
    "CancellationToken",
    "GenerationCancelled",
    "Message",
    "ProviderConfig",
    "ProviderError",
    "ProviderReply",
    "get_provider",
]


def get_provider(name: str, config: ProviderConfig) -> AIProvider:
    """Return the provider implementation for *name*.

    An unknown name resolves to Anthropic — the historical default —
    rather than raising, matching providers.selected_provider(). A
    config value nobody recognises must not be able to take the whole
    chat feature down.
    """
    from app.core.providers import PROVIDER_OLLAMA, normalise_provider

    if normalise_provider(name) == PROVIDER_OLLAMA:
        from app.core.ai.ollama_provider import OllamaProvider
        return OllamaProvider(config)

    from app.core.ai.anthropic_provider import AnthropicProvider
    return AnthropicProvider(config)
