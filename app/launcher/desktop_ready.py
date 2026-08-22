"""The parent's authenticated "the desktop application is up" signal.

This exists because "the server answers /health" is not the same claim as
"JARVIS is running and can be controlled", and treating them as one cost
a real CI failure: the acceptance test read health as fully-started, sent
a graceful close a second before the tray's message loop existed, and
nothing received it.

The first fix was to wait for a line in the boot trace. That is fine as
evidence in a failure report and wrong as a contract — a human-readable
log line is not an interface, and anything that parses one is one
rewording away from breaking. So readiness is a real signal, published by
the parent, which is the only process that knows all four facts:

    server_healthy   the server child answered /health
    window_alive     the window child answered a ping over the
                     authenticated control channel — proof it is still
                     servicing commands, not merely that a process exists
    tray_listening   the tray's message loop dispatched a message the
                     parent posted to it, so Quit, Restart and Show can
                     actually be delivered
    parent_running   the parent finished startup and owns the lock

Each is *proved*, never assumed. A flag the parent sets because it
believes it started correctly would report exactly the state that already
failed on real hardware.

**Authentication.** The parent publishes to the server child over
loopback, presenting the per-session secret the two of them already share
(app/launcher/server_process.py passes it by inherited environment,
never argv). Only a process that inherited that secret can declare the
desktop ready, so nothing else on the machine can forge it. Reading the
signal is unauthenticated: it reports no user data and answering "not
ready yet" to anything that asks is harmless.

`session_id` changes on every server start, so a caller can tell a fresh
runtime apart from the one it was talking to before — which is what makes
"restart produced a genuinely new session" checkable rather than assumed.
"""

import threading
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from app.logging_config import get_logger

logger = get_logger("launcher.desktop_ready")

READY_HEADER = "X-JARVIS-Desktop-Secret"
PUBLISH_TIMEOUT_SECONDS = 5.0


@dataclass
class DesktopReadyState:
    """What the parent has actually verified. `ready` is derived, never
    set directly, so it cannot drift from its own evidence."""

    server_healthy: bool = False
    window_alive: bool = False
    tray_listening: bool = False
    parent_running: bool = False
    session_id: str = ""
    detail: str = "Starting…"

    @property
    def ready(self) -> bool:
        return (
            self.server_healthy
            and self.window_alive
            and self.tray_listening
            and self.parent_running
        )

    def to_payload(self) -> dict:
        payload = asdict(self)
        payload["ready"] = self.ready
        return payload

    def missing(self) -> list:
        """Which facts are not yet true — so a caller waiting on
        readiness can say what it is waiting for instead of timing out
        against a bare False."""
        names = ("server_healthy", "window_alive", "tray_listening", "parent_running")
        return [name for name in names if not getattr(self, name)]


@dataclass
class DesktopReadyPublisher:
    """Parent-side. Gathers the four facts and pushes the result to the
    server child, which is the only process with an HTTP surface.

    *probe_window* and *post* are injection seams so the whole publishing
    decision is testable without a window child or a live server.
    """

    host: str
    port: int
    session_secret: str
    probe_window: Optional[Callable[[], bool]] = None
    post: Optional[Callable] = None
    _state: DesktopReadyState = field(default_factory=DesktopReadyState, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        # An identifier of its own, never derived from session_secret.
        # Reading readiness is unauthenticated, so anything published here
        # is public — a prefix of the secret would have handed twelve
        # characters of it to anything that asked.
        import secrets

        self._state.session_id = secrets.token_hex(8)

    @property
    def state(self) -> DesktopReadyState:
        with self._lock:
            return DesktopReadyState(**{
                k: v for k, v in asdict(self._state).items()
            })

    def update(self, **facts) -> DesktopReadyState:
        """Record verified facts and publish the result.

        Only the four evidence fields and `detail` may be set; `ready` is
        derived. Never raises — readiness reporting must not be able to
        take down the thing it reports on.
        """
        allowed = {"server_healthy", "window_alive", "tray_listening", "parent_running", "session_id", "detail"}
        with self._lock:
            for key, value in facts.items():
                if key not in allowed:
                    logger.warning("Ignoring unknown readiness fact %r.", key)
                    continue
                setattr(self._state, key, value)
            snapshot = DesktopReadyState(**{k: v for k, v in asdict(self._state).items()})

        self._publish(snapshot)
        return snapshot

    def verify_window(self) -> bool:
        """Ask the window child to answer a ping. Returns what actually
        happened, and records it."""
        alive = False
        if self.probe_window is not None:
            try:
                alive = bool(self.probe_window())
            except Exception:  # noqa: BLE001
                logger.warning("The window liveness probe failed.", exc_info=True)
                alive = False
        self.update(window_alive=alive)
        return alive

    def _publish(self, snapshot: DesktopReadyState) -> None:
        poster = self.post if self.post is not None else _post_with_httpx
        try:
            poster(
                f"http://{self.host}:{self.port}/desktop/ready",
                snapshot.to_payload(),
                {READY_HEADER: self.session_secret},
            )
        except Exception:  # noqa: BLE001 — a failed publish must never break startup
            logger.warning("Could not publish the desktop readiness signal.", exc_info=True)


def _post_with_httpx(url: str, payload: dict, headers: dict) -> None:
    import httpx

    httpx.post(url, json=payload, headers=headers, timeout=PUBLISH_TIMEOUT_SECONDS)
