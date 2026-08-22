"""Local AI's own endpoints.

Ten states with ten different next steps, and the actions that move
between them: install Ollama, start it, download a model, and prove the
result works by making it answer.

Every action is a POST that a person triggers, protected by the session
token like every other mutating route. Nothing here runs on a GET, on
startup, or as a side effect of anything else — `/local-ai/plan` exists
precisely so that "what would this download?" can be answered without
downloading it.
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
    hardware: str = ""
    free_disk_gb: Optional[float] = None
    percent: int = 0
    busy: bool = False
    installed_by_jarvis: bool = False


class InstallPlanResponse(BaseModel):
    """What a person is agreeing to, before anything is fetched."""

    url: str
    host: str
    publisher: str
    publisher_url: str
    licence: str
    licence_url: str
    approximate_size: str
    verification: str
    installs: str
    already_installed: bool
    model: str
    model_download: str
    model_why: str
    hardware: str
    enough_disk: Optional[bool] = None


class ActionResponse(BaseModel):
    started: bool
    message: str
    status: LocalAIStatusResponse


class PullRequest(BaseModel):
    model: str = ""


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


def _unreadable(reason: str) -> LocalAIStatusResponse:
    """A status the page can render when the machine could not be read.

    Settings polls this endpoint while a download runs. A 500 there would
    stop the progress bar and leave a user watching a frozen screen with
    no idea whether their download is still going.
    """
    return LocalAIStatusResponse(
        status=local_ai.FAILED,
        headline="Local AI could not be checked.",
        detail=reason,
        next_step="Press Re-check. Chat with Claude is unaffected.",
        models=[], selected_model="", recommended_model="",
        recommended_download="", recommended_why="",
        installed=False, can_start=False,
        download_url=local_ai.OLLAMA_DOWNLOAD_URL, usable=False,
    )


def _status_response() -> LocalAIStatusResponse:
    try:
        state = local_ai.describe()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Local AI status could not be computed.", exc_info=True)
        return _unreadable(f"This computer's local AI state could not be read ({exc}).")
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
        hardware=state.hardware,
        free_disk_gb=state.free_disk_gb,
        percent=state.percent,
        busy=state.busy,
        installed_by_jarvis=state.installed_by_jarvis,
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


@router.get("/local-ai/plan", response_model=InstallPlanResponse)
def local_ai_plan() -> InstallPlanResponse:
    """Exactly what setting up local AI would download, before it does.

    A GET, and it fetches nothing: this is the screen somebody reads to
    decide. The size of the model and whether it fits on this machine are
    computed here rather than asserted, so a computer with 3 GB free is
    told so before the download rather than after it.
    """
    from app.core import local_ai_install, machine

    hardware = machine.inspect()
    state = local_ai.describe()
    suggestion = local_ai.recommend_model(hardware.memory_gb)
    plan = local_ai_install.plan()

    return InstallPlanResponse(
        url=plan.url,
        host=plan.host,
        publisher=plan.publisher,
        publisher_url=plan.publisher_url,
        licence=plan.licence,
        licence_url=plan.licence_url,
        approximate_size=plan.approximate_size,
        verification=plan.verification,
        installs=plan.installs,
        already_installed=plan.already_installed,
        model=suggestion.name,
        model_download=suggestion.approximate_download,
        model_why=suggestion.why,
        hardware=state.hardware,
        enough_disk=hardware.can_fit(_download_gb(suggestion.approximate_download)),
    )


def _download_gb(approximate: str) -> float:
    """The number out of "around 5 GB", for the disk check.

    The wording is written for people; this is the only place it is read
    as a figure, and a value it cannot parse becomes a conservative 6 GB
    rather than a zero that would make every disk look big enough.
    """
    import re

    match = re.search(r"(\d+(?:\.\d+)?)", approximate or "")
    return float(match.group(1)) if match else 6.0


@router.post(
    "/local-ai/install",
    response_model=ActionResponse,
    dependencies=[Depends(require_session_token)],
)
def install_local_ai() -> ActionResponse:
    """Download and install Ollama, after the user has seen the plan.

    Refused when Ollama is already here: an existing installation is the
    user's, and JARVIS does not reinstall over it.
    """
    from app.core import local_ai_install

    if local_ai.is_installed():
        return ActionResponse(
            started=False,
            message=(
                "Ollama is already installed on this computer, so JARVIS left it alone."
            ),
            status=_status_response(),
        )

    started = local_ai_install.ollama_installer.start()
    return ActionResponse(
        started=started,
        message=(
            "Downloading Ollama. JARVIS checks the file is signed by Ollama before "
            "running it."
            if started
            else local_ai_install.ollama_installer.state().message
            or "Local AI setup is already running."
        ),
        status=_status_response(),
    )


@router.post(
    "/local-ai/install/cancel",
    response_model=ActionResponse,
    dependencies=[Depends(require_session_token)],
)
def cancel_local_ai_install() -> ActionResponse:
    from app.core import local_ai_install

    local_ai_install.ollama_installer.cancel()
    return ActionResponse(
        started=False,
        message="Stopping. Nothing that has not already been installed will be.",
        status=_status_response(),
    )


@router.post(
    "/local-ai/pull",
    response_model=ActionResponse,
    dependencies=[Depends(require_session_token)],
)
def pull_model(req: PullRequest) -> ActionResponse:
    """Download a model into Ollama.

    The model defaults to the one recommended for this machine rather
    than to a fixed name, so pressing the button on a small computer does
    not start a download that will not run on it.
    """
    from app.core import local_ai_models

    if not local_ai.is_installed():
        return ActionResponse(
            started=False,
            message="Ollama is not installed yet, so there is nowhere to put a model.",
            status=_status_response(),
        )

    model = (req.model or "").strip() or local_ai.recommend_model().name
    started = local_ai_models.model_puller.start(model)
    return ActionResponse(
        started=started,
        message=(
            f"Downloading {model}. You can cancel at any point; what has already "
            "downloaded is kept."
            if started
            else "A model download is already running."
        ),
        status=_status_response(),
    )


@router.post(
    "/local-ai/pull/cancel",
    response_model=ActionResponse,
    dependencies=[Depends(require_session_token)],
)
def cancel_pull() -> ActionResponse:
    from app.core import local_ai_models

    local_ai_models.model_puller.cancel()
    return ActionResponse(
        started=False,
        message="Stopping the download. What already arrived is kept.",
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
