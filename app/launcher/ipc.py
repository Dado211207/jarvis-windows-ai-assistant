"""Authenticated parent<->child control channel for the window process.

Built on multiprocessing.connection, deliberately, rather than a second
HTTP endpoint: an HTTP control surface would either need to be
unauthenticated (unacceptable — anything on the machine could then drive
JARVIS's lifecycle) or would duplicate the session-token machinery the
API already owns, for a channel that is not part of the API at all.
multiprocessing.connection instead performs an HMAC challenge/response
against a shared authkey on every connection, which is exactly the
property needed here and is implemented in the standard library.

Transport: a loopback TCP socket on an OS-assigned port. AF_PIPE (a real
Windows named pipe) would also work on Windows, but it does not exist on
POSIX, and keeping one transport for both platforms is what lets the
whole protocol be tested on this repo's Linux CI rather than only
verified by hand on Windows. The listener binds 127.0.0.1 explicitly, so
the channel is never reachable off the machine, and the authkey means
even a local process cannot issue a command without the secret.

The address and secret reach the child through its inherited
environment, never argv — see app/launcher/server_process.py's
environment() for the same reasoning (a Windows command line is readable
by other processes; an environment block is not).

Message shape is a small, closed vocabulary of dicts:

    {"command": "show"}                 parent -> child
    {"event": "closed", "reason": ...}  child  -> parent

Anything that is not a dict, carries an unknown command/event name, or
is missing required fields is rejected without being acted on — see
validate_command()/validate_event(), which are pure functions precisely
so the validation rules are testable without any sockets at all.
"""

import os
from typing import Any, Dict, Optional, Tuple

from app.logging_config import get_logger

logger = get_logger("launcher.ipc")

IPC_ADDRESS_ENV = "JARVIS_WINDOW_IPC_ADDRESS"
IPC_SECRET_ENV = "JARVIS_WINDOW_IPC_SECRET"
IPC_URL_ENV = "JARVIS_WINDOW_URL"
IPC_CLOSE_ACTION_ENV = "JARVIS_WINDOW_CLOSE_ACTION"

DEFAULT_ACCEPT_TIMEOUT_SECONDS = 30.0

# Parent -> child. Closed set: an unknown command is a bug or an attack,
# never something to attempt.
COMMAND_SHOW = "show"
COMMAND_HIDE = "hide"
COMMAND_FOCUS = "focus"
COMMAND_RELOAD = "reload"
COMMAND_QUIT = "quit"
# The only command with no visible effect. It exists so readiness can be
# *proved* rather than assumed: "the child process is alive" and "the
# child is still reading its control channel" are different facts, and
# every other command changes what the user sees, so none of them can be
# used as a liveness probe.
COMMAND_PING = "ping"
VALID_COMMANDS = frozenset({
    COMMAND_SHOW, COMMAND_HIDE, COMMAND_FOCUS, COMMAND_RELOAD, COMMAND_QUIT, COMMAND_PING,
})

# Child -> parent.
#
# EVENT_READY means "create_window() returned an object and the window
# child reached the point where its command pump could start" — not
# "this process started", and **not** "a window is on screen".
#
# The first distinction is load-bearing: the parent used to treat the
# control connection itself as proof the window worked, so a machine
# where the window could never be created reported a healthy start and
# showed the user nothing at all.
#
# The second is the limit of what this event can carry. The pump starts
# as soon as `webview_window.current_window()` is non-None, and
# `create_and_run()` publishes that object from `webview.create_window()`,
# which returns *before* `webview.start()` is called. So READY proves
# neither a successful WebView2 navigation, nor a rendered dashboard, nor
# a single visible pixel. See app/launcher/desktop_ready.py for the full
# reasoning, including why pywebview's `loaded` event is not the missing
# proof — it fires on an error page too.
EVENT_READY = "ready"
EVENT_CLOSED = "closed"
EVENT_ERROR = "error"
# The answer to COMMAND_PING. A reply, not a status flag: it can only be
# produced by a child that read the command off the channel and ran its
# handler, which is the whole point.
EVENT_PONG = "pong"
VALID_EVENTS = frozenset({EVENT_READY, EVENT_CLOSED, EVENT_ERROR, EVENT_PONG})

# Why a window could not be created. Carried on EVENT_ERROR so the parent
# can offer the right repair instead of a generic failure — "install the
# WebView2 runtime" and "something else went wrong" are different
# problems with different fixes.
ERROR_WEBVIEW2_MISSING = "webview2_missing"
ERROR_DOTNET_MISSING = "dotnet_missing"
ERROR_WINDOW_FAILED = "window_failed"
VALID_ERROR_DETAILS = frozenset({ERROR_WEBVIEW2_MISSING, ERROR_DOTNET_MISSING, ERROR_WINDOW_FAILED})

