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
from typing import Callable, Dict, List, Optional

from app.launcher import ipc
from app.launcher.process_tree import (
    CleanupReport,
    ProcessIdentity,
    capture_descendants,
    terminate_identities,
)
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
# A ping is answered by a poll loop that wakes every 0.2s, so this only
# needs to cover a few of those plus scheduling jitter.
PING_TIMEOUT_SECONDS = 3.0


def window_log_path() -> Path:
    from app.core.app_paths import logs_dir
    return logs_dir() / "jarvis-window.log"


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
    # What the last stop() did to the window child's leftovers. Kept so a
    # caller — and the lifecycle test — can ask what happened instead of
    # scraping a log line.
    _last_cleanup: Optional[CleanupReport] = field(default=None, init=False, repr=False)

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
        # decode("ascii"), strictly, and never errors="ignore": the
        # previous version silently discarded every byte that was not
        # valid UTF-8, handing the child a secret the parent had never
        # heard of. ipc.generate_secret() produces ASCII precisely so
        # this round trip is exact — if that ever changes, this raises
        # here instead of failing an HMAC challenge three modules away.
        env[ipc.IPC_SECRET_ENV] = (
            self._secret.decode("ascii") if isinstance(self._secret, bytes) else str(self._secret)
        )
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

    def responds_to_commands(self, timeout_seconds: float = PING_TIMEOUT_SECONDS) -> bool:
        """Whether the window child is still servicing its control channel.

        A real round trip, not a process check: is_running() only says a
        process exists, and a window child whose command pump has died is
        a window the tray's Show, Restart and Quit can no longer reach.
        Readiness has to mean the second thing.
        """
        if self._listener is None or not self.is_running():
            return False
        if not self._send(ipc.COMMAND_PING):
            return False
        event = self._listener.wait_for_event(ipc.EVENT_PONG, timeout_seconds=timeout_seconds)
        return event is not None and event.get("event") == ipc.EVENT_PONG

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

        **Descendants are captured before anything is asked to stop**, and
        that ordering is the whole point rather than a detail. WebView2
        runs its browser and GPU processes as children of the window
        child; once that child exits, the parent/child relationship that
        identifies them is gone, so a capture taken afterwards walks a
        dead PID, returns nothing, and cleans up nothing.

        This method used to capture on each exit path *after* poll() had
        already confirmed the child was gone — which meant the graceful
        path, the one taken every ordinary time somebody chooses Quit,
        reliably left an msedgewebview2.exe behind. It was found by the
        ten-cycle lifecycle test in scripts/test_clean_install.py, on
        cycle one, and by nothing before it: every earlier check asked
        only whether JARVIS.exe itself had exited.

        **Identities, not PIDs**, and captured *before* each poll rather
        than after. Both come from the second time this defect appeared:
        a WebView2 process outlived cycle 2 of the lifecycle test while
        cycle 1 and a whole sibling run passed. See
        app/launcher/process_tree.py for the three causes that explains.
        """
        if self._process is None:
            self._close_listener()
            return "not_started"

        leftovers: Dict[int, ProcessIdentity] = {}

        def _capture() -> None:
            # setdefault, never overwrite: the first sighting is the true
            # identity of that PID. A later sighting of the same number
            # could be a different process that inherited it, and
            # recording the impostor is how cleanup ends up aimed at a
            # stranger.
            for identity in capture_descendants(self.pid):
                leftovers.setdefault(identity.pid, identity)

        def _finish(outcome: str) -> str:
            # process_tree is written never to raise, and this catch is
            # here for the day that stops being true. Quit must not be
            # able to fail because tidying up leftovers failed: the
            # window is already gone, and a launcher that cannot finish
            # closing is a worse outcome than an orphaned helper process.
            try:
                self._last_cleanup = terminate_identities(list(leftovers.values()))
            except BaseException:  # noqa: BLE001 — shutdown must always complete
                logger.error("Leftover process cleanup failed; continuing shutdown.", exc_info=True)
            self._close_listener()
            return outcome

        _capture()

        if self._process.poll() is not None:
            return _finish("already_exited")

        self._send(ipc.COMMAND_QUIT)

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            # Capture first, check second. WebView2 starts its helper
            # processes lazily, so the last one to appear is the one that
            # appears just before the window child exits — and checking
            # poll() first leaves a whole sleep interval in which such a
            # helper is born, orphaned and never recorded.
            _capture()
            if self._process.poll() is not None:
                return _finish("graceful")
            time.sleep(0.1)

        logger.warning("Window child did not exit on request — terminating this launcher's own child only.")
        _capture()
        try:
            self._process.terminate()
        except Exception:
            logger.warning("terminate() failed on the window child.", exc_info=True)

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            _capture()
            if self._process.poll() is not None:
                return _finish("terminated")
            time.sleep(0.1)

        _capture()
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
        return _finish("killed")

    def last_cleanup_report(self) -> Optional[CleanupReport]:
        """What the last stop() did to the window child's descendants, or
        None if stop() has not run. Structured rather than logged-only so
        a failure can be diagnosed from the return value."""
        return self._last_cleanup

    def _close_listener(self) -> None:
        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass
            self._listener = None
