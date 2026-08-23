"""The preview and the browser checks.

Two claims are worth more than the rest:

* "Running" is verified, never remembered. A dev server that crashed two
  seconds ago must report stopped.
* A check that did not run reports that it did not run. Zero console
  errors and no console check look identical on screen unless the code
  keeps them apart, and the version this replaces wrote `0` after
  looking at nothing.
"""

import socket
import sys
import time
from pathlib import Path

import pytest

from app.coding import browser_qa, limits, preview
from app.coding.runner import CommandHandle, build_environment, ledger
from tests import coding_fixtures as fx


DEFECTIVE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Corner Shop</title></head>
<body>
  <h1>Corner Shop</h1>
  <h1>A second h1 that should not be here</h1>
  <img src="/missing-banner.png">
  <div style="width:1900px;background:#eee">Wider than a phone.</div>
  <script src="/does-not-exist.js"></script>
  <script>console.error("checkout total is NaN"); undefinedFunction();</script>
</body></html>
"""


@pytest.fixture(autouse=True)
def screenshots_go_to_a_scratch_directory(tmp_path, monkeypatch):
    """Keep browser-check output out of the repository.

    `screenshot_dir()` resolves through `app_paths.data_dir()`, which in a
    source checkout is ./data — so a test run wrote real PNGs into the
    working tree, and sixteen of them were committed before anyone
    noticed. Redirecting it here means the test cannot do that again even
    if the ignore rules are lost.
    """
    from app.core import app_paths

    scratch = tmp_path / "appdata"
    scratch.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app_paths, "data_dir", lambda: scratch)
    return scratch


@pytest.fixture
def served(tmp_path):
    """A real preview session serving a real page, stopped afterwards."""
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(DEFECTIVE_PAGE, encoding="utf-8")

    port = preview.find_free_port()
    assert port is not None
    session = preview.PreviewSession()
    handle = CommandHandle(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        root, root.name)
    handle.start(build_environment())
    ledger.track(handle)
    session._handle = handle
    session._state = preview.PreviewState(
        running=True, port=port, url=f"http://127.0.0.1:{port}/",
        pid=handle.pid, script="serve", started_at=time.time())

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not preview.port_in_use(port):
        time.sleep(0.1)
    assert session.state.running, "the fixture server never came up"

    yield session, root, port

    session.stop("test finished")
    ledger.forget(handle)


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

def test_a_free_port_is_found_by_binding_it():
    port = preview.find_free_port()
    assert port is not None
    assert limits.PREVIEW_PORT_RANGE[0] <= port <= limits.PREVIEW_PORT_RANGE[1]


def test_an_occupied_port_is_never_adopted_and_never_killed():
    """Something else is listening. It is not ours to use *or* to stop."""
    port = preview.find_free_port()
    listener, stop_flag, thread = fx.occupied_port_server(port)
    try:
        assert preview.port_in_use(port) is True
        chosen = preview.find_free_port()
        assert chosen != port, "JARVIS picked a port somebody else was already using"
        # And the intruder is still alive and still listening.
        assert preview.port_in_use(port) is True
    finally:
        stop_flag.set()
        listener.close()
        thread.join(timeout=2)


def test_the_whole_range_being_occupied_is_reported_not_forced(monkeypatch):
    monkeypatch.setattr(preview, "find_free_port", lambda *a, **k: None)
    session = preview.PreviewSession()
    state = session.start(Path("."), ["echo"], "dev")
    assert state.running is False
    assert "port" in state.last_error.lower()


# ---------------------------------------------------------------------------
# Truthful state
# ---------------------------------------------------------------------------

def test_a_running_preview_reports_running_and_its_loopback_url(served):
    session, root, port = served
    state = session.state
    assert state.running is True
    assert state.url == f"http://127.0.0.1:{port}/"
    assert state.bound_to if hasattr(state, "bound_to") else True
    assert state.as_dict()["bound_to"] == "127.0.0.1"


def test_a_crashed_preview_reports_stopped_rather_than_a_stale_flag(served):
    session, root, port = served
    session._handle.stop("simulated crash")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and session.state.running:
        time.sleep(0.2)
    assert session.state.running is False, "a dead preview still reported running"
    assert session.state.last_error


def test_stopping_a_preview_frees_the_port(served):
    session, root, port = served
    session.stop("test")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and preview.port_in_use(port):
        time.sleep(0.2)
    assert preview.port_in_use(port) is False, "the preview process outlived stop()"


def test_a_preview_will_not_start_a_second_time(served):
    session, root, port = served
    state = session.start(root, [sys.executable, "-m", "http.server"], "dev")
    assert "already running" in (state.last_error or "").lower()


# ---------------------------------------------------------------------------
# HTTP and structure checks
# ---------------------------------------------------------------------------

def test_the_http_probe_reports_the_status_and_a_body_sample(served):
    session, root, port = served
    probe = session.http_probe("/")
    assert probe["ok"] is True
    assert probe["status"] == 200
    assert "Corner Shop" in probe["body_sample"]


def test_a_missing_route_reports_the_status_rather_than_succeeding(served):
    session, root, port = served
    probe = session.http_probe("/nothing-here")
    assert probe["ok"] is False
    assert probe.get("status") == 404


def test_the_structure_check_finds_the_planted_defects():
    checks = preview.basic_page_checks(DEFECTIVE_PAGE)
    assert checks["h1_count"] == 2
    assert checks["exactly_one_h1"] is False
    assert checks["images_missing_alt"] == 1
    assert checks["has_title"] is True
    assert checks["has_lang"] is True


# ---------------------------------------------------------------------------
# Browser checks
# ---------------------------------------------------------------------------

def test_availability_is_reported_with_a_reason_when_it_is_false(monkeypatch, tmp_path):
    """Two different absences, two different reasons.

    Which one applies depends on the machine: a Windows CI runner has no
    Playwright package at all, a Linux dev box has the package but may
    have no browser. Both are legitimate, and both must name the thing
    that is missing and what would provide it — that is the property
    under test, not which of the two it happens to be.
    """
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "nothing-here"))
    result = browser_qa.availability()
    assert result.available is False
    reason = result.reason.lower()
    assert "playwright" in reason, reason
    assert "chromium" in reason or "does not include" in reason, reason


def test_an_unavailable_browser_reports_not_checked_never_zero(monkeypatch, tmp_path):
    """The defect this replaces wrote `console_errors = 0` after looking
    at nothing. On screen, that is a clean page."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "nothing-here"))

    class FakeSession:
        class _S:
            running = True
            port = 9999
        state = _S()

    findings = browser_qa.run_checks(FakeSession(), "/", task_id="t")
    assert findings.available is False
    assert findings.console_errors is None
    assert findings.failed_requests is None
    assert "not run" in findings.summary().lower()


