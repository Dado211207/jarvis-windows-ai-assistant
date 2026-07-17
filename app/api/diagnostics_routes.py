"""Diagnostics API — local-only support information for the Diagnostics page.

Every field returned here is deliberately safe to display and safe to copy
into a bug report; see app.core.diagnostics for what's included/excluded.
"""

from fastapi import APIRouter

from app.core import diagnostics

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


@router.get("")
def get_diagnostics() -> dict:
    return diagnostics.get_report()


@router.get("/report-text")
def get_diagnostics_report_text() -> dict:
    """The redacted, copy/paste-safe rendering — see
    app.core.diagnostics.get_report_text(). This is what the Diagnostics
    page's "Copy report" button actually copies; GET /diagnostics above
    (unredacted, real paths) is only ever used for the page's own on-screen
    display."""
    return {"text": diagnostics.get_report_text()}


@router.post("/open-logs-folder")
def open_logs_folder() -> dict:
    return diagnostics.open_logs_folder()
