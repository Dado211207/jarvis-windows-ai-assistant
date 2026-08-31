"""Streaming chat, stop-generation, and conversation reset.

**Why a separate transport at all.** POST /command answers in one piece,
which is right for a tool result and wrong for a paragraph of generated
text: the user stares at a disabled button for ten seconds with no
evidence anything is happening. This endpoint streams the same answer as
it is produced.

**Why not the WebSocket.** CLAUDE.md makes /ws/events read-only — it must
never accept a command — and it is a *broadcast* bus. Chat content
belongs to one request from one page, not to every connected client, so
it goes back down the response body of the request that asked for it.
Submission stays a session-token-protected POST, exactly like /command.

**Deterministic routes still win.** This endpoint asks the router first
(app/core/router.py::find_route). A command that matches a route is
executed through the ordinary /command path — same policy engine, same
approval gate, same audit trail — and returned as a single `routed`
event. Only a command that would have fallen through to the AI is ever
streamed. There is no second dispatch path here and no way to reach a
tool through this endpoint that /command would not have reached.

**Server-sent event framing** over a plain streaming POST response:
each line is `data: {json}\n\n`. Event shapes:

    {"type": "start",   "generation_id": ..., "provider": ..., "model": ...}
    {"type": "routed",  "response": {...CommandResponse...}}
    {"type": "delta",   "text": "..."}
    {"type": "error",   "message": "...", "error": {...SafeError...}|null}
    {"type": "done",    "stopped": bool, "used_api": bool, "persisted": bool}

`done` is always the last event, including after `error` — a client that
waits for it never hangs.
"""

import json
from typing import Iterator, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.session import require_session_token
from app.logging_config import get_logger

logger = get_logger("api.chat")

router = APIRouter()


class ChatStreamRequest(BaseModel):
    command: str


class StopGenerationRequest(BaseModel):
    # Optional: a page that reloaded mid-stream has lost the id but can
    # still legitimately ask for whatever is running to stop.
    generation_id: Optional[str] = None


class StopGenerationResponse(BaseModel):
    stopped: bool
    count: int
    message: str


