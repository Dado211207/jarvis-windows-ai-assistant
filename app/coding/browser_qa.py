"""Real browser checks against the owned loopback preview.

What a coding agent most often gets wrong is claiming a page works
because the HTTP request returned 200. A 200 is compatible with a blank
page, a React error boundary, a missing bundle and a script that threw
before rendering anything. These checks open the page in a real browser
and report what actually happened.

**This runs from the installed application, not only from a checkout.**
The previous version needed Playwright, which the packaged Windows build
does not carry, so browser QA reported `available: false` on every
machine a user actually had. It now drives the Chromium engine Windows
already has — Microsoft Edge, or the WebView2 Runtime the installer
requires — over the DevTools protocol. No new dependency, no download,
no installer growth. `docs/browser-qa-architecture.md` has the four
options that were compared and the measurements behind the choice.

**Only the owned preview.** `run_checks` takes a `PreviewSession`, not a
URL, and asks it to prove the process is still the one JARVIS started
before anything navigates. `browser_origin.py` decides what the browser
may reach; `browser_engine.launch_argv` makes every other hostname fail
to resolve inside Chromium. Two independent mechanisms, deliberately.

**Seven outcomes, not two.** See `browser_findings.QaState`. A timeout, a
blocked navigation, a missing engine, a dead preview and a cancellation
are five different problems and are reported as five different things.

**A count is never invented.** Every number starts as `None` and is set
only by something that looked. `available: false` is not a result that
can be made true by changing a flag — it is what
`QaState.ENGINE_UNAVAILABLE` renders as, and the only way to a passing
state is for a browser to have opened the page.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from app.coding import browser_engine, browser_origin, browser_probe, cdp
from app.coding.browser_findings import (
    MAX_SCREENSHOT_BYTES,
    BrowserFindings,
    QaState,
    prune_screenshots,
    screenshot_dir,
    screenshot_name,
    unavailable,
)
from app.core.redaction import redact_message
from app.launcher import process_tree
from app.logging_config import get_logger

logger = get_logger("coding.browser_qa")

# Bounds. A QA check is a step inside a task that already has a deadline,
# so it may never be the thing that makes a task run forever.
MAX_RUN_SECONDS = 120.0
NAVIGATION_TIMEOUT_SECONDS = 25.0
SETTLE_SECONDS = 1.2
VIEWPORT_SETTLE_SECONDS = 0.35
BROWSER_START_TIMEOUT_SECONDS = 25.0
MAX_DETAIL_ROWS = 20

#: The widths a "does this overflow" check is worth running at. 320 is the
#: narrowest phone still in common use; 1280 is an ordinary laptop.
OVERFLOW_WIDTHS = (320, 768, 1280)
VIEWPORT_HEIGHT = 900


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------

class BrowserAvailability:
    """Whether a real browser check can run, and why not when it cannot."""

    def __init__(self, available: bool, reason: str = "", engine: str = "",
                 version: str = "") -> None:
        self.available = available
        self.reason = reason
        self.engine = engine
        self.version = version

    def as_dict(self) -> dict:
        return {"available": self.available, "reason": self.reason,
                "engine": self.engine, "engine_version": self.version}


def availability() -> BrowserAvailability:
    """Is there an engine on this machine? Answered without starting one."""
    engine = browser_engine.find_engine()
    if engine is None:
        return BrowserAvailability(False, browser_engine.unavailable_reason())
    return BrowserAvailability(True, "", engine.display, engine.version)


# --------------------------------------------------------------------------
# The browser process, owned and provably stopped
# --------------------------------------------------------------------------

# The browser's own stderr, kept only to explain a failure to start.
# Four lines is enough to name a missing shared library or a refused
# sandbox; more would be a log of somebody's machine.
MAX_STDERR_BYTES = 16384

#: Anything path-shaped is replaced before the text is shown or logged.
#: A profile directory and an executable path both carry the account name.
_PATHISH = re.compile(r"(?:[A-Za-z]:\\|/)[^\s\"\']{2,}")

#: Lines worth keeping ahead of a raw tail: what signal, what check
#: failed, what could not be loaded, and the first few stack frames.
_DIAGNOSTIC = re.compile(
    r"(Received signal|SIGSEGV|SIGILL|SIGABRT|SEGV_|Check failed|FATAL|"
    r"error while loading|cannot open|not found|Failed to |denied|"
    r"^#\d+\s|\bCHECK\b|DCHECK|assert)", re.IGNORECASE)

#: A symbolised stack frame, e.g. "#4 0x55e3a8 content::Foo::Bar()".
_FRAME = re.compile(r"^#\d+\s")

#: The top of every Chromium crash dump is its own signal handler and the
#: libc trampoline below it. Reporting those says a handler ran, which was
#: never in doubt.
_HANDLER_FRAMES = (
    "CollectStackTrace", "StackTrace::StackTrace", "StackDumpSignalHandler",
    "<path>", "libc", "__restore_rt",
)

#: Chromium says these on every healthy Linux CI start.
_STDERR_NOISE = (
    "Fontconfig", "dbus", "DBus", "GPU process", "gpu_memory",
    "Failed to connect to the bus", "XDG_RUNTIME_DIR",
    "vaapi", "MESA", "libva", "Floating point",
)


class _OwnedBrowser:
    """A browser JARVIS started, and the means to prove it stopped.

    Same rule as everything else in this product that starts a process:
    targets come from walking down from a PID this code spawned, every
    target is a PID *plus* a creation time, and cleanup returns a
    structured report rather than a hope.
    """

    def __init__(self, engine: browser_engine.Engine, argv: List[str],
                 profile_dir: str) -> None:
        self.engine = engine
        self.argv = argv
        self.profile_dir = profile_dir
        self._process: Optional[subprocess.Popen] = None
        self._own: Optional[process_tree.ProcessIdentity] = None
        self._captured: List[process_tree.ProcessIdentity] = []
        self._stderr = tempfile.TemporaryFile()   # closed in stop()

    def start(self) -> None:
        from app.coding.runner import _identity_for_pid, build_environment

        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        self._process = subprocess.Popen(  # noqa: S603 — argv list, shell=False
            self.argv,
            env=build_environment(),   # allowlisted: no ANTHROPIC_API_KEY, ever
            stdout=subprocess.DEVNULL,
            # Kept, not discarded. A browser that will not start says why
            # on stderr, and throwing that away left "The browser
            # connection closed." as the whole of the evidence — fifteen
            # CI failures whose actual cause was unreadable from the log.
            # It goes to a temporary file rather than a pipe because
            # nothing drains a pipe while the browser runs, and a full
            # pipe buffer would block the process we are inspecting.
            stderr=self._stderr,
            stdin=subprocess.DEVNULL,
            shell=False,
            creationflags=creationflags,
        )
        self._own = _identity_for_pid(self._process.pid)
        self.recapture()

    def startup_error(self) -> str:
        """The tail of the browser's own stderr, redacted and bounded.

        Only the last few lines, only when the browser failed: enough to
        name a missing library or a refused sandbox, short enough for a
        UI. Paths are stripped — a profile directory and an executable
        path both carry the account name, and this reaches a log file.
        """
        try:
            self._stderr.seek(0)
            raw = self._stderr.read(MAX_STDERR_BYTES)
        except (OSError, ValueError):
            return ""
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        kept, interesting = [], []
        for line in text.splitlines():
            line = _PATHISH.sub("<path>", line).strip()
            # Chromium is chatty about fonts, dbus and GPU probing on any
            # Linux CI machine; those lines are noise on a healthy run.
            if not line or any(noise in line for noise in _STDERR_NOISE):
                continue
            line = line[:200]
            kept.append(line)
            if _DIAGNOSTIC.search(line):
                interesting.append(line)
        # A crash dump ends with a register dump, which says only *that* it
        # crashed, and *begins* with the signal handler's own frames, which
        # say only that a handler ran. Both are noise. What names the fault
        # is the message just before the signal and the frames below the
        # handler — so messages are kept, handler frames are dropped, and
        # the frames after them are what is reported.
        messages = [line for line in interesting if not _FRAME.match(line)]
        frames = [line for line in interesting if _FRAME.match(line)]
        useful = [f for f in frames if not any(h in f for h in _HANDLER_FRAMES)]
        chosen = messages[:2] + useful[:8]
        if not chosen:
            chosen = kept[-4:]
        return " | ".join(chosen)

    def recapture(self) -> None:
        """Chromium starts its renderer and GPU children lazily. One born
        after the first capture is exactly the one that gets orphaned."""
        if self._process is None:
            return
        known = {(i.pid, i.create_time) for i in self._captured}
        for identity in process_tree.capture_descendants(self._process.pid):
            if (identity.pid, identity.create_time) not in known:
                self._captured.append(identity)

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stop(self) -> dict:
        """Terminate the browser and everything it started. Never raises."""
        self.recapture()
        report_dict: dict = {"outcomes": {}, "survivors": []}
        try:
            targets = list(self._captured)
            if self._own is not None:
                # Labelled so a survivor report says whether the process
                # that outlived cleanup was the browser we spawned or one
                # of its children. `replace` because ProcessIdentity is
                # frozen, and `source` is excluded from equality so this
                # cannot affect the reuse check.
                import dataclasses

                targets.append(   # the root last, so children stay reachable
                    dataclasses.replace(self._own, source=process_tree.FROM_ROOT)
                )
            if targets:
                from app.coding import limits

                report = process_tree.terminate_identities(
                    targets,
                    terminate_grace_seconds=limits.TERMINATE_GRACE_SECONDS,
                    kill_grace_seconds=limits.KILL_GRACE_SECONDS,
                )
                report_dict = {
                    "outcomes": report.outcomes(),
                    "survivors": [r.as_dict() for r in report.survivors],
                    "ok": report.ok,
                    "summary": report.summary(),
                }
        except Exception:  # noqa: BLE001 — cleanup must never raise
            logger.warning("Browser cleanup raised; continuing.", exc_info=True)
            report_dict["error"] = "cleanup_failed"
        finally:
            self._remove_profile()
            try:
                self._stderr.close()
            except Exception:  # noqa: BLE001 — cleanup must never raise
                pass
        return report_dict

    def _remove_profile(self) -> None:
        """The profile is deleted after every run, not reused.

        A reused profile accumulates the history, cookies and local storage
        of every page a coding task ever opened, in a directory nothing in
        the product would think to clear.
        """
        try:
            shutil.rmtree(self.profile_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            logger.debug("Could not remove the browser profile directory.", exc_info=True)


# --------------------------------------------------------------------------
# Event collection
# --------------------------------------------------------------------------

class _Collector:
    """Turns raw CDP events into the counts the findings carry.

    Every string that comes out of here has been through
    `redact_message()`. A page can print whatever it likes to the console,
    and these strings end up in a task record, an event and possibly a
    screenshot the user shares.
    """

    #: Schemes that are inert: they carry their own content and cannot
    #: reach the network, so a request for one is not an attempt to leave
    #: loopback. Chromium's own error page is built from `data:` images,
    #: and recording those as blocked origins buried the real entries under
    #: several hundred characters of base64 each.
    #: `browser_error` joins them for a different reason: it is the error
    #: page Chromium shows *because* something was blocked, so recording it
    #: alongside the real attempt would report the defence as a second
    #: attempt.
    INERT_CATEGORIES = ("data_scheme", "blob_scheme", "browser_error")

    def __init__(self, port: int) -> None:
        self.port = port
        self.console_errors: List[str] = []
        self.page_errors: List[str] = []
        self.failed_requests: List[str] = []
        self.http_errors: List[str] = []
        self.blocked: List[str] = []
        #: Where the page tried to *go*, as opposed to what it tried to
        #: fetch. Needed for the message: when a navigation is refused,
        #: Chromium leaves the tab on `chrome-error://chromewebdata`, and
        #: reporting that address tells the user nothing about what their
        #: page attempted.
        self.blocked_navigations: List[str] = []
        self.document_status: Optional[int] = None
        self._urls: Dict[str, str] = {}
        self._origin = browser_origin.origin_for(port)
        # Requests the *browser* made on its own behalf. See `_is_implicit`.
        self._implicit_ids: set = set()
        self._implicit_urls: set = set()
        # URLs the boundary refused. Their network error messages are not
        # defects in the page and are not counted as console errors.
        self._blocked_urls: set = set()
        #: True totals. The detail lists are capped for display; a count
        #: taken from a capped list would under-report the defect. Every
        #: event reaches this handler even after the session stops
        #: *retaining* events, so these numbers are real.
        self.counts: Dict[str, int] = {"console": 0, "page": 0,
                                       "failed": 0, "http": 0}

    # -- helpers ----------------------------------------------------------

    def _short(self, url: str) -> str:
        """`/assets/main.js`, not `http://127.0.0.1:5180/assets/main.js`.

        Shorter to read, and it keeps the port — incidental, and different
        on every run — out of a record the user may share. Replaced
        wherever it appears rather than only at the start, because some of
        these strings are labels with the URL embedded in them.
        """
        text = url.replace(self._origin, "") if self._origin else url
        return redact_message(text or "/")[:200]

    def _note_foreign(self, url: str, why: str) -> None:
        verdict = browser_origin.classify(url, self.port)
        if verdict.allowed or verdict.category in self.INERT_CATEGORIES:
            return
        self._blocked_urls.add(url)
        if why in ("navigation", "redirect", "landed", "popup"):
            if url not in self.blocked_navigations:
                self.blocked_navigations.append(url)
        entry = f"{why}: {verdict.category} — {self._short(url)}"
        if entry not in self.blocked and len(self.blocked) < MAX_DETAIL_ROWS:
            self.blocked.append(entry)

    def _cap(self, name: str, bucket: List[str], value: str) -> None:
        """Count every occurrence; keep only the first few to show."""
        self.counts[name] = self.counts.get(name, 0) + 1
        if len(bucket) < MAX_DETAIL_ROWS * 5:
            bucket.append(value)

    def detail_was_capped(self) -> bool:
        """Were there more examples than are being shown?"""
        return (self.counts["console"] > MAX_DETAIL_ROWS
                or self.counts["page"] > MAX_DETAIL_ROWS
                or self.counts["failed"] > MAX_DETAIL_ROWS
                or self.counts["http"] > MAX_DETAIL_ROWS)

    @staticmethod
    def _is_implicit(url: str, params: dict) -> bool:
        """Did the browser ask for this, rather than the page?

        Chromium requests `/favicon.ico` for every page whether or not the
        document mentions one, and a site with no favicon answers 404. That
        404 was being reported as a failed request *and* logged as a console
        error, so a page with nothing wrong with it scored two problems and
        could never reach "Passed" — which would have made the clean-fixture
        half of the acceptance test impossible to satisfy honestly.

        The signature is exact rather than a path match: resource type
        `Other` with initiator `other` is the browser acting on its own. A
        page that *declares* `<link rel="icon">` produces initiator
        `parser`, and a 404 on that is the page's defect and is reported.
        """
        if params.get("type") != "Other":
            return False
        if (params.get("initiator") or {}).get("type") != "other":
            return False
        try:
            path = urlsplit(url).path.lower()
        except ValueError:
            return False
        return path in ("/favicon.ico", "/apple-touch-icon.png",
                        "/apple-touch-icon-precomposed.png")

    # -- the handler ------------------------------------------------------

    def handle(self, method: str, params: dict) -> None:
        if method == "Runtime.consoleAPICalled":
            if params.get("type") in ("error", "assert"):
                self._cap("console", self.console_errors, redact_message(
                    _console_text(params))[:cdp.MAX_EVENT_TEXT])

        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if entry.get("level") != "error":
                return
            url = str(entry.get("url", ""))
            if url in self._implicit_urls:
                return          # the browser's own favicon probe; see _is_implicit
            if url in self._blocked_urls:
                # "Failed to load resource" for an address the boundary
                # refused. That is this feature working, not a defect in the
                # page, and it is already reported under blocked origins.
                return
            if entry.get("source") == "network":
                # The network layer's own "Failed to load resource" line.
                # Every one of these has a `Network.responseReceived` or
                # `Network.loadingFailed` behind it, so counting it here as
                # well made one missing image score three problems — a
                # console error, a failed request and a broken image. The
                # request is still reported; it is reported once.
                return
            text = f"{entry.get('source', 'log')}: {entry.get('text', '')}"
            self._cap("console", self.console_errors,
                      redact_message(text)[:cdp.MAX_EVENT_TEXT])

        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            text = (details.get("exception") or {}).get("description") or \
                details.get("text") or "an exception"
            self._cap("page", self.page_errors,
                      redact_message(str(text))[:cdp.MAX_EVENT_TEXT])

        elif method == "Network.requestWillBeSent":
            url = str((params.get("request") or {}).get("url", ""))
            request_id = str(params.get("requestId", ""))
            self._urls[request_id] = url
            if self._is_implicit(url, params):
                self._implicit_ids.add(request_id)
                self._implicit_urls.add(url)
            if params.get("redirectResponse"):
                # A redirect: this URL is where the server has sent the
                # browser, which for a Document is a navigation rather than
                # a fetch. Naming it that way is what lets the report say
                # "your page redirected to X" instead of "a request failed".
                self._note_foreign(url, "redirect")
            else:
                self._note_foreign(url, "request")

        elif method == "Network.responseReceived":
            response = params.get("response") or {}
            url = str(response.get("url", ""))
            status = response.get("status")
            if params.get("type") == "Document" and self.document_status is None:
                self.document_status = status
            if str(params.get("requestId", "")) in self._implicit_ids:
                return
            if isinstance(status, int) and status >= 400:
                self._cap("http", self.http_errors, f"HTTP {status} {self._short(url)}")

        elif method == "Network.loadingFailed":
            request_id = str(params.get("requestId", ""))
            if request_id in self._implicit_ids:
                return
            url = self._urls.get(request_id, "")
            error = str(params.get("errorText", "failed"))
            verdict = browser_origin.classify(url, self.port) if url else None
            if verdict is not None and not verdict.allowed:
                # Not a defect in the page's own assets: the security
                # boundary refused to let it leave loopback. Recorded as
                # what it is rather than counted as a broken request.
                self._note_foreign(url, "blocked")
            else:
                self._cap("failed", self.failed_requests,
                          f"{self._short(url) or 'a request'} — {redact_message(error)[:120]}")

        elif method in ("Page.frameRequestedNavigation", "Page.frameScheduledNavigation"):
            url = str(params.get("url", ""))
            if url:
                self._note_foreign(url, "navigation")

        elif method == "Page.windowOpen":
            self._note_foreign(str(params.get("url", "")), "popup")


def _console_text(params: dict) -> str:
    """Flatten a console call's arguments into one line."""
    pieces: List[str] = []
    for arg in (params.get("args") or [])[:8]:
        if "value" in arg:
            pieces.append(str(arg["value"]))
        elif arg.get("description"):
            pieces.append(str(arg["description"]))
        else:
            pieces.append(str(arg.get("type", "?")))
    return " ".join(pieces) or "(empty console error)"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run_checks(session, route: str = "/", *, task_id: str = "",
               capture_screenshot: bool = True,
               cancel: Optional[threading.Event] = None) -> BrowserFindings:
    """Open one route on the owned preview and report what happened.

    `session` is a `PreviewSession`. The URL is built from *its* port, so
    there is no parameter here through which a model, a project file or a
    page could name an origin.

    The work runs on its own thread with its own event loop. The coding
    agent calls this from a worker thread where that is unnecessary, but a
    caller inside a request handler is a reasonable thing to be, and
    `asyncio.run()` cannot nest.
    """
    safe = browser_origin.safe_route(route)
    if safe is None:
        return unavailable(
            QaState.BLOCKED,
            "That route is not a path inside the preview, so JARVIS did not open it.",
            route=str(route)[:120],
            fix="Give a path such as /about, not a full URL.",
        )

    ok, why = session.verify_ownership()
    state = session.state
    if not ok or not state.running or not state.port:
        return unavailable(
            QaState.PREVIEW_UNAVAILABLE,
            why or state.last_error or "No preview is running, so there was nothing to open.",
            route=safe,
            fix="Start the preview, then run the check again.",
        )

    url = browser_origin.origin_for(state.port) + safe
    engine = browser_engine.find_engine()
    if engine is None:
        return unavailable(QaState.ENGINE_UNAVAILABLE,
                           browser_engine.unavailable_reason(),
                           route=safe, url=url,
                           fix=browser_engine.unavailable_fix())

    if cancel is not None and cancel.is_set():
        return unavailable(QaState.CANCELLED, "The check was cancelled before it started.",
                           route=safe, url=url)

    result: List[BrowserFindings] = []

    def worker() -> None:
        try:
            result.append(asyncio.run(
                _run_async(engine, state.port, url, safe, task_id,
                           capture_screenshot, cancel)
            ))
        except Exception as exc:  # noqa: BLE001 — a QA check must not end a task
            logger.warning("Browser check failed.", exc_info=True)
            result.append(unavailable(
                QaState.FAILED,
                f"The browser check could not complete ({type(exc).__name__}).",
                route=safe, url=url,
            ))

    thread = threading.Thread(target=worker, name="jarvis-browser-qa", daemon=True)
    thread.start()
    thread.join(timeout=MAX_RUN_SECONDS + 30.0)
    if not result:
        # The thread is daemonic and the browser owns its own cleanup, so a
        # hang here cannot hold the process open. Reported honestly rather
        # than waited on further.
        return unavailable(
            QaState.TIMED_OUT,
            f"The browser check did not finish within {MAX_RUN_SECONDS:.0f}s.",
            route=safe, url=url,
            fix="A page that never stops loading is the usual cause.",
        )
    return result[0]


