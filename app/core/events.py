"""Typed event envelope + in-memory event bus for the WebSocket stream.

A single, shared, thread-safe, bounded event log. Synchronous code (the
router, the policy engine, the runtime state machine — none of which are
async) appends events here; the async WebSocket endpoint (app/api/ws.py)
polls it. This sidesteps the need for cross-thread-to-event-loop callbacks
(publishing from a worker thread into an asyncio queue safely requires
`loop.call_soon_threadsafe`, which the rest of this codebase has no
infrastructure for yet) at the cost of up to one poll interval of latency —
an explicit, documented tradeoff, not an oversight.

Never put a secret, a raw exception, an environment value, or a full
sensitive tool input into an event payload. Event payloads are meant to be
broadcast to any connected browser tab.
"""

import itertools
import threading
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

_MAX_EVENTS = 500


class EventType(str, Enum):
    RUNTIME_STATE = "runtime_state"
    ASSISTANT_MESSAGE = "assistant_message"
    VOICE_STATE = "voice_state"
    ACTION_PROPOSED = "action_proposed"
    ACTION_APPROVAL_CHANGED = "action_approval_changed"
    ACTION_PROGRESS = "action_progress"
    ACTION_RESULT = "action_result"
    SYSTEM_HEALTH = "system_health"
    PROVIDER_STATUS = "provider_status"
    # Coding Workspace activity. Its own type rather than ACTION_PROGRESS
    # so the Actions page, which listens for that, does not start
    # rendering coding steps it has no way to display.
    CODING_ACTIVITY = "coding_activity"
    ERROR = "error"


class Event(BaseModel):
    seq: int
    type: EventType
    timestamp: datetime
    correlation_id: Optional[str] = None
    payload: Dict[str, Any]


class EventBus:
    """Thread-safe, bounded, append-only event log with monotonic sequence
    numbers so a client can ask "what have I missed since seq N" on
    reconnect, rather than only ever seeing events from the moment it
    connects."""

    def __init__(self, maxlen: int = _MAX_EVENTS) -> None:
        self._lock = threading.Lock()
        self._events: deque = deque(maxlen=maxlen)
        self._seq = itertools.count(1)

    def publish(
        self,
        event_type: EventType,
        payload: Dict[str, Any],
        correlation_id: Optional[str] = None,
    ) -> Event:
        event = Event(
            seq=next(self._seq),
            type=event_type,
            timestamp=datetime.now(timezone.utc),
            correlation_id=correlation_id,
            payload=payload,
        )
        with self._lock:
            self._events.append(event)
        return event

    def since(self, last_seq: int) -> List[Event]:
        """Events with seq > last_seq, oldest first. Pass 0 to get
        everything currently retained (bounded by maxlen)."""
        with self._lock:
            return [e for e in self._events if e.seq > last_seq]

    def latest_seq(self) -> int:
        with self._lock:
            return self._events[-1].seq if self._events else 0


# Module-level singleton — mirrors pending_actions.py's existing pattern.
event_bus = EventBus()