def test_a_fresh_preview_state_has_not_been_browser_checked():
    state = preview.PreviewState()
    assert state.browser_checked is False
    assert state.console_errors is None
    assert state.failed_requests is None


def test_no_preview_means_no_check_rather_than_an_empty_pass():
    session = preview.PreviewSession()
    findings = browser_qa.run_checks(session, "/", task_id="t")
    assert findings.available is False
    assert findings.console_errors is None


@pytest.mark.browser
def test_a_real_browser_finds_console_errors_failed_requests_and_overflow(served):
    if not browser_qa.availability().available:
        pytest.skip("no Chromium available for a real browser check")

    session, root, port = served
    findings = browser_qa.run_checks(session, "/", task_id="previewtest")

    assert findings.available is True
    assert findings.http_status == 200
    assert findings.h1_count == 2
    assert findings.console_errors and findings.console_errors >= 1, (
        "the page calls console.error and loads two missing resources"
    )
    assert any("NaN" in message for message in findings.console_messages)
    assert findings.page_errors, "an uncaught ReferenceError was not reported"
    assert findings.failed_requests and findings.failed_requests >= 1
    overflow = findings.horizontal_overflow or {}
    assert overflow[320]["overflows"] is True, "a 1900px block did not register at 320px"


@pytest.mark.browser
def test_a_screenshot_is_written_and_names_neither_the_user_nor_a_path(served):
    if not browser_qa.availability().available:
        pytest.skip("no Chromium available for a real browser check")

    import getpass

    session, root, port = served
    findings = browser_qa.run_checks(session, "/", task_id="shottest")
    assert findings.screenshot

    directory = browser_qa.screenshot_dir()
    assert (directory / findings.screenshot).is_file()

    name = findings.screenshot
    assert getpass.getuser() not in name
    assert "/" not in name and "\\" not in name
    assert str(root) not in name
    assert root.name not in name


