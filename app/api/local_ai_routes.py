"""Local AI's own endpoints.

Four states with four different next steps, one action that starts an
already-installed Ollama, and one that proves the thing works by making
it answer. Nothing here downloads a model or installs a runtime — see
app/core/local_ai.py for why that is a deliberate boundary rather than a
missing feature.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.session import require_session_token
from app.core import local_ai
from app.logging_config import get_logger

logger = get_logger("api.local_ai")

router = APIRouter(tags=["local-ai"])


class LocalAIStatusResponse(BaseModel):
    status: str
    headline: str
    detail: str
    next_step: str
    models: List[str]
    selected_model: str
    recommended_model: str
    recommended_download: str
    recommended_why: str
    memory_gb: Optional[float] = None
    installed: bool
    can_start: bool
    download_url: str
    usable: bool


class StartResponse(BaseModel):
    started: bool
    message: str
    status: LocalAIStatusResponse


class VerifyRequest(BaseModel):
    model: str = ""


class VerifyResponse(BaseModel):
    ok: bool
    message: str
    model: str = ""
    reply: str = ""


def _status_response() -> LocalAIStatusResponse:
    state = local_ai.describe()
    return LocalAIStatusResponse(
        status=state.status,
        headline=state.headline,
        detail=state.detail,
        next_step=state.next_step,
        models=state.models,
        selected_model=state.selected_model,
        recommended_model=state.recommended_model,
        recommended_download=state.recommended_download,
        recommended_why=state.recommended_why,
        memory_gb=state.memory_gb,
        installed=state.installed,
        can_start=state.can_start,
        download_url=state.download_url,
        usable=state.usable,
    )


@router.get("/local-ai/status", response_model=LocalAIStatusResponse)
def local_ai_status() -> LocalAIStatusResponse:
    """Which of the four states this machine is in, and what to do next.

    Recomputed per request: Ollama can be installed or stopped while
    JARVIS is running, and a cached answer would be wrong exactly when
    someone is trying to find out why local AI stopped working.
    """
    return _status_response()


@router.post(
    "/local-ai/start",
    response_model=StartResponse,
    dependencies=[Depends(require_session_token)],
)
def start_local_ai() -> StartResponse:
    """Start an already-installed Ollama.

    Refused when nothing is installed — starting what is not there would
    fail confusingly, and this endpoint must never be mistaken for one
    that installs it.
    """
    if not local_ai.is_installed():
        return StartResponse(
            started=False,
            message=(
                "Ollama is not installed on this computer, so there is nothing to "
                "start. JARVIS does not install it for you."
            ),
            status=_status_response(),
        )

    if not local_ai.start_ollama():
        return StartResponse(
            started=False,
            message="Ollama could not be started. Starting it yourself will show why.",
            status=_status_response(),
        )

    answering = local_ai.wait_until_answering()
    return StartResponse(
        started=answering,
        message=(
            "Ollama is running."
            if answering
            else (
                "Ollama was started but has not answered yet. Give it a moment and "
                "refresh — a first start can take a while."
            )
        ),
        status=_status_response(),
    )


@router.post(
    "/local-ai/verify",
    response_model=VerifyResponse,
    dependencies=[Depends(require_session_token)],
)
def verify_local_ai(req: VerifyRequest) -> VerifyResponse:
    """Make the model answer, and report only what actually happened.

    This is the difference between "set up" and "working". A running
    server and a model file on disk prove neither that the model loads
    nor that it generates, and both fail in ways that would otherwise
    first appear when the user tried to hold a conversation.
    """
    result = local_ai.verify_with_real_inference(model=req.model)
    return VerifyResponse(
        ok=result.ok, message=result.message, model=result.model, reply=result.reply,
    )
