"""Browser dashboard route handlers — Phase 4.

All pages are static HTML shells; dynamic data is loaded client-side from the
existing JSON API so no user-supplied content is ever rendered server-side.
"""

import sys
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core import onboarding
from app.logging_config import get_logger

logger = get_logger("ui.routes")

router = APIRouter(prefix="/ui", tags=["ui"])


def _onboarding_redirect():
    """RedirectResponse to the wizard when a frozen build hasn't finished
    first-run setup yet; None otherwise (always None in dev/test)."""
    if onboarding.is_required():
        return RedirectResponse(url="/ui/onboarding")
    return None


def _templates_dir() -> Path:
    """Return the templates directory — handles both source and PyInstaller bundle."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "app" / "ui" / "templates"
    return Path(__file__).resolve().parent / "templates"


templates = Jinja2Templates(directory=str(_templates_dir()))


@router.get("/onboarding", response_class=HTMLResponse, include_in_schema=False)
async def onboarding_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "onboarding.html", {"page": "onboarding"})


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    return _onboarding_redirect() or templates.TemplateResponse(request, "dashboard.html", {"page": "dashboard"})


@router.get("/chat", response_class=HTMLResponse, include_in_schema=False)
async def chat(request: Request):
    return _onboarding_redirect() or templates.TemplateResponse(request, "chat.html", {"page": "chat"})


@router.get("/logs", response_class=HTMLResponse, include_in_schema=False)
async def logs(request: Request):
    return _onboarding_redirect() or templates.TemplateResponse(request, "logs.html", {"page": "logs"})


@router.get("/memory", response_class=HTMLResponse, include_in_schema=False)
async def memory(request: Request):
    return _onboarding_redirect() or templates.TemplateResponse(request, "memory.html", {"page": "memory"})


@router.get("/voice", response_class=HTMLResponse, include_in_schema=False)
async def voice(request: Request):
    return _onboarding_redirect() or templates.TemplateResponse(request, "voice.html", {"page": "voice"})


@router.get("/actions", response_class=HTMLResponse, include_in_schema=False)
async def actions_page(request: Request):
    return _onboarding_redirect() or templates.TemplateResponse(request, "actions.html", {"page": "actions"})


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(request: Request):
    return _onboarding_redirect() or templates.TemplateResponse(request, "settings.html", {"page": "settings"})


@router.get("/help", response_class=HTMLResponse, include_in_schema=False)
async def help_page(request: Request):
    return _onboarding_redirect() or templates.TemplateResponse(request, "help.html", {"page": "help"})


@router.get("/diagnostics", response_class=HTMLResponse, include_in_schema=False)
async def diagnostics_page(request: Request):
    return _onboarding_redirect() or templates.TemplateResponse(request, "diagnostics.html", {"page": "diagnostics"})
