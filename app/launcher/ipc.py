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
VALID_COMMANDS = frozenset({COMMAND_SHOW, COMMAND_HIDE, COMMAND_FOCUS, COMMAND_RELOAD, COMMAND_QUIT})

# Child -> parent.
EVENT_READY = "ready"
EVENT_CLOSED = "closed"
EVENT_ERROR = "error"
VALID_EVENTS = frozenset({EVENT_READY, EVENT_CLOSED, EVENT_ERROR})

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
    """multiprocessing.connection's authkey must be bytes."""
    import secrets
    return secrets.token_bytes(32)


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

    def accept(self) -> bool:
        """Blocks until the child authenticates. Returns False rather
        than raising if the handshake fails — a child that cannot prove
        it holds the secret is simply not accepted, and the caller
        decides what to do about it."""
        try:
            self._conn = self._listener.accept()
            return True
        except Exception:
            logger.warning("Window control connection was not accepted.", exc_info=True)
            return False

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
    return {
        "address": address,
        "secret": secret.encode("utf-8"),
        "url": url,
        "close_action": env.get(IPC_CLOSE_ACTION_ENV, "tray"),
    }
