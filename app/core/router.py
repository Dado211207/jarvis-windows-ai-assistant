import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.models import CommandResponse
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
    Route(
        r"^open\s+(\w+)$",
        "open_app",
        lambda m: {"app_name": m.group(1).lower()},
    ),
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
]


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
