"""Ollama — the local provider.

Two rules govern this module, both about not lying to the user:

**Only models the running instance actually reports.** The configured
model is checked against `/api/tags` before every generation. If it
isn't installed, the failure names the models that *are* — a chat that
silently answers with some other model, or that fails with "model not
found" while a perfectly good model sits installed, are both worse than
one honest sentence.

**Never trigger a download.** `/api/pull` is never called from anywhere
in this codebase. Pulling a model is multiple gigabytes over someone's
connection onto someone's disk; that is their decision, made in Ollama,
not a side effect of typing a sentence into a chat box.

Network posture matches the rest of the app: loopback only, via the host
and port fixed in app/core/providers.py. There is no setting to point
this at a remote Ollama, because that would turn a local-first assistant
into one that ships your conversation to a machine you configured once
and forgot about.
"""

import json
from typing import Iterator, List, Optional

from app.core.ai.base import (
    AIProvider,
    Availability,
    CancellationToken,
    GenerationCancelled,
    Message,
    ProviderError,
    check_cancelled,
)
from app.core.errors import ErrorCategory
from app.logging_config import get_logger

logger = get_logger("core.ai.ollama")


class OllamaProvider(AIProvider):
    name = "ollama"
    display_name = "Ollama (local models)"

    def _base_url(self) -> str:
        from app.core.providers import _ollama_base_url

        return _ollama_base_url()

    def _status(self):
        from app.core.providers import ollama_status

        return ollama_status()

    def resolved_model(self) -> str:
        """The configured model, or — when none is configured — the first
        one the local instance reports. Falling back to "whatever is
        installed" is the right default for a local runtime where having
        exactly one model is the common case; it never invents a name."""
        configured = (self._config.ollama_model or "").strip()
        if configured:
            return configured
        try:
            models = self._status().models
        except Exception:  # noqa: BLE001 — detection is best-effort here
            return ""
        return models[0] if models else ""

    def availability(self) -> Availability:
        status = self._status()
        if not status.available:
            return Availability(
                ready=False,
                reason=status.detail,
                category=ErrorCategory.PROVIDER_UNAVAILABLE,
            )

        configured = (self._config.ollama_model or "").strip()
        if configured and configured not in status.models:
            return Availability(
                ready=False,
                reason=self._model_missing_detail(configured, status.models),
                category=ErrorCategory.PROVIDER_UNAVAILABLE,
            )
        return Availability(ready=True, reason=status.detail)

    @staticmethod
    def _model_missing_detail(configured: str, installed: List[str]) -> str:
        installed_text = ", ".join(installed) if installed else "none"
        return (
            f"Ollama is running but the selected model '{configured}' is not "
            f"installed. Installed models: {installed_text}. Pull it in Ollama, "
            "or choose an installed one in Settings — JARVIS never downloads "
            "models for you."
        )

    # --- generation ---

    def stream(
        self,
        messages: List[Message],
        system: str,
        cancel: Optional[CancellationToken] = None,
    ) -> Iterator[str]:
        check_cancelled(cancel)

        availability = self.availability()
        if not availability.ready:
            raise ProviderError(
                availability.category or ErrorCategory.PROVIDER_UNAVAILABLE,
                detail=availability.reason,
            )

        model = self.resolved_model()
        if not model:
            raise ProviderError(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                detail=(
                    "Ollama is running but reported no usable model. Pull one in "
                    "Ollama and try again."
                ),
            )

        payload = {
            "model": model,
            "stream": True,
            "messages": (
                ([{"role": "system", "content": system}] if system else [])
                + [{"role": m.role, "content": m.content} for m in messages]
            ),
            "options": {"num_predict": self._config.max_tokens},
        }

        try:
            yield from self._stream_lines(payload, cancel)
        except (GenerationCancelled, ProviderError):
            raise
        except Exception as exc:  # noqa: BLE001 — re-raised as a classified ProviderError
            raise ProviderError(_classify(exc), cause=exc) from exc

    def _stream_lines(self, payload: dict, cancel: Optional[CancellationToken]) -> Iterator[str]:
        import httpx

        timeout = httpx.Timeout(float(self._config.timeout_seconds))
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", f"{self._base_url()}/api/chat", json=payload) as response:
                if response.status_code != 200:
                    raise ProviderError(
                        ErrorCategory.PROVIDER_ERROR,
                        detail=(
                            "The local Ollama server rejected the request. Check that "
                            "the selected model is installed and that Ollama is up to date."
                        ),
                    )
                for line in response.iter_lines():
                    # Checked before parsing so a stopped generation exits
                    # the `with` blocks immediately, closing the connection
                    # and ending the model's work rather than draining it.
                    check_cancelled(cancel)
                    delta = _delta_from_line(line)
                    if delta:
                        yield delta


def _delta_from_line(line) -> str:
    """Ollama streams newline-delimited JSON objects. A line that is
    blank, unparseable, or shaped differently yields nothing rather than
    a guess — the same rule app/core/providers.py applies to /api/tags."""
    if not line:
        return ""
    text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
    text = text.strip()
    if not text:
        return ""
    try:
        obj = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(obj, dict):
        return ""
    message = obj.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return ""


def _classify(exc: Exception) -> ErrorCategory:
    """Ollama has no error taxonomy of its own worth mapping, so this
    classifies transport failures the same way the Anthropic path does —
    from the exception's type name only, never its message."""
    name = type(exc).__name__
    if "Timeout" in name:
        return ErrorCategory.PROVIDER_TIMEOUT
    if "Connect" in name or "Network" in name:
        return ErrorCategory.PROVIDER_UNAVAILABLE
    return ErrorCategory.PROVIDER_ERROR
