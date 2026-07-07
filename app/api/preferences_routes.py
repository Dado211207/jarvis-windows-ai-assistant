"""Preferences (personality memory) API — Phase 8.

Explicit, local-only. New entries are secret-scanned in ``preferences`` before
being stored. There is intentionally no "clear all" endpoint here: wiping all
personality memory is destructive and must go through the approval-gated
``clear_preferences`` tool (POST /command → Actions confirm), never a plain API
call.
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from app.core import preferences as prefs
from app.core.models import Preference
from app.logging_config import get_logger

logger = get_logger("api.preferences")

router = APIRouter(prefix="/preferences", tags=["preferences"])


class PreferenceCreate(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _validate_text(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("text must not be empty")
        if len(v) > prefs.MAX_PREF_LEN:
            raise ValueError(f"text too long (max {prefs.MAX_PREF_LEN} chars)")
        return v.strip()


class PreferenceResult(BaseModel):
    success: bool
    message: str
    data: Optional[dict] = None


@router.get("", response_model=List[Preference])
def list_preferences(
    category: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> List[Preference]:
    from db.database import get_db

    return get_db().get_preferences(category=category, limit=limit)


@router.get("/search", response_model=List[Preference])
def search_preferences(q: str = Query(..., min_length=1)) -> List[Preference]:
    from db.database import get_db

    return get_db().search_preferences(q)


@router.post("", response_model=PreferenceResult)
def create_preference(payload: PreferenceCreate) -> PreferenceResult:
    """Save one explicit preference. Rejected if it contains a secret."""
    result = prefs.remember_preference(payload.text)
    return PreferenceResult(
        success=result["success"],
        message=result["message"],
        data=result.get("data"),
    )


@router.delete("/{pref_id}", response_model=PreferenceResult)
def delete_preference(pref_id: int) -> PreferenceResult:
    """Delete a single preference by id."""
    from db.database import get_db

    db = get_db()
    existing = db.get_preference(pref_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Preference {pref_id} not found.")
    db.delete_preference(pref_id)
    logger.info("Preference deleted via API (id=%s)", pref_id)
    return PreferenceResult(
        success=True,
        message="Preference forgotten.",
        data={"id": pref_id},
    )