async def _run_async(engine, port: int, url: str, route: str, task_id: str,
                     capture_screenshot: bool,
                     cancel: Optional[threading.Event]) -> BrowserFindings:
    """Launch, check, and tear down. Always tears down."""
    findings = BrowserFindings(
        state=QaState.FAILED, route=route, url=url,
        engine=engine.display, engine_version=engine.version,
    )
    started = time.monotonic()
    profile_dir = tempfile.mkdtemp(prefix="jarvis-qa-profile-")

    browser = _OwnedBrowser(
        engine,
        # Port 0: Chromium picks a free one and writes it to
        # `DevToolsActivePort` in the profile. Choosing a port ourselves and
        # passing it has a race in it — between our probe closing the socket
        # and the browser binding it, something else can take it, and the
        # check then fails for a reason that has nothing to do with the page.
        browser_engine.launch_argv(engine, debug_port=0,
                                   profile_dir=profile_dir,
                                   allow_host=browser_origin.LOOPBACK),
        profile_dir,
    )
    collector = _Collector(port)

    try:
        try:
            browser.start()
        except (OSError, FileNotFoundError) as exc:
            findings.state = QaState.ENGINE_UNAVAILABLE
            findings.reason = f"The browser would not start ({type(exc).__name__})."
            findings.fix = browser_engine.unavailable_fix()
            return findings

        try:
            socket_url = await _devtools_endpoint(browser, profile_dir,
                                                  BROWSER_START_TIMEOUT_SECONDS)
        except Exception as exc:            # noqa: BLE001 - re-raised or reported
            detail = browser.startup_error()
            if detail:
                findings.state = QaState.ENGINE_UNAVAILABLE
                findings.reason = (
                    f"The browser did not open a debugging endpoint. "
                    f"It reported: {detail}")
                findings.fix = browser_engine.unavailable_fix()
                return findings
            raise
        browser.recapture()

        async with cdp.CdpSession(socket_url, on_event=collector.handle) as page:
            await page.open_page()
            await page.deny_every_permission()
            await _check_page(page, collector, findings, url, route, task_id,
                              capture_screenshot, cancel, started)
            findings.dialogs_dismissed = page.counters.dialogs_dismissed
            findings.downloads_blocked = page.counters.downloads_blocked
            findings.truncated = page.overflowed or findings.truncated

    except cdp.CdpTimeout as exc:
        findings.state = QaState.TIMED_OUT
        findings.reason = str(exc)
        findings.fix = "A page that never finishes loading is the usual cause."
    except cdp.CdpError as exc:
        # A browser that exited during the check is an engine problem, not
        # a verdict on the page, and its own stderr is the only thing that
        # says which. Discarding it left fifteen CI failures reading only
        # "The browser connection closed."
        detail = browser.startup_error()
        if detail and not browser.is_running():
            findings.state = QaState.ENGINE_UNAVAILABLE
            findings.reason = f"{exc} The browser reported: {detail}"
            findings.fix = browser_engine.unavailable_fix()
        else:
            findings.state = QaState.FAILED
            findings.reason = f"{exc} {detail}".strip() if detail else str(exc)
    except _Blocked as exc:
        findings.state = QaState.BLOCKED
        findings.reason = str(exc)
        findings.fix = ("Browser checks only ever open this task's own preview on "
                        "127.0.0.1. Nothing was fetched from the blocked address.")
    except _Cancelled:
        findings.state = QaState.CANCELLED
        findings.reason = "The check was cancelled."
    except Exception as exc:  # noqa: BLE001
        logger.warning("Browser check failed.", exc_info=True)
        findings.state = QaState.FAILED
        findings.reason = f"The browser check could not complete ({type(exc).__name__})."
    finally:
        findings.cleanup = browser.stop()
        findings.duration_seconds = time.monotonic() - started
        findings.blocked_origins = collector.blocked

    return findings


