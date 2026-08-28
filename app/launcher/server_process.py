"""Runs the FastAPI/uvicorn runtime as a real child process.

Replaces app/launcher/server_runner.py's in-process background thread for
the packaged desktop app. server_runner.py is deliberately kept — it is
still the right tool for tests and for scripts/ci_windows_smoke.py, which
want a server inside the current process — but the packaged launcher no
longer uses it.

Why a child process at all, since an in-process thread is simpler:
pywebview must own the process's main thread (webview/platforms/
winforms.py calls signal.signal(), main-thread-only in CPython, then runs
WinForms' Application.Run() on its own .NET STA thread). That forced the
tray's hidden window onto a background thread, and with no window left on
the main thread a graceful `taskkill` — the same mechanism Windows' "End
task" and Inno Setup's CloseApplications=yes use — stopped being reliably
delivered: three consecutive windows-latest runs on identical application
code produced one pass and two failures, with the boot trace proving no
close message reached any handler. See docs/desktop-shell-findings.md for
the full evidence.

Splitting the runtime out means the parent process can go back to owning
the tray's message loop on its main thread (the configuration that was
reliably green for many runs), while the fragile native GUI loop and the
server each live where they work best. It also means a server that hangs
can be terminated without taking the user's window with it, and vice
versa.

Everything platform-specific is isolated behind small, injectable seams
(*spawn*, and _creation_flags()) so the real logic stays testable on this
repo's Linux CI, matching how app/voice/stt.py and app/launcher/tray.py
already separate "logic we can test" from "native calls we cannot".
"""

import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import httpx

from app.logging_config import get_logger

logger = get_logger("launcher.server_process")

HEALTH_POLL_INTERVAL_SECONDS = 0.2
DEFAULT_HEALTH_TIMEOUT_SECONDS = 30.0
DEFAULT_STOP_TIMEOUT_SECONDS = 8.0
DEFAULT_PORT_RELEASE_TIMEOUT_SECONDS = 10.0
PORT_RELEASE_POLL_INTERVAL_SECONDS = 0.1
SESSION_SECRET_ENV = "JARVIS_SESSION_SECRET"
CHILD_LOG_MAX_BYTES = 2 * 1024 * 1024
CHILD_LOG_BACKUP_COUNT = 3

# Redaction applied to every line the child writes before it reaches a log
# file. Deliberately independent of app/core/redaction.py's redact_params(),
# which redacts structured tool kwargs by key name — this operates on raw
# unstructured text, where there are no keys to match on, so it matches the
# *shapes* that must never be persisted instead.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9\-_]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*\S+"),
)
_REDACTED = "[REDACTED]"


def redact_text(line: str, extra_secrets: Optional[List[str]] = None) -> str:
    """Redacts secret-looking text from one child output line.

    *extra_secrets* carries values known to be secret for this run (the
    per-session secret below), which have no recognisable shape of their
    own and so could never be caught by a pattern — the only reliable way
    to keep a generated secret out of the logs is to match its literal
    value."""
    for secret in extra_secrets or []:
        if secret:
            line = line.replace(secret, _REDACTED)
    for pattern in _SECRET_PATTERNS:
        line = pattern.sub(_REDACTED, line)
    return line


def generate_session_secret() -> str:
    return secrets.token_urlsafe(32)


def child_log_path() -> Path:
    from app.core.app_paths import logs_dir
    return logs_dir() / "jarvis-server.log"


def _rotate_if_needed(path: Path) -> None:
    """Size-based rotation done up front, at start, rather than mid-stream:
    the child writes continuously for the life of the process, so rotating
    while a handle is open would either lose lines or need the file
    reopened underneath the writer. Rolling once per server start keeps
    each file bounded without that complexity."""
    try:
        if not path.exists() or path.stat().st_size < CHILD_LOG_MAX_BYTES:
            return
        for index in range(CHILD_LOG_BACKUP_COUNT - 1, 0, -1):
            older, newer = path.with_suffix(f".log.{index}"), path.with_suffix(f".log.{index + 1}")
            if older.exists():
                older.replace(newer)
        path.replace(path.with_suffix(".log.1"))
    except OSError:
        logger.warning("Could not rotate the server log; continuing.", exc_info=True)


