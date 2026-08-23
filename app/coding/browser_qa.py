"""Real browser checks against the owned loopback preview.

What a coding agent most often gets wrong is claiming a page works
because the HTTP request returned 200. A 200 is compatible with a blank
page, a React error boundary, a missing bundle and a script that threw
before rendering anything. These checks open the page in a real browser
and report what actually happened.

**Only the owned preview.** `run_checks` takes a `PreviewSession`, not a
URL. There is no parameter here through which a model or a project file
could name an origin, so this cannot become general web browsing — which
§12 of the brief puts out of scope for this pass, and which would be a
much larger security question than a loopback check.

**Absent means absent, never zero.** Playwright is a test dependency; the
packaged Windows build does not carry it or a browser binary. When it is
missing, every check reports `available: False` with the reason, and the
counts are `None` rather than `0`. Reporting "0 console errors" for a
page nothing opened is the exact failure this module exists to avoid —
it is indistinguishable, on screen, from a page that is genuinely clean.

**Screenshots carry no identity.** The file is named from the task id and
a hash of the route, and it is written to JARVIS's own data directory.
A screenshot called `C:\\Users\\dragan\\projects\\...` in a report the
user shares would leak both their account name and their directory
layout.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from app.logging_config import get_logger

logger = get_logger("coding.browser_qa")

SCREENSHOT_DIRNAME = "coding_screenshots"
MAX_SCREENSHOTS_KEPT = 40
PAGE_TIMEOUT_MS = 20_000
SETTLE_MS = 1_200
# The widths a "does this overflow" check is worth running at. 320 is the
# narrowest phone still in common use; 1280 is an ordinary laptop.
OVERFLOW_WIDTHS = (320, 768, 1280)


@dataclass
class BrowserAvailability:
    available: bool
    reason: str = ""

    def as_dict(self) -> dict:
        return {"available": self.available, "reason": self.reason}


def _browser_roots() -> List[Path]:
    """Where Playwright keeps downloaded browsers on this machine."""
    import os

    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if configured and configured != "0":
        return [Path(configured)]
    home = Path.home()
    return [
        home / "AppData" / "Local" / "ms-playwright",   # Windows
        home / ".cache" / "ms-playwright",              # Linux
        home / "Library" / "Caches" / "ms-playwright",  # macOS
    ]


def availability() -> BrowserAvailability:
    """Can a real browser check run right now?

    Two separate questions — the Python package, and a browser binary for
    it to drive — because they fail for different reasons and have
    different fixes.

    Both are answered without starting Playwright's driver process. An
    earlier version opened `sync_playwright()` purely to read
    `chromium.executable_path`, which starts a Node subprocess and a
    websocket to ask a question a directory listing answers — and left a
    cancelled asyncio task behind every time the page merely rendered.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        return BrowserAvailability(
            False,
            "Browser checks need Playwright, which this build of JARVIS does not "
            "include. The HTTP status and page-structure checks still ran.",
        )

    for root in _browser_roots():
        try:
            if root.is_dir() and any(root.glob("chromium*")):
                return BrowserAvailability(True)
        except OSError:
            continue
    return BrowserAvailability(
        False,
        "Playwright is installed but its Chromium browser is not. "
        "Running 'playwright install chromium' would provide it.",
    )


@dataclass
class BrowserFindings:
    """What the browser saw. `None` means not checked, `0` means checked
    and clean — a distinction the UI renders differently."""

    available: bool = False
    reason: str = ""
    url: str = ""
    route: str = "/"
    http_status: Optional[int] = None
    console_errors: Optional[int] = None
    console_messages: List[str] = field(default_factory=list)
    failed_requests: Optional[int] = None
    failed_request_urls: List[str] = field(default_factory=list)
    page_errors: List[str] = field(default_factory=list)
    horizontal_overflow: Optional[Dict[str, object]] = None
    title: str = ""
    h1_count: Optional[int] = None
    screenshot: str = ""
    duration_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "reason": self.reason,
            "url": self.url,
            "route": self.route,
            "http_status": self.http_status,
            "console_errors": self.console_errors,
            "console_messages": self.console_messages,
            "failed_requests": self.failed_requests,
            "failed_request_urls": self.failed_request_urls,
            "page_errors": self.page_errors,
            "horizontal_overflow": self.horizontal_overflow,
            "title": self.title,
            "h1_count": self.h1_count,
            "screenshot": self.screenshot,
            "duration_seconds": round(self.duration_seconds, 2),
        }

    def summary(self) -> str:
        if not self.available:
            return f"Browser check not run — {self.reason}"
        parts = [f"HTTP {self.http_status}" if self.http_status else "loaded"]
        parts.append(f"{self.console_errors} console error(s)")
        parts.append(f"{self.failed_requests} failed request(s)")
        overflowing = [
            f"{w}px" for w, v in (self.horizontal_overflow or {}).items()
            if isinstance(v, dict) and v.get("overflows")
        ]
        parts.append(
            "horizontal overflow at " + ", ".join(overflowing) if overflowing
            else "no horizontal overflow"
        )
        return "; ".join(parts) + "."


