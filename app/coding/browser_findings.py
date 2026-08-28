"""What a browser check produced, and which of seven things happened.

**"Not available" is not a result.** The version of this feature that
shipped before had exactly two outcomes — findings, or `available:
false` with a sentence — so a page that timed out, a page whose
navigation was refused by the security boundary, a machine with no
browser and a check the user cancelled all rendered identically. Those
are four different situations with four different next steps, and
collapsing them told the user nothing.

`QaState` is the seven the brief names. Every one of them carries the
reason and, where there is one, the step that fixes it.

**`None` means not checked; `0` means checked and clean.** This is the
oldest rule in this subsystem and the reason it exists: an earlier build
wrote `console_errors = 0` after an HTML-only check, which is
indistinguishable on screen from a page that genuinely has none. Every
count here starts at `None` and is only ever set by something that
actually looked.

**A screenshot's filename carries no identity.** Task id and eight hex
characters of the route — never the project path, the project name or
the account name. A user pasting an export into a bug report should not
be publishing their directory layout with it.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from app.coding.browser_probe import ACCESSIBILITY_RULES
from app.logging_config import get_logger

logger = get_logger("coding.browser_findings")

SCREENSHOT_DIRNAME = "coding_screenshots"
MAX_SCREENSHOTS_KEPT = 40
MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024


class QaState(str, Enum):
    """The seven distinguishable outcomes of a browser check."""

    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    TIMED_OUT = "timed_out"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    PREVIEW_UNAVAILABLE = "preview_unavailable"
    CANCELLED = "cancelled"

    @property
    def headline(self) -> str:
        return {
            QaState.PASSED: "Passed",
            QaState.FAILED: "Failed",
            QaState.BLOCKED: "Blocked by security policy",
            QaState.TIMED_OUT: "Timed out",
            QaState.ENGINE_UNAVAILABLE: "Browser engine unavailable",
            QaState.PREVIEW_UNAVAILABLE: "Preview unavailable",
            QaState.CANCELLED: "Cancelled",
        }[self]


@dataclass
class BrowserFindings:
    """Everything one check saw, plus which state it ended in."""

    state: QaState = QaState.ENGINE_UNAVAILABLE
    reason: str = ""
    fix: str = ""

    #: Did a real browser load the page and return probe results?
    #:
    #: Deliberately a fact recorded by the code that did it, not something
    #: derived from `state`. `FAILED` covers both "the page has defects"
    #: and "the check itself fell over", and deriving "a browser opened
    #: this" from the state alone claimed the first when it was the
    #: second — which is exactly the class of lie this module exists to
    #: prevent, just one level up.
    opened: bool = False

    # What was checked, and with what.
    route: str = "/"
    url: str = ""
    engine: str = ""
    engine_version: str = ""

    # What the page did. None throughout means nothing looked.
    http_status: Optional[int] = None
    title: str = ""
    lang: str = ""
    h1_count: Optional[int] = None
    console_errors: Optional[int] = None
    console_messages: List[str] = field(default_factory=list)
    page_errors: Optional[int] = None
    page_error_messages: List[str] = field(default_factory=list)
    failed_requests: Optional[int] = None
    failed_request_details: List[str] = field(default_factory=list)
    http_error_details: List[str] = field(default_factory=list)
    broken_images: Optional[int] = None
    broken_image_details: List[str] = field(default_factory=list)
    accessibility_findings: Optional[int] = None
    accessibility_details: List[dict] = field(default_factory=list)
    horizontal_overflow: Optional[Dict[str, dict]] = None
    reduced_motion: Optional[dict] = None

    # What the security boundary did while the page ran.
    blocked_origins: List[str] = field(default_factory=list)
    downloads_blocked: int = 0
    dialogs_dismissed: int = 0

    # Housekeeping.
    screenshot: str = ""
    duration_seconds: float = 0.0
    checked_at: float = field(default_factory=time.time)
    truncated: bool = False
    scanned_all: bool = True
    cleanup: Optional[dict] = None

    # -- derived ----------------------------------------------------------

    @property
    def available(self) -> bool:
        """Kept for the callers and stored records that predate `state`.

        True only when a real browser opened the page — never a synonym
        for "the module is installed".
        """
        return self.opened

    def problem_count(self) -> Optional[int]:
        """How many things are wrong, or None if nothing looked."""
        if not self.opened:
            return None
        counts = (self.console_errors, self.page_errors, self.failed_requests,
                  self.broken_images, self.accessibility_findings)
        total = sum(c for c in counts if c is not None)
        total += sum(
            1 for v in (self.horizontal_overflow or {}).values()
            if isinstance(v, dict) and v.get("overflows")
        )
        return total

    def as_dict(self) -> dict:
        return {
            "state": self.state.value,
            "headline": self.state.headline,
            "opened": self.opened,
            "available": self.available,
            "reason": self.reason,
            "fix": self.fix,
            "route": self.route,
            "url": self.url,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "http_status": self.http_status,
            "title": self.title,
            "lang": self.lang,
            "h1_count": self.h1_count,
            "console_errors": self.console_errors,
            "console_messages": self.console_messages,
            "page_errors": self.page_errors,
            "page_error_messages": self.page_error_messages,
            "failed_requests": self.failed_requests,
            "failed_request_details": self.failed_request_details,
            "http_error_details": self.http_error_details,
            "broken_images": self.broken_images,
            "broken_image_details": self.broken_image_details,
            "accessibility_findings": self.accessibility_findings,
            "accessibility_details": self.accessibility_details,
            "accessibility_rules": [
                {"rule": rule, "description": text} for rule, text in ACCESSIBILITY_RULES
            ],
            "horizontal_overflow": self.horizontal_overflow,
            "reduced_motion": self.reduced_motion,
            "blocked_origins": self.blocked_origins,
            "downloads_blocked": self.downloads_blocked,
            "dialogs_dismissed": self.dialogs_dismissed,
            "screenshot": self.screenshot,
            "duration_seconds": round(self.duration_seconds, 2),
            "checked_at": self.checked_at,
            "truncated": self.truncated,
            "scanned_all": self.scanned_all,
            "problem_count": self.problem_count(),
            "cleanup": self.cleanup,
        }

    def summary(self) -> str:
        """One line for a task record. Never implies a check that did not run."""
        if not self.opened:
            return f"Browser check — {self.state.headline}: {self.reason}"
        parts = [
            f"HTTP {self.http_status}" if self.http_status is not None else "loaded",
            f"{self.console_errors} console error(s)",
            f"{self.page_errors} page error(s)",
            f"{self.failed_requests} failed request(s)",
            f"{self.broken_images} broken image(s)",
            f"{self.accessibility_findings} accessibility finding(s)",
        ]
        overflowing = [
            width for width, value in (self.horizontal_overflow or {}).items()
            if isinstance(value, dict) and value.get("overflows")
        ]
        parts.append(
            "horizontal overflow at " + ", ".join(f"{w}px" for w in overflowing)
            if overflowing else "no horizontal overflow"
        )
        if self.blocked_origins:
            parts.append(f"{len(self.blocked_origins)} request(s) blocked from leaving loopback")
        return f"Browser check {self.state.headline.lower()} — " + "; ".join(parts) + "."


def unavailable(state: QaState, reason: str, *, route: str = "/", url: str = "",
                fix: str = "") -> BrowserFindings:
    """A findings object for a check that did not run.

    Every count stays `None`. There is deliberately no argument here that
    could set one.
    """
    return BrowserFindings(state=state, reason=reason, fix=fix, route=route, url=url)


# -- screenshots ----------------------------------------------------------

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
    digest = hashlib.sha256(route.encode("utf-8", errors="replace")).hexdigest()[:8]
    safe_task = "".join(c for c in task_id if c.isalnum())[:16] or "task"
    return f"{safe_task}-{digest}-{int(time.time())}.png"


def prune_screenshots() -> None:
    target = screenshot_dir()
    if target is None:
        return
    try:
        shots = sorted(target.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for stale in shots[:-MAX_SCREENSHOTS_KEPT]:
            stale.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not prune old screenshots.", exc_info=True)
