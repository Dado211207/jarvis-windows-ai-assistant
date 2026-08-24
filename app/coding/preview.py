"""The local preview, and checks against it.

Three rules, each of which exists because the obvious implementation
breaks one of them:

**Loopback only.** The preview binds `127.0.0.1`. A dev server that
defaults to `0.0.0.0` — Vite does, with `--host` — puts the user's
work-in-progress on every network they are attached to. The port is
bound by JARVIS first to prove it is free, and the flags passed to the
dev server pin the host explicitly.

**Never adopt a process JARVIS did not start.** If something is already
answering on the port, that is somebody else's server. JARVIS does not
use it (it would report a preview it does not own and cannot stop) and
does not kill it (it is not ours). It picks another port, or says so.

**Truthful state.** `is_running()` is not a flag. It is: the owned
process is alive *and* the loopback endpoint answers. A dev server that
crashed two seconds ago reports stopped, because a UI that says
"running" over a dead process is worse than one that says nothing.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from app.coding import limits, tasks
from app.coding.runner import CommandHandle, _identity_for_pid, build_environment, ledger
from app.logging_config import get_logger

logger = get_logger("coding.preview")

LOOPBACK = "127.0.0.1"


def find_free_port(start: int = limits.PREVIEW_PORT_RANGE[0],
                   end: int = limits.PREVIEW_PORT_RANGE[1]) -> Optional[int]:
    """A port nothing is listening on, proved by binding it.

    Binding and closing is the only reliable test; asking whether a
    connection succeeds has a race in it and also cannot distinguish
    "free" from "refused for another reason".

    **`SO_REUSEADDR` is deliberately not set, and that is a Windows
    correctness point rather than a style one.** On POSIX the option
    permits rebinding a port in TIME_WAIT. On Windows it permits binding
    a port another socket is *actively listening on* — the bind succeeds
    and the two sockets fight over incoming connections. This function
    exists to answer "is anybody using this port", so an option whose
    entire effect is to make that question return yes when the answer is
    no defeats it.

    The Windows CI job caught this: with a listener on 5180,
    `find_free_port()` returned 5180. JARVIS would then have started a
    preview on a port somebody else owned, reported it as its own, and
    been unable to explain why the page showed a stranger's application.
    """
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((LOOPBACK, port))
                # Listening as well as binding: on some platforms a bind
                # can succeed for a port that cannot accept connections.
                probe.listen(1)
                return port
            except OSError:
                continue
    return None


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((LOOPBACK, port)) == 0


@dataclass
class PreviewState:
    running: bool = False
    port: Optional[int] = None
    url: Optional[str] = None
    pid: Optional[int] = None
    script: str = ""
    started_at: Optional[float] = None
    http_status: Optional[int] = None
    last_error: str = ""
    # None means "no browser check has run", which is not the same thing
    # as zero. The Preview panel renders the two differently.
    console_errors: Optional[int] = None
    failed_requests: Optional[int] = None
    browser_checked: bool = False
    # Which of the seven browser-QA outcomes the last check reached. Empty
    # until one has run — `browser_checked` alone could not distinguish a
    # machine with no engine from a check that was cancelled.
    browser_state: str = ""
    browser_reason: str = ""
    #: The whole of the last check, for the Preview panel to render. The
    #: scalar fields above stay because task records written before this
    #: existed carry them, and a stored record must not become unreadable.
    browser: Optional[Dict[str, object]] = None
    screenshot: str = ""

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "port": self.port,
            "url": self.url,
            "pid": self.pid,
            "script": self.script,
            "started_at": self.started_at,
            "http_status": self.http_status,
            "last_error": self.last_error,
            "console_errors": self.console_errors,
            "failed_requests": self.failed_requests,
            "browser_checked": self.browser_checked,
            "browser_state": self.browser_state,
            "browser_reason": self.browser_reason,
            "browser": self.browser,
            "screenshot": self.screenshot,
            "bound_to": LOOPBACK,
        }


class PreviewSession:
    """One owned dev server. At most one per task."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handle: Optional[CommandHandle] = None
        self._state = PreviewState()

    @property
    def state(self) -> PreviewState:
        self._refresh()
        return self._state

    def _refresh(self) -> None:
        """Recompute `running` from reality, never from a stored flag."""
        with self._lock:
            handle = self._handle
            if handle is None or handle._process is None:
                self._state.running = False
                return
            alive = handle._process.poll() is None
            if not alive:
                self._state.running = False
                self._state.last_error = self._state.last_error or "The preview process exited."
                return
            port = self._state.port
            self._state.running = bool(port and port_in_use(port))
            if not self._state.running and not self._state.last_error:
                self._state.last_error = "The preview process is running but not answering yet."

    def verify_ownership(self) -> tuple:
        """Prove this session's process is still the one JARVIS started.

        Returns `(ok, reason)`. Browser QA calls this immediately before it
        navigates, because "the port answers" is not the same claim as
        "the process we started is answering". Windows recycles PIDs, and
        a preview that died and left its port to something else would
        otherwise be checked as though it were ours — JARVIS would open a
        stranger's application, screenshot it, and file the findings
        against the user's project.

        The comparison is a `ProcessIdentity` — PID *plus* creation time —
        the same rule `app/launcher/process_tree.py` applies to anything it
        signals.
        """
        with self._lock:
            handle = self._handle
            port = self._state.port
        if handle is None or handle._process is None:
            return False, "No preview process belongs to this task."
        if handle._process.poll() is not None:
            return False, "The preview process has exited."
        if not port:
            return False, "The preview has no port."

        expected = handle._own
        if expected is None or not expected.is_verifiable:
            # psutil absent, or the process was not inspectable when it
            # started. Unverifiable is not the same as wrong: the Popen
            # object is still a live handle to the child we launched, so
            # ownership holds even though the stronger check cannot run.
            return True, "identity not verifiable on this machine"

        current = _identity_for_pid(handle._process.pid)
        if current is None:
            return False, "The preview process could not be inspected."
        if not expected.matches(current.create_time):
            return False, (
                "The process on the preview's PID is not the one JARVIS started "
                "(the PID has been reused)."
            )
        return True, ""

    def start(self, root: Path, argv: List[str], script: str) -> PreviewState:
        if self.state.running:
            self._state.last_error = "A preview is already running for this task."
            return self._state

        port = find_free_port()
        if port is None:
            self._state = PreviewState(
                last_error="No free loopback port was available in JARVIS's preview range."
            )
            return self._state

        env = build_environment()
        env["PORT"] = str(port)
        env["HOST"] = LOOPBACK
        env["BROWSER"] = "none"          # stop CRA/Vite opening a real browser

        # Pin host and port on the command line as well as the environment:
        # different dev servers honour different ones, and a server that
        # ignored both would fail to come up rather than bind wide.
        full_argv = list(argv) + ["--host", LOOPBACK, "--port", str(port), "--strictPort"]

        handle = CommandHandle(full_argv, root, root.name)
        try:
            handle.start(env)
        except PermissionError as exc:
            self._state = PreviewState(last_error=str(exc))
            return self._state
        except FileNotFoundError:
            self._state = PreviewState(last_error=(
                f"'{full_argv[0]}' is not installed, or is not on PATH, so the "
                "preview could not start."
            ))
            return self._state
        except OSError as exc:
            self._state = PreviewState(last_error=f"The preview could not start ({type(exc).__name__}).")
            return self._state

        ledger.track(handle)
        with self._lock:
            self._handle = handle
            self._state = PreviewState(
                running=False, port=port, url=f"http://{LOOPBACK}:{port}/",
                pid=handle.pid, script=script, started_at=time.time(),
            )

        deadline = time.monotonic() + limits.PREVIEW_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if handle._process is not None and handle._process.poll() is not None:
                self._state.last_error = "The preview process exited before it started serving."
                return self._state
            if port_in_use(port):
                self._state.running = True
                self._state.last_error = ""
                logger.info("Coding preview started on loopback port %d.", port)
                return self._state
            time.sleep(limits.PREVIEW_POLL_INTERVAL_SECONDS)

        self._state.last_error = (
            f"The preview did not answer within {limits.PREVIEW_READY_TIMEOUT_SECONDS:.0f}s."
        )
        return self._state

    def stop(self, reason: str = "stopped") -> dict:
        with self._lock:
            handle = self._handle
            self._handle = None
        if handle is None:
            self._state = PreviewState()
            return {"stopped": False, "reason": "nothing was running"}
        report = handle.stop(reason)
        ledger.forget(handle)
        self._state = PreviewState(last_error="")
        return {"stopped": True, "cleanup": report}

    def http_probe(self, route: str = "/") -> Dict[str, object]:
        state = self.state
        if not state.running or not state.url:
            return {"ok": False, "error": "No preview is running."}
        safe_route = route if route.startswith("/") else f"/{route}"
        url = f"http://{LOOPBACK}:{state.port}{safe_route}"
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 — loopback, fixed scheme
                body = response.read(200_000)
                self._state.http_status = response.status
                return {
                    "ok": True, "status": response.status, "url": url,
                    "bytes": len(body),
                    "content_type": response.headers.get("Content-Type", ""),
                    "body_sample": body[:20_000].decode("utf-8", errors="replace"),
                }
        except urllib.error.HTTPError as exc:
            self._state.http_status = exc.code
            return {"ok": False, "status": exc.code, "url": url, "error": f"HTTP {exc.code}"}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"ok": False, "url": url, "error": f"The preview did not answer ({type(exc).__name__})."}


