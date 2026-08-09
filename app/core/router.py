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
    # Bulk memory deletion — approval-required, same gate as clear_logs
    Route(r"^(?:clear|forget)\s+(?:all\s+)?(?:memory|memories)$", "clear_memory"),
    Route(r"^forget\s+everything$", "clear_memory"),
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
    # Phase 7: reading notes back. Listed before read_note's pattern so
    # "list notes" can never be parsed as a request to read a note called
    # "notes".
    Route(r"^(?:list|show|my)\s+notes$", "list_notes"),
    Route(r"^notes$", "list_notes"),
    Route(
        r"^(?:read|open)\s+note\s+(.+)$",
        "read_note",
        lambda m: {"filename": m.group(1).strip()},
    ),
    # Phase 7: time, processes, and locking the screen
    Route(r"^(?:what(?:'s| is)\s+the\s+)?time$", "current_time"),
    Route(r"^what\s+time\s+is\s+it\??$", "current_time"),
    Route(r"^(?:what(?:'s| is)\s+(?:the\s+)?)?date$", "current_time"),
    Route(r"^today(?:'s)?\s+date$", "current_time"),
    Route(r"^(?:top|show)\s+processes$", "top_processes"),
    Route(r"^what(?:'s| is)\s+using\s+(?:my\s+)?(?:memory|ram)\??$", "top_processes"),
    Route(r"^lock(?:\s+(?:my\s+)?(?:screen|pc|computer|workstation))?$", "lock_workstation"),
    # v0.2: Clipboard read (SENSITIVE — always approval-required, see app/desktop/clipboard.py)
    Route(r"^(?:read|show|check)\s+clipboard$", "read_clipboard"),
    Route(r"^clipboard$", "read_clipboard"),
    # v0.2: Privacy mode (see app/core/privacy.py)
    Route(r"^privacy\s+(?:mode\s+)?on$", "set_privacy_mode", lambda m: {"active": True}),
    Route(r"^privacy\s+(?:mode\s+)?off$", "set_privacy_mode", lambda m: {"active": False}),
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
    "clear_memory": {
        "action_name": "Delete All Saved Memories",
        "description": (
            "Permanently deletes every memory you asked JARVIS to remember. "
            "Your notes, chat history and action log are not affected. "
            "This cannot be undone."
        ),
        "risk_level": "medium",
    },
    "read_clipboard": {
        "action_name": "Read Clipboard",
        "description": (
            "Reads the current text clipboard contents and shows them to you. "
            "The clipboard may contain private information (passwords, personal "
            "messages, tokens) copied from another application."
        ),
        "risk_level": "medium",
    },
}


