"""JARVIS Brain — orchestrates tools, routing, and (future) Claude AI.

Phase 1: deterministic routing only, no Claude API required.
Phase 2 (TODO): wire in Anthropic SDK when ANTHROPIC_API_KEY is present.
"""

from app.config import settings
from app.core.models import CommandResponse
from app.core.tool_registry import registry
from app.logging_config import get_logger

logger = get_logger("brain")


class Brain:
    """Central orchestrator.  Initialises the tool registry and routes commands."""

    def __init__(self) -> None:
        self._ready = False

    def initialise(self) -> None:
        if self._ready:
            return

        # Ensure DB tables exist before any tool runs
        from db.migrations import create_tables
        create_tables()

        # Register all tool modules
        from app.core import memory as memory_module
        from app.desktop import apps, screenshots, system

        apps.register_tools(registry)
        screenshots.register_tools(registry)
        system.register_tools(registry)
        memory_module.register_tools(registry)
        self._register_utility_tools()

        self._ready = True
        logger.info(
            "Brain initialised. %d tools registered. Claude API: %s",
            len(registry),
            "available" if settings.has_anthropic_key else "not configured",
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

    def process(self, command: str) -> CommandResponse:
        if not self._ready:
            self.initialise()

        from app.core.router import CommandRouter
        router = CommandRouter(registry)
        return router.route(command)

    # --- built-in tool handlers ---

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
        msg = (
            f"JARVIS {__version__} — {__phase__}\n"
            f"  Tools registered : {len(registry)}\n"
            f"  Claude API       : {'configured' if settings.has_anthropic_key else 'not configured'}\n"
            f"  DB               : {settings.jarvis_db_path}\n"
            f"  Log file         : {settings.jarvis_log_file}"
        )
        return {"success": True, "message": msg, "data": None}

    def _exit(self) -> dict:
        return {"success": True, "message": "Goodbye! JARVIS shutting down.", "data": {"exit": True}}


# Module-level singleton — import this everywhere
brain = Brain()