def _left_the_preview(collector: "_Collector",
                      verdict: Optional[browser_origin.Verdict] = None) -> str:
    """The sentence a user reads when a page tried to leave loopback.

    It must name where the *page* was going. When Chromium refuses a
    navigation it parks the tab on `chrome-error://chromewebdata`, and an
    earlier version reported exactly that — "the page navigated away to the
    chrome-error scheme" — which describes the browser's error page rather
    than the address the project asked for.
    """
    target = ""
    if collector.blocked_navigations:
        target = collector._short(collector.blocked_navigations[-1])
    elif verdict is not None and verdict.detail and verdict.category != "browser_error":
        target = verdict.detail
    where = f" to {target}" if target else ""
    return (
        f"The page tried to navigate away from this task's preview{where}. "
        "JARVIS stopped the check rather than opening another origin, and "
        "nothing was fetched from it."
    )


class _Blocked(Exception):
    """The security boundary refused something."""


class _Cancelled(Exception):
    """The caller asked for the check to stop."""


async def _check_page(page, collector: _Collector, findings: BrowserFindings,
                      url: str, route: str, task_id: str,
                      capture_screenshot: bool,
                      cancel: Optional[threading.Event], started: float) -> None:
    """Everything that happens with a live page. Raises to signal a state."""

    def guard() -> None:
        if cancel is not None and cancel.is_set():
            raise _Cancelled()
        if time.monotonic() - started > MAX_RUN_SECONDS:
            raise cdp.CdpTimeout(
                f"The browser check exceeded its {MAX_RUN_SECONDS:.0f}s budget.")

    await _set_viewport(page, OVERFLOW_WIDTHS[-1])
    guard()

    navigation = await page.command("Page.navigate", {"url": url},
                                    timeout=NAVIGATION_TIMEOUT_SECONDS)
    if navigation.get("errorText"):
        # A navigation that failed *because the boundary refused it* is not
        # a broken page. `/redirect-external` reported "the page did not
        # load: net::ERR_PROXY_CONNECTION_FAILED", which describes JARVIS's
        # own defence and tells the user nothing about what happened.
        if collector.blocked_navigations:
            raise _Blocked(_left_the_preview(collector))
        findings.state = QaState.FAILED
        findings.reason = (
            f"The page did not load: {redact_message(str(navigation['errorText']))[:120]}"
        )
        return

    await page.settle(SETTLE_SECONDS)
    guard()

    # Where did we actually end up? A meta-refresh or a script redirect
    # happens after navigation returns, so the answer is read from the page
    # rather than assumed from the request.
    landed = str(await _evaluate(page, "window.location.href") or "")
    verdict = browser_origin.classify(landed, collector.port)
    if not verdict.allowed:
        collector._note_foreign(landed, "landed")
        raise _Blocked(_left_the_preview(collector, verdict))

    findings.http_status = collector.document_status
    guard()

    facts = await _evaluate(page, browser_probe.PAGE_FACTS_JS) or {}
    findings.title = str(facts.get("title", ""))[:200]
    findings.lang = str(facts.get("lang", ""))[:20]
    findings.h1_count = _as_int(facts.get("h1_count"))
    findings.broken_images = _as_int(facts.get("broken_images"))
    findings.broken_image_details = [
        # Through `_short` like every other URL: `img /banner.png`, not
        # `img http://127.0.0.1:5183/banner.png`. The port is incidental
        # and differs every run.
        collector._short(str(v))[:200]
        for v in (facts.get("broken_image_labels") or [])
    ][:MAX_DETAIL_ROWS]
    findings.scanned_all = bool(facts.get("scanned_all", True))
    guard()

    a11y = await _evaluate(page, browser_probe.ACCESSIBILITY_JS) or {}
    rows = a11y.get("findings") or []
    findings.accessibility_findings = len(rows)
    findings.accessibility_details = [
        {"rule": str(r.get("rule", ""))[:60],
         "detail": redact_message(str(r.get("detail", "")))[:200]}
        for r in rows[:MAX_DETAIL_ROWS]
    ]
    findings.scanned_all = findings.scanned_all and bool(a11y.get("scanned_all", True))
    guard()

    overflow: Dict[str, dict] = {}
    for width in OVERFLOW_WIDTHS:
        await _set_viewport(page, width)
        await page.settle(VIEWPORT_SETTLE_SECONDS)
        measured = await _evaluate(page, browser_probe.OVERFLOW_JS) or {}
        overflow[str(width)] = {
            "overflows": bool(measured.get("overflows")),
            "scroll_width": _as_int(measured.get("scroll_width")),
            "client_width": _as_int(measured.get("client_width")),
            "culprits": [redact_message(str(c))[:120]
                         for c in (measured.get("culprits") or [])][:MAX_DETAIL_ROWS],
        }
        guard()
    findings.horizontal_overflow = overflow

    await _set_viewport(page, OVERFLOW_WIDTHS[-1])
    await page.command("Emulation.setEmulatedMedia", {
        "features": [{"name": "prefers-reduced-motion", "value": "reduce"}],
    })
    await page.settle(VIEWPORT_SETTLE_SECONDS)
    motion = await _evaluate(page, browser_probe.REDUCED_MOTION_JS) or {}
    findings.reduced_motion = {
        "emulated": bool(motion.get("emulated")),
        "respects_reduced_motion": bool(motion.get("respects_reduced_motion")),
        "still_animating": _as_int(motion.get("still_animating")),
        "examples": [redact_message(str(e))[:120]
                     for e in (motion.get("examples") or [])][:MAX_DETAIL_ROWS],
    }
    await page.command("Emulation.setEmulatedMedia", {"features": []})
    guard()

    if capture_screenshot:
        await page.settle(VIEWPORT_SETTLE_SECONDS)
        findings.screenshot = await _capture(page, task_id, route)

    # Everything above returned, so a real browser did load this page and
    # the probes did run. This is the only place in the product that sets
    # it, and it is set after the fact rather than before the attempt.
    findings.opened = True

    # Counts come from the collector's totals, never from the length of a
    # capped list. A page that printed twenty thousand console errors
    # reports twenty thousand and shows the first twenty.
    findings.console_errors = collector.counts["console"]
    findings.console_messages = collector.console_errors[:MAX_DETAIL_ROWS]
    findings.page_errors = collector.counts["page"]
    findings.page_error_messages = collector.page_errors[:MAX_DETAIL_ROWS]
    findings.failed_requests = collector.counts["failed"] + collector.counts["http"]
    findings.failed_request_details = collector.failed_requests[:MAX_DETAIL_ROWS]
    findings.http_error_details = collector.http_errors[:MAX_DETAIL_ROWS]
    findings.truncated = findings.truncated or collector.detail_was_capped()

    problems = findings.problem_count() or 0
    findings.state = QaState.PASSED if problems == 0 else QaState.FAILED
    if findings.state is QaState.FAILED:
        findings.reason = f"{problems} problem(s) were found on the page."