# Why the window closed. The distinction matters: a user clicking X with
# close-to-tray enabled must not end the application, while a close that
# the parent itself requested is part of a real shutdown.
REASON_USER_CLOSED = "user_closed"
REASON_QUIT_COMMAND = "quit_command"
VALID_CLOSE_REASONS = frozenset({REASON_USER_CLOSED, REASON_QUIT_COMMAND})


def validate_command(message: Any) -> Optional[str]:
    """Returns the command name if *message* is a well-formed, known
    command, else None. Pure and total — never raises, whatever arrives
    on the wire."""
    if not isinstance(message, dict):
        return None
    command = message.get("command")
    if not isinstance(command, str) or command not in VALID_COMMANDS:
        return None
    return command


def validate_event(message: Any) -> Optional[Dict[str, Any]]:
    """Returns a normalised event dict, or None if malformed/unknown."""
    if not isinstance(message, dict):
        return None
    name = message.get("event")
    if not isinstance(name, str) or name not in VALID_EVENTS:
        return None
    result: Dict[str, Any] = {"event": name}
    if name == EVENT_CLOSED:
        reason = message.get("reason")
        if not isinstance(reason, str) or reason not in VALID_CLOSE_REASONS:
            return None
        result["reason"] = reason
    if name == EVENT_ERROR:
        detail = message.get("detail")
        result["detail"] = detail if isinstance(detail, str) else ""
    return result


def generate_secret() -> bytes:
    """A shared authkey that survives the trip through an environment
    variable intact.

    multiprocessing.connection's authkey must be bytes, and the obvious
    `secrets.token_bytes(32)` is the wrong kind of bytes: the secret
    reaches the child through its environment, which is *text*, so it is
    decoded and re-encoded on the way. Arbitrary binary does not survive
    that — measured, not assumed: 200 of 200 random 32-byte secrets came
    back different, typically losing a third of their length to
    `errors="ignore"` silently discarding every byte sequence that is not
    valid UTF-8.

    The consequence was not subtle and not rare. The window child failed
    its HMAC challenge every single time, on every build, so the native
    window never appeared and the launcher fell back to a browser — the
    reported "the default installed application opens an external
    browser" defect, whose real cause was here rather than in the window
    code. It was invisible because both halves looked correct in
    isolation and the failure surfaced three modules away.

    token_urlsafe() is ASCII by construction, so the round trip is exact,
    with the same 32 bytes of entropy behind it.
    """
    import secrets
    return secrets.token_urlsafe(32).encode("ascii")


