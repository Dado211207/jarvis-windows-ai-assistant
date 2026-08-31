"""The provider contract: configuration, cancellation, replies, failures.

Three design decisions worth stating, because they are what the rest of
the pipeline relies on:

**Providers never read global settings.** They are constructed with a
ProviderConfig. That makes them testable in isolation, and it means the
one place that decides "which key, which model, how long to wait" is
`Brain._provider_config()` rather than five scattered imports.

**A provider never raises a raw SDK exception past its own boundary.**
It raises ProviderError, which carries an `ErrorCategory` and nothing
else that came from the provider — no response body, no URL, no header
fragment. The original exception is still logged in full server-side by
app/core/errors.py::to_safe_error(). This is the same rule the v0.2
audit's exception-leak fix established; this package keeps it.

**Cancellation is cooperative and honest about its limits.** A
CancellationToken is checked between streamed chunks, so stopping a
streaming generation really does stop the work and close the upstream
connection. For a non-streaming call already in flight, cancelling
cannot un-send an HTTP request — it suppresses the reply instead: the
text is discarded, nothing is persisted, and the user is told it was
stopped. `stream()` is what the chat endpoint actually uses, so in
practice stop means stop.
"""

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, List, Optional

from app.core.errors import ErrorCategory


@dataclass(frozen=True)
class ProviderConfig:
    """Everything a provider needs, resolved once by the caller.

    `api_key` is write-only in spirit: it is passed to an SDK and never
    logged, never returned in a reply, never included in a repr that
    reaches a client. (The dataclass repr would show it, which is why
    nothing in this package ever puts a ProviderConfig into a log line
    or an API response — see the redaction rules in CLAUDE.md.)
    """

    model: str = ""
    max_tokens: int = 250
    # A per-request timeout, not a cap on total response length: it
    # bounds how long we wait for the connection and for the *next*
    # chunk. A long answer that keeps producing tokens is not a stall
    # and is not cut off at this value.
    timeout_seconds: float = 20.0
    api_key: str = ""
    ollama_model: str = ""
    # Anthropic only. Blank for a legacy workspace-scoped key, which needs
    # no header; required by Anthropic for an identity-linked (personal or
    # service account) key, which is rejected with HTTP 400 without it.
    # Not a secret, but still never logged or returned — see
    # app/core/ai/workspace.py.
    anthropic_workspace_id: str = ""


@dataclass
class Message:
    """One conversation turn sent to a provider. `role` is "user" or
    "assistant"; system text is passed separately, because that is how
    both providers model it."""

    role: str
    content: str


@dataclass
class ProviderReply:
    content: str
    provider: str
    model: str
    used_api: bool = True
    stopped: bool = False


@dataclass
class Availability:
    ready: bool
    reason: str = ""
    # Set when the reason is a real failure rather than "not configured
    # yet", so the caller can report it honestly instead of telling
    # everyone to go add an API key.
    category: Optional[ErrorCategory] = None


class GenerationCancelled(Exception):
    """Raised inside a provider when its CancellationToken is set. Not an
    error condition — the user asked for it — so callers treat it as a
    normal end of generation, not something to log as a failure."""


class ProviderError(Exception):
    """A provider failure, already classified.

    Carries no text from the provider. `category` drives the message the
    user sees (app/core/errors.py owns those strings) and `detail` is a
    short, provider-authored, credential-free clarification that we
    wrote ourselves — e.g. naming which local models are installed. It is
    never derived from an exception message or a response body.
    """

    def __init__(
        self,
        category: ErrorCategory,
        detail: str = "",
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(detail or category.value)
        self.category = category
        self.detail = detail
        self.cause = cause


class CancellationToken:
    """Thread-safe, one-way. Once cancelled it stays cancelled — a token
    is per-generation, so there is no legitimate reason to un-cancel one
    and every reason not to allow it."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise GenerationCancelled()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout)


class AIProvider(ABC):
    """What every provider must offer.

    Subclasses implement `availability()` and `stream()`. `generate()`
    has a working default built on `stream()`, so a provider only has to
    override it when a non-streaming call is genuinely different (the
    Anthropic one does, to keep its single-request path).
    """

    name = ""
    display_name = ""

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    @property
    def config(self) -> ProviderConfig:
        return self._config

    @abstractmethod
    def availability(self) -> Availability:
        """Can this provider be used *right now*? Must be quick and must
        never raise: it is called on a page render and before every
        generation."""

    @abstractmethod
    def stream(
        self,
        messages: List[Message],
        system: str,
        cancel: Optional[CancellationToken] = None,
    ) -> Iterator[str]:
        """Yield text deltas. Raises ProviderError on failure and
        GenerationCancelled when *cancel* is set."""

    @abstractmethod
    def resolved_model(self) -> str:
        """The model this provider would actually use for the next call.
        Reported to the client so a user can see which model answered."""

    def generate(
        self,
        messages: List[Message],
        system: str,
        cancel: Optional[CancellationToken] = None,
    ) -> ProviderReply:
        chunks: List[str] = []
        for delta in self.stream(messages, system, cancel=cancel):
            chunks.append(delta)
        return ProviderReply(
            content="".join(chunks),
            provider=self.name,
            model=self.resolved_model(),
            used_api=True,
        )


def check_cancelled(cancel: Optional[CancellationToken]) -> None:
    if cancel is not None:
        cancel.raise_if_cancelled()