def find_route(command: str) -> Optional[Tuple[Route, Dict]]:
    """The first ROUTES entry matching *command*, with its extracted
    kwargs, or None.

    Exposed because the streaming chat endpoint must answer "will this go
    to a tool or to the AI?" *before* it decides whether to open a stream
    — and it has to get the same answer route() would, which means asking
    route()'s own matcher rather than keeping a second copy of the loop.
    Deterministic routes still take priority everywhere (CLAUDE.md's
    Phase 2 rules); this only lets a caller see that decision early.
    """
    cmd = command.strip()
    if not cmd:
        return None
    for route in ROUTES:
        kwargs = route.match(cmd)
        if kwargs is not None:
            return route, kwargs
    return None


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

        match = find_route(cmd)
        if match is not None:
            route, kwargs = match
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
        from app.core.runtime_state import RuntimeState, runtime
        runtime.try_transition(RuntimeState.THINKING, reason="waiting on AI provider")
        br = self._brain.generate_response(cmd)
        runtime.try_transition(RuntimeState.STANDBY, reason="AI response ready")

        # Persistence (and its privacy-mode gate) lives in one place —
        # app/core/conversation.py — because the streaming chat endpoint
        # has to make the identical decision, and two copies of "should
        # this be written?" eventually disagree.
        from app.core.conversation import record_exchange
        record_exchange(cmd, br.content)

        return CommandResponse(
            success=True,
            message=br.content,
            data={
                "provider": br.provider,
                "model": br.model,
                "used_api": br.used_api,
                **({"error": br.error.model_dump()} if br.error else {}),
            },
            tool_used="brain",
        )

    def _dispatch(self, tool_name: str, raw_cmd: str, **kwargs) -> CommandResponse:
        """Route a matched command to its tool via the v0.2 pipeline:
        classify risk -> policy decision -> record the proposal -> act on
        the decision -> record the result. policy.evaluate() is the single
        source of truth for auto-execute/require-approval/deny; the
        permission_level-based check that used to live directly in this
        method is gone (it produced the same decision for every tool
        registered without an explicit risk — see app/core/policy.py's
        risk_for() — so folding it into one call site removes a duplicate
        policy decision without changing behavior). registry.execute()
        still independently re-checks permission right before the handler
        runs; that is intentional defense in depth, not a second authority.
        """
        from db.database import get_db
        from app.core.action_lifecycle import transition as lifecycle_transition
        from app.core.action_lifecycle import propose
        from app.core.events import EventType, event_bus
        from app.core.models import ActionLifecycleStatus
        from app.core.policy import PolicyAction, evaluate, risk_for
        from app.core.runtime_state import RuntimeState, runtime

        tool = self._registry.get(tool_name)
        permission_level = tool.definition.permission_level if tool else PermissionLevel.SAFE
        declared_risk = tool.definition.risk if tool else None
        risk = risk_for(permission_level, declared_risk)
        policy_result = evaluate(risk, tool_name)

        record = propose(
            tool_name,
            dict(kwargs),
            risk=risk.value,
            policy_action=policy_result.action.value,
            policy_reason=policy_result.reason,
        )
        event_bus.publish(
            EventType.ACTION_PROPOSED,
            {
                "action_id": record.id,
                "tool_name": tool_name,
                "risk": risk.value,
                "policy_action": policy_result.action.value,
                "input_summary": record.input_summary,
            },
            correlation_id=record.id,
        )

        if policy_result.action == PolicyAction.DENY:
            lifecycle_transition(record.id, ActionLifecycleStatus.BLOCKED)
            event_bus.publish(
                EventType.ACTION_RESULT,
                {
                    "action_id": record.id, "tool_name": tool_name,
                    "success": False, "message": policy_result.reason,
                },
                correlation_id=record.id,
            )
            logger.warning("Dispatch denied by policy: %s (%s)", tool_name, policy_result.reason)
            return CommandResponse(success=False, message=policy_result.reason, tool_used=tool_name)

        if policy_result.action == PolicyAction.REQUIRE_APPROVAL:
            return self._create_pending_action(tool_name, raw_cmd, record.id, **kwargs)

        # PolicyAction.AUTO_EXECUTE
        runtime.try_transition(RuntimeState.EXECUTING, correlation_id=record.id, reason=f"executing {tool_name}")
        lifecycle_transition(record.id, ActionLifecycleStatus.EXECUTING)

        result = self._registry.execute(tool_name, **kwargs)

        runtime.try_transition(RuntimeState.STANDBY, correlation_id=record.id, reason="dispatch complete")
        result_data = result.get("data") or {}
        final_status = ActionLifecycleStatus.SUCCEEDED if result.get("success") else ActionLifecycleStatus.FAILED
        lifecycle_fields = {"result_summary": str(result.get("message", ""))[:500]}
        if final_status == ActionLifecycleStatus.FAILED:
            lifecycle_fields["error_category"] = result_data.get("error_category", "tool_execution_failed")
        lifecycle_transition(record.id, final_status, **lifecycle_fields)
        event_bus.publish(
            EventType.ACTION_RESULT,
            {
                "action_id": record.id, "tool_name": tool_name,
                "success": result.get("success", False), "message": result.get("message", ""),
                "timed_out": bool(result_data.get("timed_out", False)),
            },
            correlation_id=record.id,
        )

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

    def _create_pending_action(self, tool_name: str, raw_cmd: str, lifecycle_id: str, **kwargs) -> CommandResponse:
        """Create a pending approval action instead of executing immediately.

        *lifecycle_id* is the id already assigned by _dispatch's propose()
        call; passing it into pending_store.create() gives the live
        approval queue entry and the persisted audit record the same id,
        so a client can correlate a WebSocket event, a /actions/{id}
        lookup, and the audit trail as one action throughout its life.
        """
        from app.core.pending_actions import pending_store
        from app.core.action_lifecycle import transition as lifecycle_transition
        from app.core.events import EventType, event_bus
        from app.core.models import ActionLifecycleStatus
        from app.core.runtime_state import RuntimeState, runtime

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
            id=lifecycle_id,
        )

        runtime.try_transition(
            RuntimeState.AWAITING_APPROVAL, correlation_id=lifecycle_id,
            reason=f"awaiting approval for {tool_name}",
        )
        lifecycle_transition(lifecycle_id, ActionLifecycleStatus.PENDING_APPROVAL)
        event_bus.publish(
            EventType.ACTION_APPROVAL_CHANGED,
            {
                "action_id": lifecycle_id,
                "tool_name": tool_name,
                "status": "pending_approval",
                "action_name": action_name,
                "expires_at": action.expires_at.isoformat() if action.expires_at else None,
            },
            correlation_id=lifecycle_id,
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