def test_a_screenshot_name_is_stable_in_shape_and_carries_no_path():
    name = browser_qa.screenshot_name("abc123def", "/products?id=1&user=someone")
    assert name.endswith(".png")
    assert "someone" not in name
    assert "products" not in name
    assert "/" not in name


# ---------------------------------------------------------------------------
# Loopback only
# ---------------------------------------------------------------------------

def test_the_bind_address_is_one_constant_and_it_is_loopback():
    """Not a grep for the bind-all literal — the repository-wide
    test_nothing_binds_to_all_interfaces already does that, and does it
    better, because it excludes docstrings (this module's docstring
    explains *why* a dev server defaulting to all interfaces is the
    problem, and a cruder grep flags that explanation).

    What is checked here instead is that there is exactly one bind
    address in the module and it is loopback, so a second one cannot be
    introduced quietly alongside it."""
    assert preview.LOOPBACK == "127.0.0.1"
    assert preview.PreviewState().as_dict()["bound_to"] == "127.0.0.1"

    source = (Path(__file__).resolve().parent.parent /
              "app" / "coding" / "preview.py").read_text(encoding="utf-8")
    import ast
    literals = {
        node.value for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value.count(".") == 3 and node.value.replace(".", "").isdigit()
    }
    assert literals == {"127.0.0.1"}, f"more than one IP literal in preview.py: {literals}"


def test_the_dev_server_is_pinned_to_loopback_on_the_command_line(monkeypatch, tmp_path):
    """Vite defaults to loopback but `--host` publishes it. Both the
    environment and the argv are pinned, because different servers honour
    different ones."""
    captured = {}

    class FakeHandle:
        def __init__(self, argv, cwd, display):
            captured["argv"] = list(argv)
            self._process = None
            self.pid = None

        def start(self, env):
            captured["env"] = dict(env)
            raise OSError("not really starting anything")

    monkeypatch.setattr(preview, "CommandHandle", FakeHandle)
    session = preview.PreviewSession()
    session.start(tmp_path, ["npm", "run", "dev"], "dev")

    assert "--host" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--host") + 1] == "127.0.0.1"
    assert captured["env"]["HOST"] == "127.0.0.1"
    assert captured["env"]["BROWSER"] == "none"


def test_starting_a_preview_needs_a_script_the_project_declares(tmp_path):
    """JARVIS will not guess a dev-server command."""
    from app.coding import agent, tasks

    record = tasks.TaskRecord(id="t", project_id="p", request="r", created_at=0.0)
    context = agent.TaskContext(task_id="t", project_id="p", root=tmp_path,
                                project_root=tmp_path, record=record,
                                declared_commands={})

    class Proposal:
        action = "start_preview"
        script = "dev"

    outcome = preview.handle(context, Proposal())
    assert outcome.ok is False
    assert "does not declare" in outcome.summary


