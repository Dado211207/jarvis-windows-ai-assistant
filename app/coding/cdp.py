"""A minimal Chrome DevTools Protocol client.

Deliberately small. This is not a browser-automation library and must not
grow into one: it does exactly what `browser_qa.py` needs — attach to a
target, enable four domains, navigate, collect events, evaluate an
expression, capture a screenshot, and close — and nothing else.

**Why write this rather than take a dependency.** The alternative that
does this is Playwright, whose Python package carries a 137 MB driver
including its own 124 MB private Node runtime, before any browser.
`websockets` is already present through `uvicorn[standard]`, which
`requirements.txt` has always declared, so this costs nothing to ship.
The measured comparison is in `docs/browser-qa-architecture.md`.

**One receive pump, not a read-until-my-reply loop.** The obvious
implementation — send a command, then read messages until the matching
id comes back — deadlocks the first time a page calls `alert()`. With
`Page.enable` on, Chromium blocks the renderer until the dialog is
answered, and the answer is a command that cannot be sent because the
socket is busy waiting for the reply to the command that triggered it.
So a background task owns the socket, resolves command futures, and
answers dialogs and download requests the moment they arrive.

**`proxy=None` is not tidiness.** websockets 17 reads `HTTPS_PROXY` from
the environment by default. On a machine with a proxy configured, a
connection to the browser on `127.0.0.1` would be attempted *through it*.

**Everything is bounded.** A connection timeout, a per-command timeout, a
cap on retained events, a cap on message size, and a cap on how much text
any single event contributes. A browser that stops answering ends the
session rather than hanging the task that started it.

**No URL is ever taken from a caller.** `navigate()` receives a URL that
`browser_qa.py` built from an owned `PreviewSession`'s port. This module
does not validate origins — it is the wrong layer to do so, and putting
the check here would let a future caller reach the socket without it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.logging_config import get_logger

logger = get_logger("coding.cdp")

# Bounds. Every one exists so a misbehaving page cannot make a coding task
# hang or exhaust memory.
CONNECT_TIMEOUT_SECONDS = 20.0
COMMAND_TIMEOUT_SECONDS = 30.0
MAX_MESSAGE_BYTES = 32 * 1024 * 1024
MAX_QUEUED_MESSAGES = 512
MAX_EVENTS_RETAINED = 2000
MAX_EVENT_TEXT = 400

#: Everything a page might ask permission for. Denied browser-wide so a
#: page cannot sit waiting on a prompt that will never be shown.
DENIED_PERMISSIONS = (
    "geolocation", "notifications", "camera", "microphone",
    "clipboardReadWrite", "clipboardSanitizedWrite", "midi", "midiSysex",
    "backgroundSync", "idleDetection", "displayCapture", "storageAccess",
    "windowManagement",
)


class CdpError(Exception):
    """A protocol failure, carrying a message that is safe to display."""


class CdpTimeout(CdpError):
    """The browser did not answer in time. Distinguished because it maps to
    its own user-visible state rather than to a generic failure."""


@dataclass
class Counters:
    """Things the pump handled on the caller's behalf."""

    dialogs_dismissed: int = 0
    downloads_blocked: int = 0
    events_dropped: int = 0
    new_windows_blocked: int = 0
    extra: Dict[str, int] = field(default_factory=dict)


