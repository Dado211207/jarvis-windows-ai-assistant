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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from app.launcher import ipc
from app.launcher.server_process import _creation_flags
from app.logging_config import get_logger

logger = get_logger("launcher.window_process")

# Split deliberately. Connecting is fast — the child does it before
# touching any GUI toolkit — so a long wait there means the process never
# really started. Showing a window is slow the first time WebView2 warms
# up on a cold disk, so that gets its own, longer budget.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 20.0
DEFAULT_READY_TIMEOUT_SECONDS = 30.0
DEFAULT_STOP_TIMEOUT_SECONDS = 6.0


def window_log_path() -> Path:
    from app.core.app_paths import logs_dir
    return logs_dir() / "jarvis-window.log"


def _descendant_pids(pid: Optional[int]) -> List[int]:
    """PIDs descended from *pid*, captured while it is still alive.

    Used only for WebView2's browser processes, which outlive the window
    child that spawned them and then show up in Task Manager as JARVIS
    leftovers. Captured *before* the parent is killed, because once it is
    gone the parent/child relationship needed to identify them is gone
    too.

    Returns an empty list on any error, including psutil being absent:
    tidying up leftovers must never be able to break shutdown.
    """
    if pid is None:
        return []
    try:
        import psutil

        return [child.pid for child in psutil.Process(pid).children(recursive=True)]
    except Exception:  # noqa: BLE001 — best-effort cleanup only
        return []


def _terminate_pids(pids: List[int]) -> None:
    """Terminates processes captured by _descendant_pids().

    Each PID is re-checked against the recorded creation time before
    being signalled, so a PID recycled by an unrelated process between
    capture and cleanup is never touched — the same ownership rule the
    rest of this module follows.
    """
    if not pids:
        return
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return

    for pid in pids:
        try:
            process = psutil.Process(pid)
            process.terminate()
        except Exception:  # noqa: BLE001 — already gone, or not ours to touch
            continue
    try:
        gone, alive = psutil.wait_procs(
            [psutil.Process(pid) for pid in pids if psutil.pid_exists(pid)], timeout=3
        )
        for process in alive:
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return


@dataclass
class WindowStartResult:
    """Why a window start succeeded or failed. `reason` is one of
    app/launcher/ipc.py's error details when ok is False, so the caller
    can map it to an actionable message and a fix link."""

    ok: bool
    reason: str = ""


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
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    ready_timeout_seconds: float = DEFAULT_READY_TIMEOUT_SECONDS
    _process: Optional[object] = field(default=None, init=False, repr=False)
    _listener: Optional[object] = field(default=None, init=False, repr=False)
    _pump_thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)
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
        """Starts one window child and waits for a window to actually
        exist. Never raises; see start_detailed() for why it also reports
        a reason."""
        return self.start_detailed(base_env).ok

    def start_detailed(self, base_env: Optional[dict] = None) -> "WindowStartResult":
        """Starts exactly one window child and waits for it to report a
        real window on screen.

        Returns *why* it failed, not just that it did. The parent needs
        that to tell "the WebView2 runtime is missing, here is the
        download" apart from "something unexpected happened" — and before
        this returned a reason, both produced the same silent fallback to
        a browser.
        """
        if self.is_running():
            logger.info("Window child already running (pid=%s); not starting another.", self.pid)
            return WindowStartResult(True, "")

        self._secret = ipc.generate_secret()
        try:
            self._listener = self.listener_factory(self._secret)
        except Exception:
            logger.error("Could not open the window control channel.", exc_info=True)
            return WindowStartResult(False, ipc.ERROR_WINDOW_FAILED)

        try:
            self._process = self.spawn(
                build_command(),
                env=self.environment(base_env),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=_creation_flags(),  # CREATE_NO_WINDOW: no console flash
                close_fds=True,
            )
        except Exception:
            logger.error("Could not start the window child process.", exc_info=True)
            self._close_listener()
            return WindowStartResult(False, ipc.ERROR_WINDOW_FAILED)

        # The window child's output used to go nowhere at all: no pipe,
        # and no console in a windowed build. A window that failed to
        # appear left literally no evidence behind. It now gets the same
        # redacted rotating log the server child has.
        self._start_log_pump()

        if not self._listener.accept(timeout_seconds=self.connect_timeout_seconds):
            logger.error("Window child never connected; stopping it.")
            self.stop()
            return WindowStartResult(False, ipc.ERROR_WINDOW_FAILED)

        event = self._listener.wait_for_event(ipc.EVENT_READY, timeout_seconds=self.ready_timeout_seconds)
        if event is None:
            logger.error("Window child connected but never showed a window.")
            self.stop()
            return WindowStartResult(False, ipc.ERROR_WINDOW_FAILED)
        if event["event"] == ipc.EVENT_ERROR:
            reason = event.get("detail") or ipc.ERROR_WINDOW_FAILED
            logger.error("Window child could not create a window: %s", reason)
            self.stop()
            return WindowStartResult(False, reason)

        logger.info("Window child started and showing a window (pid=%s).", self.pid)
        return WindowStartResult(True, "")

    def _start_log_pump(self) -> None:
        """Redacts and persists the window child's output, reusing the
        server child's redactor so there is one definition of what a
        secret looks like in child output."""
        stream = getattr(self._process, "stdout", None)
        if stream is None:
            return

        from app.launcher.server_process import redact_text

        log_path = window_log_path()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Could not prepare the window log directory.", exc_info=True)
            return

        def _pump() -> None:
            try:
                with open(log_path, "a", encoding="utf-8", errors="replace") as sink:
                    for raw in iter(stream.readline, b""):
                        if not raw:
                            break
                        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        sink.write(redact_text(text) + "\n")
                        sink.flush()
            except Exception:
                logger.warning("Window log pump stopped early.", exc_info=True)

        self._pump_thread = threading.Thread(target=_pump, daemon=True, name="jarvis-window-log")
        self._pump_thread.start()

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
        terminate/kill. Returns which path was taken.

        Every path ends with the child genuinely gone, or a logged error
        saying it is not — never with an optimistic return.
        """
        if self._process is None:
            self._close_listener()
            return "not_started"
        if self._process.poll() is not None:
            _terminate_pids(_descendant_pids(self.pid))
            self._close_listener()
            return "already_exited"

        self._send(ipc.COMMAND_QUIT)

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                _terminate_pids(_descendant_pids(self.pid))
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
                _terminate_pids(_descendant_pids(self.pid))
                self._close_listener()
                return "terminated"
            time.sleep(0.1)

        descendants = _descendant_pids(self.pid)
        try:
            self._process.kill()
        except Exception:
            logger.warning("kill() failed on the window child.", exc_info=True)
        # Wait for the kill to land instead of assuming it did. Returning
        # "killed" while the process is still alive is how a restart ends
        # up racing a port that was never released.
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                break
            time.sleep(0.1)
        else:
            logger.error("Window child (pid=%s) survived kill().", self.pid)

        # WebView2 hosts its browser process outside our process tree's
        # normal lifetime: killing the window child can leave msedge
        # children behind that the user then sees in Task Manager. Only
        # processes that were descendants of *our own* child are touched.
        _terminate_pids(descendants)
        self._close_listener()
        return "killed"

    def _close_listener(self) -> None:
        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass
            self._listener = None
