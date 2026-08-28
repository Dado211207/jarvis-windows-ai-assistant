"""Anthropic (Claude) — the cloud provider.

The SDK is imported inside the methods rather than at module import
time, deliberately: `anthropic` is an optional-feeling dependency for a
user running JARVIS purely on local commands, and importing this module
(which app/core/brain.py does on every request path) must not fail or
cost anything when no key is configured and no call will be made.

Failures are classified from the exception's TYPE NAME only — never its
message — by app/core/errors.py::classify_anthropic_exception(). A
message can carry request/response detail; a class name cannot.
"""

from typing import Iterator, List, Optional

from app.core.ai.base import (
    AIProvider,
    Availability,
    CancellationToken,
    GenerationCancelled,
    Message,
    ProviderError,
    ProviderReply,
    check_cancelled,
)
from app.core.errors import classify_anthropic_exception
from app.logging_config import get_logger

logger = get_logger("core.ai.anthropic")

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class AnthropicProvider(AIProvider):
    name = "anthropic"
    display_name = "Anthropic (Claude)"

    def resolved_model(self) -> str:
        return self._config.model or DEFAULT_MODEL

    def availability(self) -> Availability:
        """Only ever answers the question it can actually answer: is a
        key present? Whether the key is *valid* is unknowable without
        spending a request, so it is reported when a call fails, as an
        auth error — not guessed at here."""
        if not self._config.api_key:
            return Availability(
                ready=False,
                reason=(
                    "AI responses aren't set up yet. Open Settings and add an "
                    "Anthropic API key to enable them — everything else keeps "
                    "working without one."
                ),
            )
        return Availability(ready=True, reason="API key configured.")

    # --- generation ---

    def _client(self):
        import anthropic

        return anthropic.Anthropic(
            api_key=self._config.api_key,
            timeout=float(self._config.timeout_seconds),
        )

    def _payload(self, messages: List[Message], system: str) -> dict:
        return {
            "model": self.resolved_model(),
            "max_tokens": self._config.max_tokens,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }

    def generate(
        self,
        messages: List[Message],
        system: str,
        cancel: Optional[CancellationToken] = None,
    ) -> ProviderReply:
        """A single non-streaming request. Kept as its own path (rather
        than draining stream()) because it is what /command uses, where
        there is nowhere to put a partial answer anyway."""
        check_cancelled(cancel)
        try:
            message = self._client().messages.create(**self._payload(messages, system))
        except Exception as exc:  # noqa: BLE001 — re-raised as a classified ProviderError
            raise ProviderError(classify_anthropic_exception(exc), cause=exc) from exc

        content = message.content[0].text if message.content else ""
        logger.info("Anthropic reply received. model=%s", self.resolved_model())
        return ProviderReply(
            content=content,
            provider=self.name,
            model=self.resolved_model(),
            used_api=True,
        )

    def stream(
        self,
        messages: List[Message],
        system: str,
        cancel: Optional[CancellationToken] = None,
    ) -> Iterator[str]:
        check_cancelled(cancel)
        try:
            client = self._client()
            with client.messages.stream(**self._payload(messages, system)) as stream:
                for delta in stream.text_stream:
                    # Checked before yielding, so a stopped generation
                    # neither delivers nor persists the chunk it was
                    # holding — and leaving the `with` block closes the
                    # HTTP response, ending the upstream work.
                    check_cancelled(cancel)
                    if delta:
                        yield delta
        except (GenerationCancelled, ProviderError):
            raise
        except Exception as exc:  # noqa: BLE001 — re-raised as a classified ProviderError
            raise ProviderError(classify_anthropic_exception(exc), cause=exc) from exc
