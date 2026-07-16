"""JARVIS Brain — orchestrates tools, routing, and Claude AI.

Phase 1: deterministic routing only.
Phase 2: unknown commands fall through to the Anthropic API when a key is present;
          local fallback message returned when no key is configured.
"""

from app.config import settings
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
        from app.core import preferences as preferences_module
        from app.core import settings_service
        from app.desktop import apps, folders, maintenance, notes, screenshots, system, web
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
        settings_service.register_tools(registry)
        preferences_module.register_tools(registry)
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

    def generate_response(self, command: str) -> BrainResponse:
        """Call the Anthropic API with *command* as the user message.

        Falls back to a local message if the key is absent or the call fails.
        """
        if not self.is_configured():
            return self._local_fallback(command)

        model = settings.jarvis_ai_model or "claude-haiku-4-5-20251001"
        try:
            import anthropic
            client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key,
                timeout=float(settings.jarvis_ai_timeout_seconds),
            )
            message = client.messages.create(
                model=model,
                max_tokens=settings.jarvis_ai_max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": command}],
            )
            content = message.content[0].text if message.content else ""
            logger.info("Claude API response received. model=%s tokens=%s", model, message.usage)
            return BrainResponse(
                content=content,
                provider=settings.jarvis_ai_provider,
                model=model,
                used_api=True,
            )
        except Exception as exc:
            logger.warning("Claude API call failed (%s), using local fallback.", exc)
            return BrainResponse(
                content=self._local_fallback(command).content,
                provider=settings.jarvis_ai_provider,
                model=model,
                used_api=False,
                error=str(exc),
            )

    # --- private helpers ---

    def _local_fallback(self, command: str) -> BrainResponse:
        """Return a polite message when no API key is set or the call fails."""
        msg = (
            f"I received your message: \"{command}\"\n"
            "Claude AI is not configured. "
            "Add your ANTHROPIC_API_KEY to .env to enable AI responses."
        )
        return BrainResponse(
            content=msg,
            provider="local",
            used_api=False,
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
            f"  DB               : {settings.jarvis_db_path}\n"
            f"  Log file         : {settings.jarvis_log_file}"
        )
        return {"success": True, "message": msg, "data": None}

    def _exit(self) -> dict:
        return {"success": True, "message": "Goodbye! JARVIS shutting down.", "data": {"exit": True}}


# Module-level singleton — import this everywhere
brain = Brain()
