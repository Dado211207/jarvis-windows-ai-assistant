import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.models import CommandResponse, PermissionLevel
from app.logging_config import get_logger

logger = get_logger("router")


@dataclass
class Route:
    pattern: str
    tool_name: str
    # Extracts kwargs from the regex match; None means no args
    arg_extractor: Optional[Callable] = None
    _compiled: Any = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self._compiled = re.compile(self.pattern, re.IGNORECASE)

    def match(self, command: str) -> Optional[Dict]:
        m = self._compiled.match(command.strip())
        if m is None:
            return None
        return self.arg_extractor(m) if self.arg_extractor else {}


ROUTES: List[Route] = [
    Route(r"^help$", "help"),
    Route(r"^status$", "status"),
    Route(r"^system\s+status$", "system_status"),
    # Phase 6: URL opener (before generic open_app to intercept "open website/url/site ...")
    Route(
        r"^open\s+(?:website|url|site)\s+(.+)$",
        "open_website",
        lambda m: {"url": m.group(1).strip()},
    ),
    # Phase 6: Dashboard shortcut (before generic open_app — "dashboard" is one word)
    Route(r"^open\s+(?:jarvis\s+)?dashboard$", "open_dashboard"),
    Route(r"^open\s+jarvis$", "open_dashboard"),
    # Phase 6: Folder opener — intercepts known folder names before the generic open_app
    Route(
        r"^open\s+(downloads?|documents?|desktop|pictures?|music|videos?)$",
        "open_folder",
        lambda m: {"folder_name": m.group(1).strip()},
    ),
    Route(
        r"^open\s+(notes?(?:\s+folder)?|jarvis\s+folder)$",
        "open_folder",
        lambda m: {"folder_name": m.group(1).strip()},
    ),
    # Generic single-word app opener (kept here to preserve existing test expectations)
    Route(
        r"^open\s+(\w+)$",
        "open_app",
        lambda m: {"app_name": m.group(1).lower()},
    ),
    # Phase 6: Multi-word app names (after generic — these patterns are more specific)
    Route(r"^open\s+file\s+explorer$", "open_app", lambda m: {"app_name": "file_explorer"}),
    Route(r"^(take\s+)?screenshot$", "take_screenshot"),
    Route(
        r"^memory\s+add\s+(.+)$",
        "add_memory",
        lambda m: {"content": m.group(1).strip()},
    ),
    Route(
        r"^memory\s+search\s+(.+)$",
        "search_memory",
        lambda m: {"query": m.group(1).strip()},
    ),
    Route(r"^exit$", "exit"),
    # TTS voice commands (Phase 3)
    Route(r"^speak\s+on$", "tts_enable"),
    Route(r"^speak\s+off$", "tts_disable"),
    Route(r"^speak\s+status$", "tts_status"),
    Route(r"^speak\s+test$", "tts_test"),
    Route(r"^stop\s+speaking$", "tts_stop"),
    # Maintenance commands (Phase 5 — approval required)
    Route(r"^clear\s+logs?$", "clear_logs"),
    # Phase 6: Dedicated system info commands
    Route(r"^disk\s+(?:space|usage)$", "disk_space"),
    Route(r"^show\s+disk(?:\s+usage)?$", "disk_space"),
    Route(r"^how\s+much\s+(?:space|disk).*$", "disk_space"),
    Route(r"^network\s+(?:status|info)$", "network_info"),
    Route(r"^show\s+(?:my\s+)?ip$", "network_info"),
    Route(r"^show\s+network(?:\s+info)?$", "network_info"),
    Route(r"^(?:battery|power)\s+status$", "battery_status"),
    # Phase 6: Note creation
    Route(
        r"^(?:create|write)\s+note\s+(.+)$",
        "create_note",
        lambda m: {"content": m.group(1).strip()},
    ),
]

# Human-readable metadata for APPROVAL_REQUIRED tools.
# Defines what the user sees in the approval card before confirming.
_PENDING_ACTION_META: Dict[str, Dict[str, str]] = {
    "clear_logs": {
        "action_name": "Clear All Action Logs",
        "description": (
            "Permanently deletes all action log entries from the local JARVIS database. "
            "This action cannot be undone."
        ),
        "risk_level": "medium",
    },
}


class CommandRouter:
    """Maps raw command strings to registered tools and executes them."""

    def __init__(self, registry, brain=None) -> None:
        self._registry = registry
        self._brain = brain

    def route(self, command: str) -> CommandResponse:
        cmd = command.strip()
        logger.info("Command received: %r", cmd)

        if not cmd:
            return CommandResponse(success=False, message="Empty command.")

        for route in ROUTES:
            kwargs = route.match(cmd)
            if kwargs is not None:
                logger.debug("Matched route -> tool '%s', kwargs=%s", route.tool_name, kwargs)
                return self._dispatch(route.tool_name, cmd, **kwargs)

        if self._brain is not None:
            return self._brain_response(cmd)

        return CommandResponse(
            success=False,
            message=(
                f"Unknown command: '{cmd}'. "
                "Type 'help' to see available commands."
            ),
        )

    def _brain_response(self, cmd: str) -> CommandResponse:
        """Delegate an unrecognised command to the Brain's AI layer."""
        br = self._brain.generate_response(cmd)
        try:
            from db.database import get_db
            db = get_db()
            db.add_conversation("user", cmd)
            db.add_conversation("assistant", br.content)
        except Exception:
            pass
        return CommandResponse(
            success=True,
            message=br.content,
            data={
                "provider": br.provider,
                "model": br.model,
                "used_api": br.used_api,
                **({"error": br.error} if br.error else {}),
            },
            tool_used="brain",
        )

    def _dispatch(self, tool_name: str, raw_cmd: str, **kwargs) -> CommandResponse:
        from db.database import get_db

        # Check permission before executing — intercept APPROVAL_REQUIRED to
        # create a pending action instead of failing silently.
        tool = self._registry.get(tool_name)
        if tool and tool.definition.permission_level == PermissionLevel.APPROVAL_REQUIRED:
            return self._create_pending_action(tool_name, raw_cmd, **kwargs)

        result = self._registry.execute(tool_name, **kwargs)
        try:
            get_db().log_action(
                command=raw_cmd,
                tool_name=tool_name,
                status="success" if result.get("success") else "failure",
                message=result.get("message", ""),
            )
        except Exception:
            pass  # never crash on logging
        return CommandResponse(
            success=result.get("success", False),
            message=result.get("message", ""),
            data=result.get("data"),
            tool_used=tool_name,
        )

    def _create_pending_action(self, tool_name: str, raw_cmd: str, **kwargs) -> CommandResponse:
        """Create a pending approval action instead of executing immediately."""
        from app.core.pending_actions import pending_store

        meta = _PENDING_ACTION_META.get(tool_name, {})
        action_name = meta.get("action_name", tool_name.replace("_", " ").title())
        description = meta.get("description", f"Execute tool '{tool_name}'.")
        risk_level = meta.get("risk_level", "medium")

        action = pending_store.create(
            command=raw_cmd,
            tool_name=tool_name,
            action_name=action_name,
            description=description,
            risk_level=risk_level,
            parameters=dict(kwargs),
        )

        logger.info("Pending action created: %s (id=%s)", tool_name, action.id)
        msg = (
            f"'{action_name}' requires your approval before it can execute. "
            f"Review and confirm on the Actions page, or use action ID: {action.id[:8]}…"
        )
        return CommandResponse(
            success=True,
            message=msg,
            data=action.model_dump(),
            tool_used=tool_name,
            requires_approval=True,
            pending_action_id=action.id,
        )
