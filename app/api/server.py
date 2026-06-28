"""JARVIS FastAPI application — local-only, binds to 127.0.0.1."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

    return app


app = create_app()


if __name__ == "__main__":
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
