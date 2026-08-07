"""Typed WebSocket event stream (v0.2) — read-only server-to-client push
of app/core/events.py's EventBus.

This does not accept commands: command submission and approval stay on
the existing REST endpoints (app/api/routes.py, app/api/actions.py). The
socket exists purely so the dashboard can reflect runtime state, action
lifecycle, and provider/system health in real time instead of polling.

There is no asyncio event queue in this codebase yet — every existing
route is a plain sync `def`. Rather than introduce one, this bridges the
thread-safe, synchronous EventBus by polling it on a short interval
inside this async handler. Simple and correct, at the cost of up to
POLL_INTERVAL_SECONDS of added latency, which is acceptable for a local
dashboard.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.api.origin import is_allowed_origin
from app.core.events import Event, EventType, event_bus
from app.core.runtime_state import runtime
from app.logging_config import get_logger

logger = get_logger("api.ws")

router = APIRouter()

POLL_INTERVAL_SECONDS = 0.5


@router.websocket("/ws/events")
async def stream_events(websocket: WebSocket, since: Optional[int] = None) -> None:
    """Stream typed events to a connected dashboard client.

    Optional query param `since`: resume after this sequence number
    (for a reconnecting client that wants to catch up on the backlog
    still held in the bus). Omit to receive only new events from now on.
    """
    origin = websocket.headers.get("origin")
    if not is_allowed_origin(origin):
        logger.warning("Rejected WebSocket handshake from disallowed origin: %s", origin)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    logger.info("WebSocket client connected (since=%s)", since)

    # Snapshot: tell a newly (re)connected client the current runtime
    # state immediately, so the UI is correct before any new transition
    # happens. Reuses the same Event shape the client already knows how
    # to render rather than a separate "hello" protocol. seq=0 always, so
    # it can never be mistaken for a real bookmark by a client that
    # tracks max(seq seen) — real sequence numbers start at 1.
    snapshot = Event(
        seq=0,
        type=EventType.RUNTIME_STATE,
        timestamp=datetime.now(timezone.utc),
        payload={"from": None, "to": runtime.state.value, "reason": "snapshot"},
    )
    await websocket.send_text(snapshot.model_dump_json())

    last_seq = since if since is not None else event_bus.latest_seq()

    try:
        while True:
            for event in event_bus.since(last_seq):
                await websocket.send_text(event.model_dump_json())
                last_seq = event.seq
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected.")
    except Exception:
        # A stream loop must not take the whole server down over one
        # connection's transport error; log it and let this handler exit.
        logger.exception("WebSocket stream error; closing this connection.")