async def _set_viewport(page, width: int) -> None:
    await page.command("Emulation.setDeviceMetricsOverride", {
        "width": width, "height": VIEWPORT_HEIGHT,
        "deviceScaleFactor": 1, "mobile": False,
    })


async def _evaluate(page, expression: str):
    """Run an expression in the page and return its value, or None."""
    result = await page.command("Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": False,
        # A page must not be able to stop a probe by redefining a global it
        # uses. This does not sandbox the page from itself, but it does mean
        # the probe sees the real DOM API rather than a replaced one.
        "includeCommandLineAPI": False,
        "userGesture": False,
    })
    if result.get("exceptionDetails"):
        logger.debug("A page probe threw: %s", result["exceptionDetails"].get("text"))
        return None
    return (result.get("result") or {}).get("value")


async def _capture(page, task_id: str, route: str) -> str:
    """One screenshot, bounded, written under a name that carries no identity."""
    target = screenshot_dir()
    if target is None:
        return ""
    try:
        shot = await page.command("Page.captureScreenshot",
                                  {"format": "png", "captureBeyondViewport": False})
    except cdp.CdpError:
        logger.debug("The screenshot could not be captured.", exc_info=True)
        return ""
    encoded = shot.get("data") or ""
    # Checked before decoding: a base64 string is 4/3 the size of what it
    # decodes to, so the bound is applied to the cheap measurement first.
    if len(encoded) > MAX_SCREENSHOT_BYTES * 4 // 3:
        logger.info("Skipping an oversized screenshot from a browser check.")
        return ""
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:  # noqa: BLE001
        return ""
    if len(raw) > MAX_SCREENSHOT_BYTES:
        return ""
    name = screenshot_name(task_id or "task", route)
    try:
        (target / name).write_bytes(raw)
    except OSError:
        logger.debug("Could not write the screenshot.", exc_info=True)
        return ""
    prune_screenshots()
    return name