def basic_page_checks(html: str) -> Dict[str, object]:
    """Checks that need no browser: structure a coding agent should catch
    before it claims a page is fine.

    Deliberately string-level rather than a parser: adding an HTML parser
    dependency for four checks is not a trade this pass makes, and the
    checks are reported as indicative, never as an accessibility audit.
    """
    import re

    lowered = html.lower()
    h1_count = len(re.findall(r"<h1[\s>]", lowered))
    title_match = re.search(r"<title[^>]*>(.*?)</title>", lowered, re.DOTALL)
    lang_match = re.search(r"<html[^>]*\blang\s*=\s*[\"']([^\"']+)", lowered)
    images = re.findall(r"<img\b[^>]*>", lowered)
    missing_alt = [i for i in images if "alt=" not in i]
    return {
        "h1_count": h1_count,
        "exactly_one_h1": h1_count == 1,
        "has_title": bool(title_match and title_match.group(1).strip()),
        "title": (title_match.group(1).strip()[:120] if title_match else ""),
        "has_lang": bool(lang_match),
        "lang": (lang_match.group(1) if lang_match else ""),
        "image_count": len(images),
        "images_missing_alt": len(missing_alt),
    }


def handle(context, proposal):
    """Dispatch for the three preview-related proposals.

    Imported lazily by `agent.py` so a task that never previews anything
    never touches this module.
    """
    from app.coding.agent import StepOutcome

    action = proposal.action
    session: PreviewSession = getattr(context, "preview", None) or PreviewSession()
    context.preview = session

    if action == "stop_preview":
        report = session.stop("stopped by the task")
        tasks.append_step(context.record, "preview", "Preview stopped.", report)
        return StepOutcome(True, "Preview stopped.", "preview", report,
                           model_text="The preview is stopped.")

    if action == "start_preview":
        entry = context.declared_commands.get(proposal.script) or context.declared_commands.get("dev")
        if entry is None:
            message = (
                "This project does not declare a development-server script, so JARVIS "
                "has nothing safe to start. It will not guess a command."
            )
            tasks.append_step(context.record, "preview", message, {}, ok=False)
            return StepOutcome(False, message, "preview", model_text=f"REFUSED: {message}")
        state = session.start(context.root, list(entry["argv"]), proposal.script)
        summary = (
            f"Preview running at {state.url}" if state.running
            else f"Preview did not start: {state.last_error}"
        )
        tasks.append_step(context.record, "preview", summary, state.as_dict(), ok=state.running)
        return StepOutcome(state.running, summary, "preview", state.as_dict(),
                           model_text=summary)

    # browser_check
    probe = session.http_probe(proposal.route)
    if not probe.get("ok"):
        summary = f"Check of {proposal.route} failed: {probe.get('error')}"
        tasks.append_step(context.record, "browser", summary, {"route": proposal.route}, ok=False)
        return StepOutcome(False, summary, "browser", probe, model_text=summary)

    checks = basic_page_checks(str(probe.get("body_sample", "")))

    # The real browser, when this build has one. Its findings are merged
    # in as-is: `console_errors` stays None when nothing opened the page,
    # so the UI can say "not checked" instead of "0". Writing 0 here
    # (which this function used to do) reported a clean page for a check
    # that never ran.
    from app.coding import browser_qa

    findings = browser_qa.run_checks(
        session, proposal.route, task_id=context.task_id,
        cancel=getattr(context, "cancel", None),
    )
    session._state.console_errors = findings.console_errors
    session._state.failed_requests = findings.failed_requests
    session._state.browser_checked = findings.available
    session._state.browser_state = findings.state.value
    session._state.browser_reason = findings.reason
    session._state.browser = findings.as_dict()
    session._state.screenshot = findings.screenshot

    summary = (
        f"{proposal.route} returned HTTP {probe.get('status')}; "
        f"{checks['h1_count']} <h1>, "
        f"{checks['images_missing_alt']} image(s) without alt text. "
        + findings.summary()
    )
    detail = {
        "route": proposal.route,
        **checks,
        "status": probe.get("status"),
        "browser": findings.as_dict(),
    }
    tasks.append_step(context.record, "browser", summary, detail, ok=True)
    return StepOutcome(True, summary, "browser", detail, model_text=summary)
