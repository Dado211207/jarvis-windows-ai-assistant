"""JARVIS FastAPI application — local-only, binds to 127.0.0.1."""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __phase__, __version__
from app.config import settings
from app.logging_config import get_logger, setup_logging

logger = get_logger("api.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("JARVIS API starting up — %s %s", __version__, __phase__)
    from app.core.brain import brain
    brain.initialise()
    yield
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

    # Local-only CORS: only allow requests from the same machine
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:*", "http://localhost:*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    from app.api.routes import router
    app.include_router(router)

    from app.api.actions import router as actions_router
    app.include_router(actions_router)

    from app.api.settings_routes import router as settings_router
    app.include_router(settings_router)

    from app.api.preferences_routes import router as preferences_router
    app.include_router(preferences_router)

    from app.api.onboarding_routes import router as onboarding_router
    app.include_router(onboarding_router)

    from app.api.diagnostics_routes import router as diagnostics_router
    app.include_router(diagnostics_router)

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
    from app.core import runtime_state
    runtime_state.set_actual_port(settings.jarvis_port)
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
