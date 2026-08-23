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

    # A coding task recorded as RUNNING cannot be running: nothing
    # survived the restart. Reclassify it as interrupted so the page
    # offers inspect/archive/undo instead of a spinner over a task that
    # stopped existing. Deliberately never resumed — re-running a command
    # whose outcome nobody observed is how a half-finished install
    # becomes two.
    try:
        from app.coding import tasks as coding_tasks
        coding_tasks.mark_interrupted_on_startup()
    except Exception:  # noqa: BLE001 — optional data is not worth a failed start
        logger.warning("Could not reconcile interrupted coding tasks.", exc_info=True)

    yield

    from app.voice.tts import tts_service
    tts_service.stop()  # release any in-progress speech before the process exits

    # Every process Coding Workspace owns — commands and previews — ends
    # with the server. A dev server that outlives JARVIS holds its port
    # and its file handles, and the user has no way left to stop it.
    try:
        from app.coding import sessions as coding_sessions
        coding_sessions.stop_all("server shutting down")
    except Exception:  # noqa: BLE001 — shutdown must complete regardless
        logger.warning("Coding Workspace cleanup failed during shutdown.", exc_info=True)

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

    from app.api.local_ai_routes import router as local_ai_router
    app.include_router(local_ai_router)

    from app.api.chat import router as chat_router
    app.include_router(chat_router)

    from app.api.ws import router as ws_router
    app.include_router(ws_router)

    # Coding Workspace. Its capabilities live in their own registry
    # (app/coding/registry.py) and are never added to the global tool
    # registry, so mounting these routes gives the ordinary assistant
    # nothing it did not already have — see
    # docs/coding-workspace-architecture.md §2.2 and
    # tests/test_coding_isolation.py.
    from app.api.coding_routes import router as coding_router
    app.include_router(coding_router)

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


# Loopback addresses. "localhost" is included because it is what a person
# types; it resolves to a loopback address and nothing else.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})


def loopback_host(configured: str) -> str:
    """The address the server may actually bind to.

    JARVIS_HOST exists so somebody can write `localhost` instead of the
    numeric form, not so the API can be published to a network. Anything
    that is not a loopback address is refused here and replaced with
    127.0.0.1, loudly.

    **Why this is enforced rather than commented.** The line this replaces
    read `host=settings.jarvis_host,  # always 127.0.0.1 — never 0.0.0.0`
    — a comment describing a property nothing checked. `JARVIS_HOST` is
    an ordinary pydantic-settings field, so an environment variable or a
    `.env` file in the working directory silently widened the bind, and
    48 unauthenticated GET endpoints sit behind it: /logs carries command
    text, /memory carries saved memories, /conversation carries the chat,
    /diagnostics carries paths. Loopback is the only thing protecting
    them, which makes "loopback" a thing to verify rather than assert.

    The two existing invariant tests could not catch it: one asserts the
    *default* value, and the other greps the source for the literal
    "0.0.0.0", which an environment value never is.
    """
    host = (configured or "").strip()
    if host.lower() in LOOPBACK_HOSTS:
        return host
    logger.error(
        "Refusing to bind the JARVIS API to %r — it is not a loopback address. "
        "Binding to 127.0.0.1 instead. The API is local-only by design and has "
        "read endpoints that are not token-protected.",
        host,
    )
    return "127.0.0.1"


def run_api() -> None:
    """Start the FastAPI server. Called by run_jarvis.py when --api is passed."""
    import uvicorn

    setup_logging()
    # The resolved host, not the configured one: logging an address the
    # server is not going to bind to would be a message that lies.
    host = loopback_host(settings.jarvis_host)
    logger.info("Starting JARVIS API on http://%s:%s", host, settings.jarvis_port)
    uvicorn.run(
        "app.api.server:app",
        host=host,
        port=settings.jarvis_port,
        reload=False,
        log_level=settings.jarvis_log_level.lower(),
    )


if __name__ == "__main__":
    run_api()