class ControlListener:
    """Parent side. Binds a loopback port before the child is spawned, so
    the address is known in time to be put in the child's environment."""

    def __init__(self, secret: bytes, connection_module=None) -> None:
        self._secret = secret
        self._mp = connection_module
        if self._mp is None:
            from multiprocessing import connection as _connection
            self._mp = _connection
        self._listener = self._mp.Listener(("127.0.0.1", 0), authkey=secret)
        self._conn = None

    @property
    def address(self) -> Tuple[str, int]:
        return self._listener.address

    def address_string(self) -> str:
        host, port = self.address
        return f"{host}:{port}"

    def accept(self, timeout_seconds: float = DEFAULT_ACCEPT_TIMEOUT_SECONDS) -> bool:
        """Waits, for a bounded time, for the child to authenticate.

        **The timeout is the whole point.** This method used to call
        `Listener.accept()` directly, which blocks forever with no way to
        interrupt it. A window child that died before connecting — a
        missing runtime DLL, a failed interpreter bootstrap, anything at
        all — therefore parked the parent's main thread permanently: no
        window, no tray, no error, no way to quit. That is the failure
        this constant was declared for and never wired to.

        Implemented by running the blocking accept on a daemon thread and
        closing the listener on timeout, rather than reaching into
        Listener's private socket to set SO_RCVTIMEO. Closing is what
        actually unblocks a thread parked in accept(), and it uses only
        public API.
        """
        import threading

        result: Dict[str, Any] = {}

        def _accept() -> None:
            try:
                result["conn"] = self._listener.accept()
            except Exception as exc:  # noqa: BLE001 — reported via result, not raised
                result["error"] = exc

        worker = threading.Thread(target=_accept, daemon=True, name="jarvis-ipc-accept")
        worker.start()
        worker.join(timeout=timeout_seconds)

        if "conn" in result:
            self._conn = result["conn"]
            return True

        if worker.is_alive():
            logger.error(
                # %.3g, not %.0f: a sub-second timeout printed as "within
                # 0s" reads like the wait never happened, which is the
                # opposite of what this line is reporting.
                "Window child did not connect within %.3gs; abandoning the control channel.",
                timeout_seconds,
            )
            # Closing the listener is what releases the parked accept()
            # thread. Without it the thread would live until process exit.
            try:
                self._listener.close()
            except Exception:
                pass
            return False

        logger.warning("Window control connection was not accepted.", exc_info=result.get("error"))
        return False

    def wait_for_event(self, name: str, timeout_seconds: float) -> Optional[Dict[str, Any]]:
        """Waits for one specific event, returning it, or the first
        *error* event, or None on timeout.

        Exists because "the child process connected" and "the child got
        as far as building a window object" are different facts, and the
        parent was treating the first as proof of the second. Neither is
        proof that anything is on screen — see EVENT_READY above. Events that
        are neither the one awaited nor an error are skipped rather than
        discarded silently — they are valid protocol traffic that simply
        arrived first.
        """
        import time

        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            event = self.poll_event(timeout=min(remaining, 0.25))
            if event is None:
                continue
            if event["event"] in (name, EVENT_ERROR):
                return event

    def send_command(self, command: str) -> bool:
        """Refuses to put an unknown command on the wire in the first
        place, so a caller typo can never reach the child."""
        if command not in VALID_COMMANDS:
            logger.error("Refusing to send unknown window command %r.", command)
            return False
        if self._conn is None:
            return False
        try:
            self._conn.send({"command": command})
            return True
        except Exception:
            logger.warning("Could not send %r to the window child.", command, exc_info=True)
            return False

    def poll_event(self, timeout: float = 0.0) -> Optional[Dict[str, Any]]:
        """Returns the next valid event, or None if nothing arrived or
        what arrived was malformed. Malformed input is dropped and
        logged, never acted on."""
        if self._conn is None:
            return None
        try:
            if not self._conn.poll(timeout):
                return None
            raw = self._conn.recv()
        except Exception:
            return None
        event = validate_event(raw)
        if event is None:
            logger.warning("Discarded a malformed message from the window child.")
        return event

    def close(self) -> None:
        for closeable in (self._conn, self._listener):
            try:
                if closeable is not None:
                    closeable.close()
            except Exception:
                pass
        self._conn = None


class ControlClient:
    """Child side."""

    def __init__(self, address: Tuple[str, int], secret: bytes, connection_module=None) -> None:
        self._mp = connection_module
        if self._mp is None:
            from multiprocessing import connection as _connection
            self._mp = _connection
        self._conn = self._mp.Client(address, authkey=secret)

    def send_event(self, event: str, **fields) -> bool:
        if event not in VALID_EVENTS:
            return False
        try:
            self._conn.send({"event": event, **fields})
            return True
        except Exception:
            return False

    def poll_command(self, timeout: float = 0.0) -> Optional[str]:
        try:
            if not self._conn.poll(timeout):
                return None
            raw = self._conn.recv()
        except Exception:
            return None
        command = validate_command(raw)
        if command is None:
            logger.warning("Discarded a malformed command from the parent.")
        return command

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def parse_address(value: str) -> Optional[Tuple[str, int]]:
    """"127.0.0.1:54321" -> ("127.0.0.1", 54321). None if unparseable —
    the window child uses this to fail safe when launched without a
    proper inherited context."""
    if not value or ":" not in value:
        return None
    host, _, port = value.rpartition(":")
    if host != "127.0.0.1":
        return None  # never dial anything but loopback
    try:
        return host, int(port)
    except ValueError:
        return None


def child_context_from_env(env: Optional[dict] = None) -> Optional[dict]:
    """The window child's full inherited context, or None if any part is
    missing/invalid. Returning None (rather than raising) is what lets
    `JARVIS.exe --window`, run directly by a curious user with no parent,
    exit with a clear message instead of a traceback."""
    env = env if env is not None else os.environ
    address = parse_address(env.get(IPC_ADDRESS_ENV, ""))
    secret = env.get(IPC_SECRET_ENV, "")
    url = env.get(IPC_URL_ENV, "")
    if address is None or not secret or not url:
        return None

    # The exact inverse of what the parent wrote — see generate_secret()
    # for why this pairing is load-bearing and what broke when the two
    # halves disagreed. Non-ASCII means this did not come from a JARVIS
    # parent, which is the same "no usable context" answer as a missing
    # variable; the never-raises promise above covers it too.
    try:
        secret_bytes = secret.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        logger.warning("The inherited window secret was not the expected encoding; refusing to run.")
        return None

    return {
        "address": address,
        "secret": secret_bytes,
        "url": url,
        "close_action": env.get(IPC_CLOSE_ACTION_ENV, "tray"),
    }
