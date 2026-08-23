"""Browser dashboard route handlers — Phase 4.

All pages are static HTML shells; dynamic data is loaded client-side from the
existing JSON API so no user-supplied content is ever rendered server-side.
"""

import sys
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.logging_config import get_logger

logger = get_logger("ui.routes")

router = APIRouter(prefix="/ui", tags=["ui"])


def _templates_dir() -> Path:
    """Return the templates directory — handles both source and PyInstaller bundle."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "app" / "ui" / "templates"
    return Path(__file__).resolve().parent / "templates"


templates = Jinja2Templates(directory=str(_templates_dir()))


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard.html", {"page": "dashboard"})


@router.get("/chat", response_class=HTMLResponse, include_in_schema=False)
async def chat(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "chat.html", {"page": "chat"})


@router.get("/logs", response_class=HTMLResponse, include_in_schema=False)
async def logs(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "logs.html", {"page": "logs"})


@router.get("/memory", response_class=HTMLResponse, include_in_schema=False)
async def memory(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "memory.html", {"page": "memory"})


@router.get("/voice", response_class=HTMLResponse, include_in_schema=False)
async def voice(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "voice.html", {"page": "voice"})


@router.get("/actions", response_class=HTMLResponse, include_in_schema=False)
async def actions_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "actions.html", {"page": "actions"})


@router.get("/coding", response_class=HTMLResponse, include_in_schema=False)
async def coding_page(request: Request) -> HTMLResponse:
    """Coding Workspace. The page renders whether or not a project exists;
    with none, it shows an explicit empty state rather than a coding agent
    nobody asked for."""
    return templates.TemplateResponse(request, "coding.html", {"page": "coding"})


@router.get("/help", response_class=HTMLResponse, include_in_schema=False)
async def help_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "help.html", {"page": "help"})


@router.get("/setup", response_class=HTMLResponse, include_in_schema=False)
async def setup_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "setup.html", {"page": "setup"})


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "settings.html", {"page": "settings"})


@router.get("/diagnostics", response_class=HTMLResponse, include_in_schema=False)
async def diagnostics_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "diagnostics.html", {"page": "diagnostics"})