@pytest.mark.browser
def test_a_browser_check_works_from_a_thread_that_has_an_event_loop(served):
    """Playwright's sync API refuses to run where an asyncio loop is
    live, and reports it as a bare `Error`. A caller inside a request
    handler is a reasonable thing to be, so the work moves to its own
    thread rather than failing with a message nobody can act on.

    The loop is created on a thread of this test's own rather than with
    `asyncio.run()`: by the time the whole browser suite reaches here,
    another suite may already have left a loop running on the main
    thread, and `asyncio.run()` refuses to nest. Owning the thread makes
    the condition under test independent of what ran before it.
    """
    import asyncio
    import threading

    if not browser_qa.availability().available:
        pytest.skip("no Chromium available for a real browser check")

    session, root, port = served
    outcome = {}

    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def inside_the_loop():
            # The loop really is running at this point, which is the
            # whole condition being tested.
            assert asyncio.get_running_loop() is loop
            return browser_qa.run_checks(session, "/", task_id="loop",
                                         capture_screenshot=False)

        try:
            outcome["findings"] = loop.run_until_complete(inside_the_loop())
        except Exception as exc:  # noqa: BLE001 — reported through outcome
            outcome["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=worker, name="loop-check")
    thread.start()
    thread.join(timeout=120)
    assert not thread.is_alive(), "the browser check never returned"
    assert "error" not in outcome, f"run_checks raised: {outcome.get('error')}"

    findings = outcome["findings"]
    assert findings.available is True, findings.reason
    assert findings.http_status == 200


def test_a_browser_failure_reason_names_the_problem_not_just_the_type(monkeypatch):
    """`type(exc).__name__` is `Error` for nearly every Playwright
    failure, so the message it produced — "could not complete (Error)" —
    told the user nothing they could act on. Playwright writes its own
    first line, and it names the real problem."""
    class FakeSession:
        class _S:
            running = True
            port = 4321
        state = _S()

    monkeypatch.setattr(browser_qa, "availability",
                        lambda: browser_qa.BrowserAvailability(True))

    class PlaywrightShapedError(Exception):
        pass

    def explode():
        raise PlaywrightShapedError(
            "Executable doesn't exist at /nowhere/chrome\nTry playwright install")

    class FakeModule:
        @staticmethod
        def sync_playwright():
            explode()

    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", FakeModule)

    findings = browser_qa.run_checks(FakeSession(), "/", task_id="t")
    assert findings.available is False
    assert "Executable doesn't exist" in findings.reason
    assert "\n" not in findings.reason, "only the first line may be shown"
    assert len(findings.reason) < 300


def test_a_port_another_process_is_listening_on_is_never_chosen():
    """SO_REUSEADDR means different things on POSIX and Windows.

    On POSIX it permits rebinding a port in TIME_WAIT. On Windows it
    permits binding a port another socket is *actively listening on* —
    the bind succeeds and the two sockets compete for connections. This
    function exists to answer "is anybody using this port", so setting an
    option whose whole effect is to make that answer wrong defeated it.

    The Windows CI job caught it: with a listener on 5180,
    find_free_port() returned 5180.
    """
    import ast
    import inspect
    import textwrap

    # The AST, not a grep: the function's docstring explains *why* the
    # option is not set, and a substring search cannot tell an
    # explanation from a call.
    tree = ast.parse(textwrap.dedent(inspect.getsource(preview.find_free_port)))
    sockopts = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setsockopt"
    ]
    assert sockopts == [], (
        "find_free_port must not call setsockopt — SO_REUSEADDR on Windows "
        "allows binding a port somebody else is actively listening on, which "
        "is exactly the question this function exists to answer"
    )

    first = preview.find_free_port()
    listener, stop_flag, thread = fx.occupied_port_server(first)
    try:
        for _ in range(3):
            chosen = preview.find_free_port()
            assert chosen != first, "a port with a live listener was chosen"
        assert preview.port_in_use(first) is True, "the other process was disturbed"
    finally:
        stop_flag.set()
        listener.close()
        thread.join(timeout=2)