def _creation_flags() -> int:
    """CREATE_NO_WINDOW so the child never flashes a console window in the
    packaged build. Zero off Windows, where the flag does not exist —
    isolated here so callers never branch on the platform themselves."""
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def build_command(executable: Optional[str] = None, frozen: Optional[bool] = None) -> List[str]:
    """The command that starts the API runtime.

    Frozen (packaged): re-invokes the packaged executable itself with
    --api, so the child never needs a system Python installation — the
    exact reason run_jarvis.py keeps its --api mode. Dev: runs the same
    entry point through the current interpreter.
    """
    executable = executable if executable is not None else sys.executable
    frozen = frozen if frozen is not None else bool(getattr(sys, "frozen", False))
    if frozen:
        return [executable, "--api"]
    repo_root = Path(__file__).resolve().parent.parent.parent
    return [executable, str(repo_root / "run_jarvis.py"), "--api"]


@dataclass
class ServerProcess:
    """Owns exactly one child server process.

    Never touches a process it did not itself start: every stop/terminate
    path acts only on the Popen object this instance created, so an
    unrelated process that happens to reuse the PID after our child exits
    can never be signalled.
    """

    host: str
    port: int
    session_secret: str = field(default_factory=generate_session_secret)
    spawn: Callable = subprocess.Popen
    _process: Optional[object] = field(default=None, init=False, repr=False)
    _pump_thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)

    # --- lifecycle ---

    def environment(self, base_env: Optional[dict] = None) -> dict:
        """The child's environment: the parent's, plus the loopback bind
        and the per-session secret. The secret is passed by inherited
        environment rather than a command-line argument on purpose — a
        command line is world-readable on Windows (any process can read
        another's via WMI/NtQueryInformationProcess), while an environment
        block is not exposed the same way."""
        env = dict(base_env if base_env is not None else os.environ)
        env["JARVIS_HOST"] = self.host  # always loopback; see host validation in start()
        env["JARVIS_PORT"] = str(self.port)
        env[SESSION_SECRET_ENV] = self.session_secret
        return env

    def start(self, base_env: Optional[dict] = None) -> None:
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                f"Refusing to start the server on non-loopback host {self.host!r}. "
                "JARVIS's API is loopback-only by design."
            )
        log_path = child_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(log_path)

        command = build_command()
        logger.info("Starting server child process on %s:%s", self.host, self.port)
        self._process = self.spawn(
            command,
            env=self.environment(base_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # one stream keeps ordering intact
            creationflags=_creation_flags(),
            close_fds=True,
        )
        self._start_log_pump(log_path)

    def _start_log_pump(self, log_path: Path) -> None:
        """Pipes the child's output through redaction into a JARVIS-owned
        log file. A pipe plus a pump thread, rather than handing the child
        a file handle directly: only by reading each line in the parent can
        it be redacted before it is ever written to disk."""
        stream = getattr(self._process, "stdout", None)
        if stream is None:
            return

        secret = self.session_secret

        def _pump() -> None:
            try:
                with open(log_path, "a", encoding="utf-8", errors="replace") as sink:
                    for raw in iter(stream.readline, b""):
                        if not raw:
                            break
                        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        sink.write(redact_text(text, [secret]) + "\n")
                        sink.flush()
            except Exception:
                logger.warning("Server log pump stopped early.", exc_info=True)

        self._pump_thread = threading.Thread(target=_pump, daemon=True, name="jarvis-server-log")
        self._pump_thread.start()

    # --- state ---

    def is_running(self) -> bool:
        if self._process is None:
            return False
        return self._process.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return getattr(self._process, "pid", None) if self._process is not None else None

    @property
    def returncode(self) -> Optional[int]:
        return self._process.poll() if self._process is not None else None

    def health_url(self) -> str:
        return f"http://{self.host}:{self.port}/health"

    def wait_until_healthy(self, timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS) -> bool:
        """Polls the real /health endpoint. Returns False immediately if
        the child exits first — distinguishing "crashed on startup" from
        "still starting" is exactly what makes a startup failure
        diagnosable instead of just a timeout."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self.is_running():
                logger.error("Server child process exited (code %s) before becoming healthy.", self.returncode)
                return False
            try:
                response = httpx.get(self.health_url(), timeout=1.0)
                if response.status_code == 200 and response.json().get("healthy") is True:
                    return True
            except Exception:
                pass
            time.sleep(HEALTH_POLL_INTERVAL_SECONDS)
        logger.error("Server child process did not become healthy within %.0fs.", timeout_seconds)
        return False

    # --- shutdown ---

    def stop(self, timeout_seconds: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> str:
        """Asks the child to stop, waits a bounded time, then escalates.

        Returns one of "not_started", "already_exited", "graceful", or
        "killed" so callers (and tests) can tell which path was taken
        rather than inferring it.

        Honesty note on what "graceful" means per platform: terminate() is
        SIGTERM on POSIX, which uvicorn handles by running its normal
        lifespan shutdown; on Windows it is TerminateProcess, which is
        abrupt. That is acceptable here for the same reason the launcher's
        force-exit watchdog is: every SQLite write is already committed
        synchronously, and the approval queue and privacy mode are
        documented per-process/reset-on-restart, so there is no buffered
        state to lose. Claiming a clean Windows unwind we do not actually
        perform would be worse than saying this plainly.
        """
        from app.launcher.process_tree import capture_descendants, terminate_identities

        if self._process is None:
            return "not_started"
        if self._process.poll() is not None:
            terminate_identities(capture_descendants(self.pid))
            return "already_exited"

        logger.info("Stopping server child process (pid=%s).", self.pid)
        # Captured while the child is still alive: once it is gone, the
        # relationship that identifies anything it spawned is gone too.
        # Identities rather than bare PIDs, for the same reason the window
        # child captures them — a PID held across a grace period can come
        # to mean a different process entirely.
        descendants = capture_descendants(self.pid)
        try:
            self._process.terminate()
        except Exception:
            logger.warning("terminate() failed on the server child.", exc_info=True)

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                logger.info("Server child process stopped.")
                terminate_identities(descendants)
                return "graceful"
            time.sleep(0.1)

        logger.warning(
            "Server child did not exit within %.0fs — killing it. Only this "
            "launcher's own child is signalled; no other process is touched.",
            timeout_seconds,
        )
        try:
            self._process.kill()
        except Exception:
            logger.warning("kill() failed on the server child.", exc_info=True)

        # Wait for the kill to take effect rather than assuming it did.
        # Returning while the process still holds the listening socket is
        # how a restart ends up trying to bind a port that is not free
        # yet, which then looks like "the new server never came up".
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                terminate_identities(descendants)
                return "killed"
            time.sleep(0.1)
        logger.error("Server child (pid=%s) survived kill().", self.pid)
        terminate_identities(descendants)
        return "killed"

    def port_is_free(self) -> bool:
        """Whether the next server could actually take this port.

        A bind attempt, plus a connect attempt as a second opinion. The
        subtlety is entirely in SO_REUSEADDR, which means *opposite
        things* on the two platforms and cost this check two CI rounds to
        get right:

        * **On Windows**, SO_REUSEADDR lets a socket bind a port another
          socket is actively listening on. A probe that sets it therefore
          reports every busy port as free — which is exactly what it did,
          silently, until its own test caught it. So the probe binds
          *without* it, and a bind failure is a truthful "still in use".
        * **On POSIX**, SO_REUSEADDR does not permit stealing a live
          listener; it only permits rebinding through TIME_WAIT. uvicorn
          sets it, so a probe without it would be stricter than the real
          server and report a port busy that uvicorn would have taken.

        Each branch is the right question for its own platform. The
        connect check adds nothing on a machine where bind is already
        correct, and costs one loopback attempt — worth keeping, because
        "something answered on this port" is unambiguous evidence on any
        platform whatever the binding rules happen to be.
        """
        import socket

        # create_connection(), not connect_ex() on a socket with a
        # timeout: setting a timeout puts the socket in non-blocking mode,
        # so connect_ex() returns WSAEWOULDBLOCK immediately on Windows
        # rather than 0, even when the connection goes on to succeed.
        try:
            with socket.create_connection((self.host, self.port), timeout=0.25):
                return False  # something answered; the port is plainly in use
        except OSError:
            pass  # nothing answered, which is what we want

        binder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform != "win32":
            binder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            binder.bind((self.host, self.port))
            return True
        except OSError:
            return False
        finally:
            binder.close()

    def wait_until_port_released(self, timeout_seconds: float = DEFAULT_PORT_RELEASE_TIMEOUT_SECONDS) -> bool:
        """Waits until this server's port is genuinely available again.

        A restart rebinds the port the previous child just released, and
        process exit and socket teardown are not the same instant. Before
        this existed, restart() started the replacement server straight
        after stop() returned; when the socket had not finished tearing
        down, the new server died on bind and the user was told the
        runtime "did not come back up" — with no mention of the port,
        which was the entire problem.
        """
        deadline = time.monotonic() + timeout_seconds
        while True:
            if self.port_is_free():
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(PORT_RELEASE_POLL_INTERVAL_SECONDS)
        logger.error(
            "Port %s was still in use %.0fs after stopping the server child.",
            self.port, timeout_seconds,
        )
        return False
