"""Single authoritative runtime state model.

One state machine, one place transitions are validated, one place state
changes turn into events for the WebSocket stream. Nothing else in this
codebase is allowed to track "what is JARVIS doing right now" independently
of this module — see CLAUDE.md's "register, don't hard-code" principle
applied to state, not just tools.
"""

import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional, Set

from app.core.events import EventType, event_bus
from app.logging_config import get_logger

logger = get_logger("runtime_state")


class RuntimeState(str, Enum):
    BOOTING = "booting"
    STANDBY = "standby"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    SPEAKING = "speaking"
    ERROR = "error"
    OFFLINE = "offline"


# Explicit adjacency list — every legal transition, nothing implicit.
# STANDBY is the hub: almost everything returns there, and almost anything
# can jump to ERROR (a failure can happen from any active state) or OFFLINE
# (a shutdown/disconnect can happen from any state at all, including ERROR).
_TRANSITIONS: Dict[RuntimeState, Set[RuntimeState]] = {
    RuntimeState.BOOTING: {RuntimeState.STANDBY, RuntimeState.ERROR, RuntimeState.OFFLINE},
    RuntimeState.STANDBY: {
        RuntimeState.LISTENING, RuntimeState.THINKING, RuntimeState.EXECUTING,
        RuntimeState.SPEAKING, RuntimeState.AWAITING_APPROVAL,
        RuntimeState.ERROR, RuntimeState.OFFLINE,
    },
    RuntimeState.LISTENING: {
        RuntimeState.TRANSCRIBING, RuntimeState.STANDBY, RuntimeState.ERROR, RuntimeState.OFFLINE,
    },
    RuntimeState.TRANSCRIBING: {
        RuntimeState.THINKING, RuntimeState.STANDBY, RuntimeState.ERROR, RuntimeState.OFFLINE,
    },
    RuntimeState.THINKING: {
        RuntimeState.AWAITING_APPROVAL, RuntimeState.EXECUTING, RuntimeState.SPEAKING,
        RuntimeState.STANDBY, RuntimeState.ERROR, RuntimeState.OFFLINE,
    },
    RuntimeState.AWAITING_APPROVAL: {
        RuntimeState.EXECUTING,  # approved
        RuntimeState.STANDBY,    # cancelled or expired
        RuntimeState.ERROR, RuntimeState.OFFLINE,
    },
    RuntimeState.EXECUTING: {
        RuntimeState.SPEAKING, RuntimeState.STANDBY, RuntimeState.ERROR, RuntimeState.OFFLINE,
    },
    RuntimeState.SPEAKING: {
        RuntimeState.STANDBY, RuntimeState.LISTENING, RuntimeState.ERROR, RuntimeState.OFFLINE,
    },
    RuntimeState.ERROR: {RuntimeState.STANDBY, RuntimeState.OFFLINE},
    RuntimeState.OFFLINE: {RuntimeState.BOOTING},
}


class InvalidTransitionError(Exception):
    def __init__(self, current: RuntimeState, target: RuntimeState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Cannot transition from {current.value} to {target.value}")


class RuntimeStateMachine:
    """Thread-safe. One instance for the whole process (see the module-level
    singleton below) — there is exactly one JARVIS runtime state at a time,
    matching the single-user, single-machine architecture this app is built
    for (see docs/THREAT_MODEL.md)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = RuntimeState.BOOTING
        self._changed_at = datetime.now(timezone.utc)

    @property
    def state(self) -> RuntimeState:
        with self._lock:
            return self._state

    def can_transition(self, target: RuntimeState) -> bool:
        with self._lock:
            return target in _TRANSITIONS.get(self._state, set())

    def transition(
        self,
        target: RuntimeState,
        correlation_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> RuntimeState:
        """Validates and performs the transition, then publishes a typed
        event. Raises InvalidTransitionError rather than silently clamping
        to a "closest legal" state — an illegal transition means a caller's
        assumption about what JARVIS was doing was wrong, and that should
        surface immediately, not be papered over."""
        with self._lock:
            current = self._state
            if target not in _TRANSITIONS.get(current, set()):
                raise InvalidTransitionError(current, target)
            self._state = target
            self._changed_at = datetime.now(timezone.utc)

        logger.info(
            "Runtime state: %s -> %s%s", current.value, target.value,
            f" ({reason})" if reason else "",
        )
        event_bus.publish(
            EventType.RUNTIME_STATE,
            {"from": current.value, "to": target.value, "reason": reason},
            correlation_id=correlation_id,
        )
        return target

    def try_transition(
        self,
        target: RuntimeState,
        correlation_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """Best-effort transition for call sites (like command dispatch)
        that must never fail the caller's real work over a UI-state
        conflict. FastAPI can run sync request handlers concurrently in
        its threadpool, so two dispatches racing against this one shared
        state machine is a real possibility, not a hypothetical — unlike
        transition(), this never raises; it logs and returns False instead.
        The state machine reflects reality for the dashboard, it does not
        gate whether an action is allowed to run — that is policy.py's job.
        """
        try:
            self.transition(target, correlation_id=correlation_id, reason=reason)
            return True
        except InvalidTransitionError:
            logger.warning(
                "Skipped runtime state transition to %s (currently %s, likely a race).",
                target.value, self.state.value,
            )
            return False

    def force_state(self, target: RuntimeState) -> None:
        """Bypasses transition validation — for test setup and startup/
        shutdown only. Never call this from request-handling code; use
        transition() so illegal jumps are caught."""
        with self._lock:
            self._state = target
            self._changed_at = datetime.now(timezone.utc)


# Module-level singleton — mirrors pending_actions.py's existing pattern.
runtime = RuntimeStateMachine()
