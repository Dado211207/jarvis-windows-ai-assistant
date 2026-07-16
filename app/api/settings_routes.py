"""Settings API — Phase 8. Local-only persistent user settings.

Never returns or accepts secrets: values are validated and secret-scanned in
``settings_service`` before being persisted. The Anthropic API key and any
``.env`` credentials are never exposed here.
"""

from typing import Dict

from fastapi import APIRouter
from pydantic import BaseModel

from app.core import settings_service
from app.logging_config import get_logger

logger = get_logger("api.settings")

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsUpdate(BaseModel):
    # Free-form map of {key: value}; unknown/locked keys are rejected per-key.
    values: Dict[str, str]


class SettingsUpdateResult(BaseModel):
    success: bool
    updated: Dict[str, str]
    errors: Dict[str, str]
    settings: Dict[str, str]


@router.get("")
def get_settings() -> Dict[str, str]:
    """Return current settings (defaults merged with stored values)."""
    return settings_service.get_all()


@router.get("/defaults")
def get_defaults() -> Dict[str, str]:
    """Return the default value for every setting."""
    return dict(settings_service.DEFAULTS)


@router.patch("", response_model=SettingsUpdateResult)
def patch_settings(update: SettingsUpdate) -> SettingsUpdateResult:
    """Update one or more safe settings. Each key is validated independently.

    Invalid, unknown, locked, or secret-bearing values are reported in
    ``errors`` and never persisted; valid ones are applied.
    """
    updated: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    for key, value in update.values.items():
        ok, message = settings_service.update(key, value)
        if ok:
            updated[key] = value
        else:
            errors[key] = message
    return SettingsUpdateResult(
        success=not errors,
        updated=updated,
        errors=errors,
        settings=settings_service.get_all(),
    )
