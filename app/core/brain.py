"""JARVIS Brain — orchestrates tools, routing, and the AI providers.

Phase 1: deterministic routing only.
Phase 2: unknown commands fall through to the AI when one is configured;
          local fallback message returned when none is.
Phase 5: the provider call itself moved out to app/core/ai/, so this
          module decides *what to say* about a failure rather than
          re-implementing *how to call* each provider. The important
          behavioural change is that every failure no longer produces the
          same "add an API key" sentence: the message a user sees now
          matches the actual cause (rate limit, expired key, unreachable
          local server, timeout), because a wrong diagnosis sends people
          to change settings that were never the problem.

The AI still only ever returns text. It cannot invoke a tool, and no
code path from here reaches the tool registry — see CLAUDE.md's Phase 2
rules and app/core/policy.py, which is the only risk decision-maker.
"""

from typing import Iterator, Optional, Tuple

from app.config import settings
from app.core.errors import ErrorCategory, SafeError, to_safe_error
from app.core.models import BrainResponse, CommandResponse
from app.core.system_prompt import SYSTEM_PROMPT
from app.core.tool_registry import registry
from app.logging_config import get_logger

logger = get_logger("brain")


class Brain:
    """Central orchestrator.  Initialises the tool registry and routes commands."""

    def __init__(self) -> None:
        self._ready = False

    # --- lifecycle ---

    def initialise(self) -> None:
        if self._ready:
            return

        from db.migrations import create_tables
        create_tables()

        from app.core import memory as memory_module
        from app.core import privacy as privacy_module
        from app.desktop import apps, clipboard, folders, maintenance, notes, screenshots, system, web
        from app.voice import tts as tts_module

        apps.register_tools(registry)
        screenshots.register_tools(registry)
        system.register_tools(registry)
        memory_module.register_tools(registry)
        tts_module.register_tools(registry)
        maintenance.register_tools(registry)
        web.register_tools(registry)
        folders.register_tools(registry)
        notes.register_tools(registry)
        clipboard.register_tools(registry)
        privacy_module.register_tools(registry)
        self._register_utility_tools()

        self._ready = True
        logger.info(
            "Brain initialised. %d tools registered. Claude API: %s",
            len(registry),
            "available" if settings.has_anthropic_key else "not configured",
        )

    # --- public API ---

    def is_configured(self) -> bool:
        """Return True when the Anthropic API key is present and non-empty."""
        return settings.has_anthropic_key

    def process(self, command: str) -> CommandResponse:
        if not self._ready:
            self.initialise()

        from app.core.router import CommandRouter
        router = CommandRouter(registry, brain=self)
        return router.route(command)

    # --- provider plumbing ---

    def provider_name(self) -> str:
        """A choice saved in Settings wins over the configured default —
        see app/core/preferences.py. Falling through to this module's own
        `settings` (rather than calling selected_provider(), which reads
        app.config directly) keeps the answer patchable when a test
        drives the Brain through a mocked settings object."""
        from app.core.preferences import get as get_preference
        from app.core.providers import normalise_provider

        return normalise_provider(get_preference("ai_provider") or settings.jarvis_ai_provider)

    def _provider_config(self):
        """The single place global settings become provider configuration.

        The key is resolved to "" whenever no key is configured, so a
        provider's own availability check and this class's is_configured()
        can never disagree about whether credentials exist."""
        from app.core.ai import ProviderConfig

        return ProviderConfig(
            model=settings.jarvis_ai_model or "",
            max_tokens=settings.jarvis_ai_max_tokens,
            timeout_seconds=float(settings.jarvis_ai_timeout_seconds),
            api_key=settings.effective_api_key if settings.has_anthropic_key else "",
            ollama_model=self._ollama_model(),
        )

    @staticmethod
    def _ollama_model() -> str:
        from app.core.preferences import get as get_preference

        configured = getattr(settings, "jarvis_ollama_model", "")
        if not isinstance(configured, str):
            configured = ""  # a mocked settings object in tests
        return get_preference("ollama_model") or configured

    def provider(self):
        from app.core.ai import get_provider

        return get_provider(self.provider_name(), self._provider_config())

    def provider_ready(self) -> Tuple[bool, str]:
        """(usable right now, why not). Never raises — it is called from
        request handlers and page renders."""
        try:
            availability = self.provider().availability()
            return bool(availability.ready), availability.reason
        except Exception:  # noqa: BLE001
            logger.warning("Provider availability check failed.", exc_info=True)
            return False, "The AI provider could not be checked. Local commands still work normally."

    # --- generation ---

    def generate_response(self, command: str) -> BrainResponse:
        """Ask the configured provider to answer *command*.

        Never raises and never returns an unhandled failure: an
        unavailable provider, a rejected key, a rate limit or a timeout
        all produce a BrainResponse whose text says which of those it
        actually was.
        """
        from app.core.ai.base import GenerationCancelled, ProviderError
        from app.core.conversation import build_request_messages

        provider = self.provider()
        availability = provider.availability()
        if not availability.ready:
            return self._unavailable(availability, provider)

        try:
            reply = provider.generate(build_request_messages(command), SYSTEM_PROMPT)
        except ProviderError as exc:
            return self._provider_failed(exc, provider)
        except GenerationCancelled:
            return BrainResponse(
                content="Stopped.", provider=provider.name,
                model=provider.resolved_model(), used_api=False,
            )
        except Exception as exc:  # noqa: BLE001 — a provider that broke its own contract
            return self._provider_failed(
                ProviderError(ErrorCategory.PROVIDER_ERROR, cause=exc), provider
            )

        return BrainResponse(
            content=reply.content,
            provider=reply.provider,
            model=reply.model,
            used_api=reply.used_api,
        )

    def stream_response(self, command: str, cancel=None) -> Iterator[str]:
        """Yield answer text as it arrives. Raises ProviderError, which
        the caller turns into a SafeError — the streaming endpoint needs
        the classified failure mid-stream, where returning a BrainResponse
        is no longer possible."""
        from app.core.conversation import build_request_messages

        provider = self.provider()
        yield from provider.stream(build_request_messages(command), SYSTEM_PROMPT, cancel=cancel)

    # --- failure reporting ---

    def _unavailable(self, availability, provider) -> BrainResponse:
        """No usable provider. This is not an error — it is the normal
        state of a fresh install — so no correlation ID is minted and
        nothing is logged as a failure. The reason comes from the
        provider itself, which knows whether the cause is "no key yet"
        or "Ollama isn't running"."""
        return BrainResponse(
            content=availability.reason,
            provider="local",
            model=None,
            used_api=False,
        )

    def _provider_failed(self, exc, provider) -> BrainResponse:
        """A real failure. The raw exception is logged server-side with a
        correlation ID; the user gets the category's fixed safe message,
        plus the provider's own credential-free detail when it wrote one
        (e.g. which local models are actually installed)."""
        safe_error: SafeError = to_safe_error(
            exc.cause or exc,
            category=exc.category,
            context=f"{provider.name} generation",
        )
        message = safe_error.message
        if getattr(exc, "detail", ""):
            message = exc.detail
        return BrainResponse(
            content=message,
            provider=provider.name,
            model=provider.resolved_model(),
            used_api=False,
            error=safe_error,
        )

    def _register_utility_tools(self) -> None:
        from app.core.models import PermissionLevel, ToolCategory, ToolDefinition

        registry.register(
            ToolDefinition(
                name="help",
                description="List all available commands.",
                permission_level=PermissionLevel.SAFE,
                category=ToolCategory.UTILITY,
            ),
            self._help,
        )
        registry.register(
            ToolDefinition(
                name="status",
                description="Show JARVIS status.",
                permission_level=PermissionLevel.SAFE,
                category=ToolCategory.UTILITY,
            ),
            self._status,
        )
        registry.register(
            ToolDefinition(
                name="exit",
                description="Exit JARVIS CLI.",
                permission_level=PermissionLevel.SAFE,
                category=ToolCategory.UTILITY,
            ),
            self._exit,
        )

    def _help(self) -> dict:
        tools = registry.list_definitions()
        lines = ["Available commands and tools:\n"]
        lines.append(f"  {'Command':<30} {'Description'}")
        lines.append("  " + "-" * 60)
        for t in sorted(tools, key=lambda x: x.category):
            lines.append(f"  [{t.category.value}] {t.name:<26} {t.description}")
        return {"success": True, "message": "\n".join(lines), "data": None}

    def _status(self) -> dict:
        from app import __phase__, __version__
        ai_status = "configured" if settings.has_anthropic_key else "not configured"
        msg = (
            f"JARVIS {__version__} — {__phase__}\n"
            f"  Tools registered : {len(registry)}\n"
            f"  Claude API       : {ai_status}\n"
            f"  AI model         : {settings.jarvis_ai_model or 'default'}\n"
            f"  DB               : {settings.db_path}\n"
            f"  Log file         : {settings.log_file}"
        )
        return {"success": True, "message": msg, "data": None}

    def _exit(self) -> dict:
        return {"success": True, "message": "Goodbye! JARVIS shutting down.", "data": {"exit": True}}


# Module-level singleton — import this everywhere
brain = Brain()