class CdpSession:
    """One attached page. Use as an async context manager."""

    def __init__(self, websocket_url: str,
                 on_event: Optional[Callable[[str, dict], None]] = None) -> None:
        self._url = websocket_url
        self._ws = None
        self._pump: Optional[asyncio.Task] = None
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._session_id: Optional[str] = None
        self._target_id: Optional[str] = None
        self._events: List[dict] = []
        self._on_event = on_event
        self._overflowed = False
        self._closed = asyncio.Event()
        self.counters = Counters()

    # -- lifecycle --------------------------------------------------------

    async def __aenter__(self) -> "CdpSession":
        import websockets

        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self._url,
                    max_size=MAX_MESSAGE_BYTES,
                    max_queue=MAX_QUEUED_MESSAGES,
                    ping_interval=None,
                    open_timeout=CONNECT_TIMEOUT_SECONDS,
                    # See the module docstring: a configured HTTPS_PROXY
                    # must not be used to reach a browser on loopback.
                    proxy=None,
                ),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            raise CdpError(
                f"The browser did not accept a debugging connection ({type(exc).__name__})."
            ) from None
        self._pump = asyncio.create_task(self._receive_loop(), name="jarvis-cdp-pump")
        return self

    async def __aexit__(self, *exc_info) -> None:
        # Order matters: close the target while the socket still works, then
        # stop the pump, then close the socket. Each step is independently
        # guarded — teardown must not raise over a failure in teardown.
        try:
            if self._target_id:
                await asyncio.wait_for(
                    self.command("Target.closeTarget", {"targetId": self._target_id},
                                 session=False),
                    timeout=5.0,
                )
        except Exception:  # noqa: BLE001
            logger.debug("Closing the CDP target failed.", exc_info=True)
        if self._pump is not None:
            self._pump.cancel()
            try:
                await self._pump
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            if self._ws is not None:
                await asyncio.wait_for(self._ws.close(), timeout=5.0)
        except Exception:  # noqa: BLE001
            logger.debug("Closing the CDP socket failed.", exc_info=True)

    # -- the pump ---------------------------------------------------------

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                if "id" in payload:
                    self._resolve(payload)
                else:
                    self._handle_event(payload)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a closed socket is not an error here
            logger.debug("The CDP receive loop ended.", exc_info=True)
        finally:
            self._closed.set()
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(CdpError("The browser connection closed."))
            self._pending.clear()

    def _resolve(self, payload: dict) -> None:
        future = self._pending.pop(payload["id"], None)
        if future is None or future.done():
            return
        if "error" in payload:
            detail = str(payload["error"].get("message", ""))[:200]
            future.set_exception(CdpError(f"The browser refused the request: {detail}"))
        else:
            future.set_result(payload.get("result", {}))

    def _handle_event(self, payload: dict) -> None:
        method = str(payload.get("method", ""))
        params = payload.get("params") or {}

        # Answered here rather than by the caller: both of these block the
        # renderer until they are dealt with, and the caller is by
        # definition awaiting something when they arrive.
        if method == "Page.javascriptDialogOpening":
            self.counters.dialogs_dismissed += 1
            self._fire_and_forget("Page.handleJavaScriptDialog", {"accept": False})
        elif method == "Page.downloadWillBegin":
            self.counters.downloads_blocked += 1
        elif method == "Page.fileChooserOpened":
            self.counters.dialogs_dismissed += 1

        if len(self._events) >= MAX_EVENTS_RETAINED:
            self._overflowed = True
            self.counters.events_dropped += 1
        else:
            self._events.append(payload)

        if self._on_event is not None:
            try:
                self._on_event(method, params)
            except Exception:  # noqa: BLE001 — a collector must not kill the pump
                logger.debug("A CDP event handler raised.", exc_info=True)

    def _fire_and_forget(self, method: str, params: dict) -> None:
        """Send a command whose reply nobody waits for.

        Used only for the two dialog answers above, where waiting would
        re-create the deadlock this design exists to avoid.
        """
        if self._ws is None:
            return
        self._next_id += 1
        message: Dict[str, Any] = {"id": self._next_id, "method": method,
                                   "params": params}
        if self._session_id:
            message["sessionId"] = self._session_id
        try:
            asyncio.create_task(self._ws.send(json.dumps(message)))
        except Exception:  # noqa: BLE001
            logger.debug("Could not send %s.", method, exc_info=True)

    # -- commands ---------------------------------------------------------

    async def command(self, method: str, params: Optional[dict] = None, *,
                      session: bool = True,
                      timeout: float = COMMAND_TIMEOUT_SECONDS) -> dict:
        """Send one command and wait for its reply."""
        if self._ws is None:
            raise CdpError("The browser connection is not open.")

        self._next_id += 1
        message_id = self._next_id
        message: Dict[str, Any] = {"id": message_id, "method": method,
                                   "params": params or {}}
        if session and self._session_id:
            message["sessionId"] = self._session_id

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._ws.send(json.dumps(message))
        except Exception as exc:  # noqa: BLE001
            self._pending.pop(message_id, None)
            raise CdpError(f"The browser connection closed ({type(exc).__name__}).") from None

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(message_id, None)
            raise CdpTimeout(f"The browser did not answer '{method}' within "
                             f"{timeout:.0f}s.") from None

    async def settle(self, seconds: float) -> None:
        """Let the page run for a while. The pump is already collecting."""
        await asyncio.sleep(max(0.0, seconds))

    # -- attach -----------------------------------------------------------

    async def open_page(self) -> None:
        """Create a fresh `about:blank` target and attach to it.

        A new target rather than the browser's initial one: the initial
        page belongs to the browser process and closing it ends the
        browser, which makes an orderly teardown impossible to distinguish
        from a crash.
        """
        created = await self.command("Target.createTarget", {"url": "about:blank"},
                                     session=False)
        self._target_id = created.get("targetId")
        if not self._target_id:
            raise CdpError("The browser did not open a page.")
        attached = await self.command(
            "Target.attachToTarget",
            {"targetId": self._target_id, "flatten": True}, session=False,
        )
        self._session_id = attached.get("sessionId")
        if not self._session_id:
            raise CdpError("The browser did not attach a debugging session.")

        for domain in ("Page.enable", "Runtime.enable", "Log.enable",
                       "Network.enable", "Inspector.enable"):
            await self.command(domain, {})

        # Nothing may be written to disk, and no file dialog may block us.
        await self.command("Page.setDownloadBehavior", {"behavior": "deny"})
        try:
            await self.command("Page.setInterceptFileChooserDialog", {"enabled": True})
        except CdpError:
            # Older builds do not have it; `--headless=new` does not show a
            # file chooser anyway. Not a reason to abandon the run.
            logger.debug("This build does not support file-chooser interception.")

    async def deny_every_permission(self) -> None:
        """Refuse everything a page might ask for, browser-wide.

        `--deny-permission-prompts` already suppresses the prompt; this
        makes the *query* answer "denied" too, so a page cannot detect a
        pending prompt and wait on it.
        """
        for name in DENIED_PERMISSIONS:
            try:
                await self.command("Browser.setPermission",
                                   {"permission": {"name": name}, "setting": "deny"},
                                   session=False, timeout=5.0)
            except CdpError:
                continue

    # -- results ----------------------------------------------------------

    @property
    def events(self) -> List[dict]:
        return list(self._events)

    @property
    def overflowed(self) -> bool:
        return self._overflowed


async def fetch_websocket_url(debug_port: int, timeout: float) -> str:
    """Wait for the browser's debugging endpoint and return its socket URL."""
    import urllib.request

    endpoint = f"http://127.0.0.1:{debug_port}/json/version"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = ""

    def _read() -> str:
        # `urlopen` with an explicit empty-proxy handler: the same reason
        # the websocket sets `proxy=None`. A machine-wide proxy must not be
        # consulted for a browser on loopback.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(endpoint, timeout=1.5) as response:  # noqa: S310 — fixed loopback URL
            return json.load(response)["webSocketDebuggerUrl"]

    while loop.time() < deadline:
        try:
            return await loop.run_in_executor(None, _read)
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
            await asyncio.sleep(0.15)
    raise CdpTimeout(
        f"The browser never opened its debugging port ({last or 'timeout'})."
    )
