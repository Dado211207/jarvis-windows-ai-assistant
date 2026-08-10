"""JARVIS FastAPI application — local-only, binds to 127.0.0.1."""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __phase__, __version__
from app.api.origin import allowed_origins
from app.api.session import SessionCookieMiddleware
from app.config import settings
from app.logging_config import get_logger, setup_logging

logger = get_logger("api.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("JARVIS API starting up — %s %s", __version__, __phase__)
    from app.core.brain import brain
    brain.initialise()

    from app.core.runtime_state import RuntimeState, runtime
    # Handles both a fresh process (state is BOOTING) and a process whose
    # lifespan has already run once before, e.g. repeated TestClient
    # startup/shutdown cycles within one test run (state is OFFLINE from
    # the previous shutdown below) — OFFLINE can only re-enter via BOOTING.
    if runtime.state == RuntimeState.OFFLINE:
        runtime.transition(RuntimeState.BOOTING, reason="restarting")
    if runtime.state == RuntimeState.BOOTING:
        runtime.transition(RuntimeState.STANDBY, reason="startup complete")

    yield

    from app.voice.tts import tts_service
    tts_service.stop()  # release any in-progress speech before the process exits

    runtime.try_transition(RuntimeState.OFFLINE, reason="shutdown")
    logger.info("JARVIS API shutting down.")


def create_app() -> FastAPI:
    app = FastAPI(
        title="JARVIS Local API",
        version=__version__,
        description="Personal Windows AI Assistant — local-only API (Phase 1)",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # Local-only CORS: only allow requests from this dashboard's own real
    # origins. (v0.2 fix: CORSMiddleware matches allow_origins by exact
    # string equality, not glob — the previous "http://127.0.0.1:*" style
    # entries never matched a real browser Origin header at all.)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    # v0.2: every response gets (or keeps) a valid CSRF/mutation session
    # cookie — see app/api/session.py. Mutating routes separately require
    # it via Depends(require_session_token); this middleware's only job is
    # making sure the browser always has a current one to read and echo.
    app.add_middleware(SessionCookieMiddleware)

    from app.api.routes import router
    app.include_router(router)

    from app.api.actions import router as actions_router
    app.include_router(actions_router)

    from app.api.voice_routes import router as voice_router
    app.include_router(voice_router)

    from app.api.chat import router as chat_router
    app.include_router(chat_router)

    from app.api.ws import router as ws_router
    app.include_router(ws_router)

    from app.ui.routes import router as ui_router
    app.include_router(ui_router)

    static_dir = _ui_static_dir()
    if static_dir.exists():
        app.mount("/ui/static", StaticFiles(directory=str(static_dir)), name="ui_static")

    return app


def _ui_static_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "app" / "ui" / "static"
    return Path(__file__).resolve().parent.parent / "ui" / "static"


app = create_app()


def run_api() -> None:
    """Start the FastAPI server. Called by run_jarvis.py when --api is passed."""
    import uvicorn

    setup_logging()
    logger.info(
        "Starting JARVIS API on http://%s:%s",
        settings.jarvis_host,
        settings.jarvis_port,
    )
    uvicorn.run(
        "app.api.server:app",
        host=settings.jarvis_host,  # always 127.0.0.1 — never 0.0.0.0
        port=settings.jarvis_port,
        reload=False,
        log_level=settings.jarvis_log_level.lower(),
    )


if __name__ == "__main__":
    run_api()
