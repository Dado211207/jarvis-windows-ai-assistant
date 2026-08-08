"""Parent-side supervisor for the window child process.

Owns at most one window child at a time and the authenticated control
channel to it (app/launcher/ipc.py). Mirrors
app/launcher/server_process.py's ownership rules deliberately: only the
Popen object this instance created is ever signalled, so a PID reused by
an unrelated process after our child exits can never be terminated.

The window is intentionally disposable. It holds no application state —
the server child holds all of it — so "the window crashed" is a
recoverable condition the parent handles by starting a fresh one, not an
application failure.
"""

import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from app.launcher import ipc
from app.launcher.server_process import _creation_flags
from app.logging_config import get_logger

logger = get_logger("launcher.window_process")

DEFAULT_READY_TIMEOUT_SECONDS = 30.0
DEFAULT_STOP_TIMEOUT_SECONDS = 6.0


def build_command(executable: Optional[str] = None, frozen: Optional[bool] = None) -> List[str]:
    """Frozen builds re-invoke the packaged executable with --window, so
    the child never needs a system Python — the same rule the server
    child follows."""
    executable = executable if executable is not None else sys.executable
    frozen = frozen if frozen is not None else bool(getattr(sys, "frozen", False))
    if frozen:
        return [executable, "--window"]
    from pathlib import Path
    repo_root = Path(__file__).resolve().parent.parent.parent
    return [executable, str(repo_root / "run_jarvis.py"), "--window"]


@dataclass
class WindowProcess:
    url: str
    close_action: str = "tray"
    spawn: Callable = subprocess.Popen
    listener_factory: Callable = ipc.ControlListener
    _process: Optional[object] = field(default=None, init=False, repr=False)
    _listener: Optional[object] = field(default=None, init=False, repr=False)
    # Never logged and never placed on argv — see environment().
    _secret: bytes = field(default=b"", init=False, repr=False)

    # --- lifecycle ---

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return getattr(self._process, "pid", None) if self._process is not None else None

    def environment(self, base_env: Optional[dict] = None) -> dict:
        """The IPC address and secret travel by inherited environment, not
        argv — a Windows command line is readable by other processes."""
        if self._listener is None:
            raise RuntimeError("environment() requires a listener; call start() instead.")
        env = dict(base_env if base_env is not None else os.environ)
        env[ipc.IPC_ADDRESS_ENV] = self._listener.address_string()
        env[ipc.IPC_SECRET_ENV] = self._secret.decode("utf-8", errors="ignore") if isinstance(self._secret, bytes) else str(self._secret)
        env[ipc.IPC_URL_ENV] = self.url
        env[ipc.IPC_CLOSE_ACTION_ENV] = self.close_action
        return env

    def start(self, base_env: Optional[dict] = None) -> bool:
        """Starts exactly one window child and waits for it to
        authenticate. Returns False (never raises) if it could not be
        started or never connected — the parent treats a missing window as
        a degraded state, not a fatal one, since the tray and server are
        unaffected."""
        if self.is_running():
            logger.info("Window child already running (pid=%s); not starting another.", self.pid)
            return True

        self._secret = ipc.generate_secret()
        try:
            self._listener = self.listener_factory(self._secret)
        except Exception:
            logger.error("Could not open the window control channel.", exc_info=True)
            return False

        try:
            self._process = self.spawn(
                build_command(),
                env=self.environment(base_env),
                creationflags=_creation_flags(),  # CREATE_NO_WINDOW: no console flash
                close_fds=True,
            )
        except Exception:
            logger.error("Could not start the window child process.", exc_info=True)
            self._close_listener()
            return False

        if not self._listener.accept():
            logger.error("Window child never authenticated; stopping it.")
            self.stop()
            return False

        logger.info("Window child started (pid=%s).", self.pid)
        return True

    # --- commands ---

    def _send(self, command: str) -> bool:
        if self._listener is None or not self.is_running():
            return False
        return self._listener.send_command(command)

    def show(self) -> bool:
        return self._send(ipc.COMMAND_SHOW)

    def hide(self) -> bool:
        return self._send(ipc.COMMAND_HIDE)

    def focus(self) -> bool:
        return self._send(ipc.COMMAND_FOCUS)

    def reload(self) -> bool:
        return self._send(ipc.COMMAND_RELOAD)

    def poll_event(self, timeout: float = 0.0):
        return self._listener.poll_event(timeout) if self._listener is not None else None

    def show_or_restart(self, base_env: Optional[dict] = None) -> bool:
        """What the tray's "Open JARVIS" needs: focus the existing window,
        or transparently start a fresh one if it died. Never creates a
        second window child — is_running() gates that inside start()."""
        if self.is_running():
            return self.show()
        logger.info("No live window child — starting a fresh one.")
        return self.start(base_env)

    # --- shutdown ---

    def stop(self, timeout_seconds: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> str:
        """Asks the window to close over IPC first, then escalates to
        terminate/kill. Returns which path was taken."""
        if self._process is None:
            self._close_listener()
            return "not_started"
        if self._process.poll() is not None:
            self._close_listener()
            return "already_exited"

        self._send(ipc.COMMAND_QUIT)

        import time
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self._close_listener()
                return "graceful"
            time.sleep(0.1)

        logger.warning("Window child did not exit on request — terminating this launcher's own child only.")
        try:
            self._process.terminate()
        except Exception:
            logger.warning("terminate() failed on the window child.", exc_info=True)

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self._close_listener()
                return "terminated"
            time.sleep(0.1)

        try:
            self._process.kill()
        except Exception:
            logger.warning("kill() failed on the window child.", exc_info=True)
        self._close_listener()
        return "killed"

    def _close_listener(self) -> None:
        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass
            self._listener = None
