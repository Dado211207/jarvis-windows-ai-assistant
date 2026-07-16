"""Onboarding API — first-run setup wizard for the installed Windows app.

Local-only, same as every other JARVIS endpoint. The raw API key is accepted
here (over the loopback-only API, never logged) but is never returned back
to the browser — only a masked form and success/error state.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.core import onboarding
from app.logging_config import get_logger

logger = get_logger("api.onboarding")

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class StepUpdate(BaseModel):
    step: str


class ApiKeySubmit(BaseModel):
    api_key: str


class BoolPreference(BaseModel):
    enabled: bool


@router.get("/state")
def get_state() -> dict:
    return onboarding.get_state()


@router.post("/step")
def set_step(body: StepUpdate) -> dict:
    return onboarding.set_step(body.step)


@router.post("/api-key")
def submit_api_key(body: ApiKeySubmit) -> dict:
    return onboarding.submit_api_key(body.api_key)


@router.post("/api-key/skip")
def skip_api_key() -> dict:
    return onboarding.skip_api_key()


@router.delete("/api-key")
def remove_api_key() -> dict:
    return onboarding.remove_api_key()


@router.post("/voice")
def set_voice(body: BoolPreference) -> dict:
    return onboarding.set_voice_preference(body.enabled)


@router.post("/startup")
def set_startup(body: BoolPreference) -> dict:
    return onboarding.set_startup_preference(body.enabled)


@router.post("/complete")
def complete_onboarding() -> dict:
    return onboarding.complete()