def screenshot_dir() -> Optional[Path]:
    from app.core.app_paths import data_dir

    try:
        target = data_dir() / SCREENSHOT_DIRNAME
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError:
        logger.warning("Could not create the screenshot directory.", exc_info=True)
        return None


def screenshot_name(task_id: str, route: str) -> str:
    """A filename that identifies the check and nothing else.

    Not the project path, not the project name, not the account name —
    a task id and eight hex characters of the route. A user pasting a
    findings export into an issue should not be publishing their
    directory layout with it.
    """
    digest = hashlib.sha256(route.encode("utf-8", errors="replace")).hexdigest()[:8]
    safe_task = "".join(c for c in task_id if c.isalnum())[:16] or "task"
    return f"{safe_task}-{digest}-{int(time.time())}.png"


def _prune_screenshots() -> None:
    target = screenshot_dir()
    if target is None:
        return
    try:
        shots = sorted(target.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for stale in shots[:-MAX_SCREENSHOTS_KEPT]:
            stale.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not prune old screenshots.", exc_info=True)


def run_checks(session, route: str = "/", *, task_id: str = "",
               capture_screenshot: bool = True) -> BrowserFindings:
    """Open one route on the owned preview and report what happened.

    `session` is a `PreviewSession`. The URL is built from *its* port, so
    a caller cannot redirect this at another origin.
    """
    state = session.state
    if not state.running or not state.port:
        return BrowserFindings(
            available=False,
            reason="No preview is running, so there was nothing to open.",
            route=route,
        )

    probe = availability()
    safe_route = route if route.startswith("/") else f"/{route}"
    url = f"http://127.0.0.1:{state.port}{safe_route}"
    if not probe.available:
        return BrowserFindings(available=False, reason=probe.reason,
                               url=url, route=safe_route)

    findings = BrowserFindings(available=True, url=url, route=safe_route)
    started = time.monotonic()
    console_errors: List[str] = []
    failed: List[str] = []
    page_errors: List[str] = []

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                context = browser.new_context(viewport={"width": OVERFLOW_WIDTHS[-1],
                                                        "height": 900})
                page = context.new_page()
                page.set_default_timeout(PAGE_TIMEOUT_MS)

                page.on("console", lambda m: (
                    console_errors.append(f"{m.type}: {m.text}"[:400])
                    if m.type in ("error",) else None
                ))
                page.on("pageerror", lambda e: page_errors.append(str(e)[:400]))
                page.on("requestfailed", lambda r: failed.append(
                    f"{r.method} {_strip_origin(r.url, state.port)} — "
                    f"{(r.failure or 'failed')}"[:400]
                ))
                page.on("response", lambda r: (
                    failed.append(f"HTTP {r.status} {_strip_origin(r.url, state.port)}")
                    if r.status >= 400 else None
                ))

                response = page.goto(url, wait_until="domcontentloaded")
                findings.http_status = response.status if response else None
                page.wait_for_timeout(SETTLE_MS)

                findings.title = (page.title() or "")[:200]
                findings.h1_count = page.locator("h1").count()

                overflow: Dict[str, object] = {}
                for width in OVERFLOW_WIDTHS:
                    page.set_viewport_size({"width": width, "height": 900})
                    page.wait_for_timeout(250)
                    measured = page.evaluate(
                        "() => ({scroll: document.documentElement.scrollWidth,"
                        " client: document.documentElement.clientWidth})"
                    )
                    # One pixel of slack: sub-pixel layout rounding makes an
                    # exact comparison report overflow on pages that have none.
                    overflows = measured["scroll"] > measured["client"] + 1
                    overflow[width] = {
                        "overflows": overflows,
                        "scroll_width": measured["scroll"],
                        "client_width": measured["client"],
                    }
                findings.horizontal_overflow = overflow

                if capture_screenshot:
                    page.set_viewport_size({"width": OVERFLOW_WIDTHS[-1], "height": 900})
                    page.wait_for_timeout(200)
                    target = screenshot_dir()
                    if target is not None:
                        name = screenshot_name(task_id or "task", safe_route)
                        page.screenshot(path=str(target / name), full_page=True)
                        findings.screenshot = name
                        _prune_screenshots()
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 — a QA check must not end a task
        logger.warning("Browser check failed.", exc_info=True)
        return BrowserFindings(
            available=False,
            reason=f"The browser check could not complete ({type(exc).__name__}).",
            url=url, route=safe_route,
        )

    findings.console_errors = len(console_errors)
    findings.console_messages = console_errors[:20]
    findings.failed_requests = len(failed)
    findings.failed_request_urls = failed[:20]
    findings.page_errors = page_errors[:20]
    findings.duration_seconds = time.monotonic() - started
    return findings


def _strip_origin(url: str, port: Optional[int]) -> str:
    """Report `/assets/main.js`, not `http://127.0.0.1:5180/assets/main.js`.

    Shorter to read, and it keeps the port — which is incidental and
    changes every run — out of a record the user may share."""
    prefix = f"http://127.0.0.1:{port}"
    return url[len(prefix):] if port and url.startswith(prefix) else url
