"""Update-check API — local-only. Never installs anything; at most reports
that a newer release exists and links to its GitHub release page."""

from fastapi import APIRouter

from app.core import update_check

router = APIRouter(prefix="/update", tags=["update"])


@router.get("/check")
def check() -> dict:
    return update_check.check_for_updates()
