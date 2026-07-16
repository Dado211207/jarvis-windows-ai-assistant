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
    from app.core import session_token
    token = session_token.generate_token()  # fresh every launch; never persisted, never logged
    # Deliberate trusted path for --api / dev use: the token is only ever
    # printed to an interactive console the caller explicitly opened
    # themselves (never to the rotating log file — see session_token.py).
    # The production launcher never sets this flag; its only consumer is
    # the browser tab it opens itself, which gets the token server-rendered
    # into the page (see app/ui/routes.py) instead.
    if getattr(app.state, "print_token_on_startup", False) and sys.stdout is not None:
        print(f"JARVIS local API session token (this run only): {token}")
        print("Pass it as the X-Jarvis-Token header on state-changing requests (POST/PUT/PATCH/DELETE).")
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

    # Local-only CORS: only allow requests from this machine, on any port.
    # (allow_origins does exact/literal matching, not glob — a hardcoded
    # "http://127.0.0.1:*" would never match a real Origin header at all;
    # allow_origin_regex is the correct mechanism for a variable port.)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    # Host/Origin allowlist + per-launch session token for state-changing
    # requests — see app/api/local_guard.py and docs/SECURITY.md.
    from app.api.local_guard import LocalOnlyGuardMiddleware
    app.add_middleware(LocalOnlyGuardMiddleware)

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

    from app.api.update_routes import router as update_router
    app.include_router(update_router)

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
    # Deliberate trusted path: --api is always started explicitly from a
    # console the caller already controls, so printing the session token
    # there (never to the log file) is safe — see the lifespan handler above.
    app.state.print_token_on_startup = True
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
