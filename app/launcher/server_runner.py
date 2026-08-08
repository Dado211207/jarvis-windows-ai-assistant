"""Runs the existing FastAPI app (app.api.server:app — unchanged) on a
background thread with an externally-triggerable clean shutdown.

Reuses the exact uvicorn.Server + should_exit pattern already proven in
scripts/ci_windows_smoke.py's health-wait smoke check, so the packaged
launcher and CI's own Windows smoke test share one known-working
approach to "start real uvicorn, wait for real health, shut down
cleanly" instead of the launcher inventing a second one.
"""

import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import uvicorn

from app.config import settings
from app.launcher import boot_trace
from app.logging_config import get_logger

logger = get_logger("launcher.server_runner")

HEALTH_POLL_INTERVAL_SECONDS = 0.2
DEFAULT_HEALTH_TIMEOUT_SECONDS = 20.0


@dataclass
class RunningServer:
    server: uvicorn.Server
    thread: threading.Thread

    def request_shutdown(self, join_timeout: float = 5.0) -> None:
        """Ask uvicorn to stop and wait briefly for it to actually do so.
        Safe to call more than once — should_exit is idempotent and a
        second join() on an already-finished thread returns immediately."""
        self.server.should_exit = True
        self.thread.join(timeout=join_timeout)


def _run_server(server: uvicorn.Server) -> None:
    """Thread target — never lets server.run() fail silently. A daemon
    thread's uncaught exception is easy to lose entirely in a
    console=False packaged build (there is no console for Python's
    default threading.excepthook to write to); wait_until_healthy()
    would then just time out with no diagnostic trail anywhere. Logging
    it here means a real startup failure (a missing frozen-build import,
    a bind error, etc.) is at least visible in the log file, not just
    "never became healthy" with no further explanation."""
    boot_trace.trace("_run_server() thread: calling server.run()")
    try:
        server.run()
        boot_trace.trace("_run_server() thread: server.run() returned normally")
    except Exception as e:
        boot_trace.trace(f"_run_server() thread: server.run() raised {type(e).__name__}: {e}")
        logger.error("Background uvicorn server crashed.", exc_info=True)


def start_server_in_background(host: Optional[str] = None, port: Optional[int] = None) -> RunningServer:
    """*host*/*port* default to settings; tests pass explicit values for
    isolation on an ephemeral port, matching db/migrations.py::create_tables()'s
    own default-from-settings-but-overridable-for-tests pattern."""
    boot_trace.trace("start_server_in_background() importing app.api.server")
    from app.api.server import app as fastapi_app

    boot_trace.trace("start_server_in_background() import succeeded")
    host = host if host is not None else settings.jarvis_host
    port = port if port is not None else settings.jarvis_port

    config_kwargs = {}
    if sys.stdout is None or sys.stderr is None:
        # Root cause of a real hang found via a boot-trace CI investigation:
        # uvicorn.Config.__init__() calls configure_logging(), whose default
        # log_config wires "ext://sys.stdout"/"ext://sys.stderr" StreamHandlers
        # through logging.config.dictConfig() — both are None in a
        # --windowed/console=False PyInstaller build, and dictConfig() raises
        # ValueError trying to configure them (reproduced directly: calling
        # dictConfig(uvicorn.config.LOGGING_CONFIG) with sys.stdout/stderr set
        # to None raises "Unable to configure formatter 'default'"). That
        # exception is never caught here, so it propagates to PyInstaller's
        # own bootloader (jarvis.spec sets disable_windowed_traceback=False),
        # which shows a native traceback dialog box — with no human on a CI
        # runner (or any unattended machine) to dismiss it, this blocks
        # forever: the process stays alive, jarvis.log never receives a
        # single line (nothing got far enough to log it), and
        # wait_until_healthy() just times out with no explanation, which is
        # exactly what 30+ seconds of real CI runs showed before this was
        # tracked down. Passing log_config=None disables uvicorn's own
        # dictConfig() call entirely (log_level= below still works — it's
        # handled in a separate, independent branch of configure_logging());
        # app/logging_config.py's own "jarvis" logger already handles a None
        # sys.stdout safely, so this app's own logging is unaffected.
        config_kwargs["log_config"] = None

    config = uvicorn.Config(
        fastapi_app,
        host=host,
        port=port,
        reload=False,
        log_level=settings.jarvis_log_level.lower(),
        **config_kwargs,
    )
    boot_trace.trace("start_server_in_background() uvicorn.Config built")
    server = uvicorn.Server(config)
    boot_trace.trace("start_server_in_background() uvicorn.Server built, about to start thread")
    thread = threading.Thread(target=_run_server, args=(server,), daemon=True, name="jarvis-uvicorn")
    thread.start()
    boot_trace.trace("start_server_in_background() thread.start() returned, about to call logger.info")
    logger.info("Background uvicorn thread started on %s:%s", host, port)
    boot_trace.trace("start_server_in_background() logger.info() returned")
    return RunningServer(server=server, thread=thread)


def wait_until_healthy(
    host: Optional[str] = None,
    port: Optional[int] = None,
    timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS,
) -> bool:
    """Poll GET /health until it reports healthy or *timeout_seconds*
    elapses. Never raises — a request error just means "not ready yet"."""
    host = host if host is not None else settings.jarvis_host
    port = port if port is not None else settings.jarvis_port
    url = f"http://{host}:{port}/health"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200 and response.json().get("healthy") is True:
                return True
        except Exception:
            pass
        time.sleep(HEALTH_POLL_INTERVAL_SECONDS)
    return False