async def _devtools_endpoint(browser: "_OwnedBrowser", profile_dir: str,
                             timeout: float) -> str:
    """Where the browser is listening, read from the browser itself.

    Chromium writes `DevToolsActivePort` into its profile directory: the
    port on the first line and the browser's WebSocket path on the second.
    Reading it is exact, needs no HTTP request, and cannot be confused by
    another Chromium the user is running.

    If the second line is missing — some builds have written only the port —
    the endpoint is asked over HTTP instead.
    """
    from pathlib import Path

    marker = Path(profile_dir) / "DevToolsActivePort"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while loop.time() < deadline:
        if browser._process is not None and browser._process.poll() is not None:
            raise cdp.CdpError(
                f"The browser exited immediately (code "
                f"{browser._process.returncode})."
            )
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
        except OSError:
            await asyncio.sleep(0.1)
            continue
        if not lines or not lines[0].strip().isdigit():
            await asyncio.sleep(0.1)
            continue
        debug_port = int(lines[0].strip())
        path = lines[1].strip() if len(lines) > 1 else ""
        if path.startswith("/"):
            return f"ws://{browser_origin.LOOPBACK}:{debug_port}{path}"
        return await cdp.fetch_websocket_url(
            debug_port, max(1.0, deadline - loop.time()))

    raise cdp.CdpTimeout(
        f"The browser did not open its debugging endpoint within {timeout:.0f}s."
    )


def _as_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