class ConversationResetResponse(BaseModel):
    success: bool
    removed: int
    message: str


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _stream_events(command: str) -> Iterator[str]:
    """The generator behind the response body.

    Never raises: an exception escaping here would abort the response
    mid-body, leaving the browser with a half-read stream and no
    explanation. Every failure becomes an `error` event followed by
    `done`.
    """
    from app.core.ai.base import GenerationCancelled, ProviderError
    from app.core.brain import brain
    from app.core.conversation import record_exchange
    from app.core.errors import ErrorCategory, to_safe_error
    from app.core.generation import generations
    from app.core.router import find_route
    from app.core.runtime_state import RuntimeState, runtime

    text = (command or "").strip()
    if not text:
        yield _event({"type": "error", "message": "Empty command.", "error": None})
        yield _event({"type": "done", "stopped": False, "used_api": False, "persisted": False})
        return

    # A deterministic command is not streamed — it is executed exactly as
    # POST /command would execute it, through the policy engine and the
    # approval gate, and returned whole.
    if find_route(text) is not None:
        try:
            response = brain.process(text)
            yield _event({"type": "routed", "response": json.loads(response.model_dump_json())})
        except Exception as exc:  # noqa: BLE001
            safe = to_safe_error(exc, category=ErrorCategory.INTERNAL_ERROR, context="routed command")
            yield _event({"type": "error", "message": safe.message, "error": safe.model_dump()})
        yield _event({"type": "done", "stopped": False, "used_api": False, "persisted": False})
        return

    provider = brain.provider()
    availability = provider.availability()
    if not availability.ready:
        # Not an error: a fresh install with no key is the normal state.
        yield _event({"type": "start", "generation_id": "", "provider": "local", "model": ""})
        yield _event({"type": "delta", "text": availability.reason})
        yield _event({"type": "done", "stopped": False, "used_api": False, "persisted": False})
        return

    generation_id, token = generations.start()
    collected = []
    stopped = False
    failed = False

    yield _event({
        "type": "start",
        "generation_id": generation_id,
        "provider": provider.name,
        "model": provider.resolved_model(),
    })
    runtime.try_transition(RuntimeState.THINKING, correlation_id=generation_id, reason="streaming AI response")

    try:
        for delta in brain.stream_response(text, cancel=token):
            # Re-checked here, not only inside the provider. The providers
            # do check between chunks, but "Stop means nothing more
            # appears on screen" is a promise made at this boundary, and
            # it should not depend on every present and future provider
            # implementing its half correctly.
            if token.cancelled:
                stopped = True
                break
            collected.append(delta)
            yield _event({"type": "delta", "text": delta})
    except GenerationCancelled:
        stopped = True
    except ProviderError as exc:
        from app.core.ai.events import record_provider_failure

        failed = True
        safe = to_safe_error(
            exc.cause or exc, category=exc.category, context=f"{provider.name} streaming generation"
        )
        message = exc.detail or safe.message
        # The streaming path gets the same safe Logs row as /command —
        # the failure is identical; only the transport differs.
        record_provider_failure(
            provider=provider.name,
            category=exc.category,
            correlation_id=safe.correlation_id,
            detail=exc.detail or None,
        )
        yield _event({"type": "error", "message": message, "error": safe.model_dump()})
    except Exception as exc:  # noqa: BLE001 — a provider that broke its own contract
        failed = True
        safe = to_safe_error(exc, category=ErrorCategory.PROVIDER_ERROR, context="streaming generation")
        yield _event({"type": "error", "message": safe.message, "error": safe.model_dump()})
    finally:
        generations.finish(generation_id)
        runtime.try_transition(RuntimeState.STANDBY, correlation_id=generation_id, reason="generation ended")

    # A stopped generation still persists what was actually produced: the
    # user saw those words, and a follow-up question that refers to them
    # would otherwise make no sense. A failed one persists nothing —
    # there is no answer to remember.
    answer = "".join(collected)
    persisted = bool(answer) and not failed and record_exchange(text, answer)

    yield _event({
        "type": "done",
        "stopped": stopped,
        "used_api": not failed,
        "persisted": persisted,
    })


@router.post("/chat/stream", dependencies=[Depends(require_session_token)])
def chat_stream(req: ChatStreamRequest) -> StreamingResponse:
    return StreamingResponse(
        _stream_events(req.command),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # Belt and braces for any proxy that might sit in front of a
            # loopback server (none does today); without it a buffering
            # proxy turns a stream back into one lump.
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/chat/stop",
    response_model=StopGenerationResponse,
    dependencies=[Depends(require_session_token)],
)
def chat_stop(req: StopGenerationRequest) -> StopGenerationResponse:
    """Stop a generation. Stopping something that already finished is not
    an error — it is a race the user cannot see and should not be shown
    as a failure."""
    from app.core.generation import generations

    if req.generation_id:
        stopped = generations.stop(req.generation_id)
        return StopGenerationResponse(
            stopped=stopped,
            count=1 if stopped else 0,
            message="Stopping." if stopped else "That response had already finished.",
        )

    count = generations.stop_all()
    return StopGenerationResponse(
        stopped=count > 0,
        count=count,
        message="Stopping." if count else "Nothing is generating right now.",
    )


@router.post(
    "/conversation/reset",
    response_model=ConversationResetResponse,
    dependencies=[Depends(require_session_token)],
)
def conversation_reset() -> ConversationResetResponse:
    """Delete stored chat history. Irreversible, and scoped to the
    conversations table — the action audit trail is deliberately not
    touched (see app/core/conversation.py::reset)."""
    from app.core.conversation import reset

    try:
        removed = reset()
    except Exception as exc:  # noqa: BLE001
        from app.core.errors import ErrorCategory, to_safe_error

        safe = to_safe_error(exc, category=ErrorCategory.INTERNAL_ERROR, context="conversation reset")
        return ConversationResetResponse(success=False, removed=0, message=safe.message)

    return ConversationResetResponse(
        success=True,
        removed=removed,
        message=(
            f"Chat history cleared — {removed} message(s) removed."
            if removed else "There was no chat history to clear."
        ),
    )
