"""Browser QA: the engine, the boundary, and the seven states.

Two things are being proved here, and they are different claims.

**That the check is real.** A browser opens the page, and the numbers come
from what it saw. The clean fixture reaches `PASSED` with zero problems
*and* an engine name — a state no amount of flag-flipping can reach,
because `QaState.PASSED` is only ever assigned after a navigation
succeeded and the probes returned. The defective fixture reports specific,
non-zero, correct counts.

**That the boundary holds against a page that attacks it.** Every page in
`coding_browser_fixtures` is something a repository could serve, and each
one is asserted to be blocked, bounded or safely reported — never merely
"nothing bad happened".

The browser tests skip when this machine has no Chromium. That is not a
loophole: `test_a_clean_page_passes_only_with_a_named_engine` fails if a
`PASSED` result ever arrives without one, so a skipped environment cannot
produce a false pass, and the packaged acceptance test asserts the
opposite direction — that the installed product does not skip.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from app.coding import (browser_engine, browser_origin, browser_probe,
                        browser_qa, preview)
from app.coding.browser_findings import BrowserFindings, QaState
from app.coding.runner import CommandHandle, build_environment, ledger
from tests import coding_browser_fixtures as hostile

ENGINE = browser_engine.find_engine()
needs_browser = pytest.mark.skipif(
    ENGINE is None,
    reason="no Chromium-based engine on this machine",
)


# ---------------------------------------------------------------------------
# The origin boundary — pure, fast, and the only place the decision is made
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,category", [
    ("https://example.com/landing", "foreign_host"),
    ("http://example.com/landing", "dns_name"),
    ("http://localhost:5180/", "loopback_alias"),
    ("http://[::1]:5180/", "loopback_alias"),
    ("http://127.0.0.2:5180/", "loopback_alias"),
    ("http://0.0.0.0:5180/", "loopback_alias"),
    ("http://192.168.1.10:5180/", "private_lan"),
    ("http://8.8.8.8/", "public_ip"),
    ("file:///etc/passwd", "file_scheme"),
    ("data:text/html,<h1>hi", "data_scheme"),
    ("javascript:alert(1)", "javascript_scheme"),
    ("vbscript:msgbox", "javascript_scheme"),
    ("chrome://settings", "browser_scheme"),
    ("ws://127.0.0.1:5180/", "websocket_scheme"),
    ("ftp://127.0.0.1/", "custom_scheme"),
    ("http://127.0.0.1:9999/", "wrong_port"),
])
def test_everything_that_is_not_the_owned_preview_is_refused(url, category):
    verdict = browser_origin.classify(url, 5180)
    assert verdict.allowed is False
    assert verdict.category == category


def test_the_owned_preview_on_its_own_port_is_allowed():
    assert browser_origin.classify("http://127.0.0.1:5180/about", 5180).allowed is True
    assert browser_origin.classify("about:blank", 5180).allowed is True


def test_localhost_is_refused_even_though_it_reaches_this_machine():
    """A name is resolved; an address is not. The refusal is deliberate."""
    verdict = browser_origin.classify("http://localhost:5180/", 5180)
    assert verdict.allowed is False
    assert "name or alias" in verdict.detail


@pytest.mark.parametrize("route", [
    "//example.com/x",              # protocol-relative: a host, not a path
    "http://example.com/x",
    "https://example.com/x",
    "javascript:alert(1)",
    "\\\\evil.example\\share",      # Chromium normalises backslashes to slashes
    "/ok\x00/nul",
    "/" + "a" * 600,
])
def test_a_route_that_is_not_a_path_is_refused_rather_than_repaired(route):
    assert browser_origin.safe_route(route) is None


@pytest.mark.parametrize("route,expected", [
    ("/", "/"), ("about", "/about"), ("/products?id=1", "/products?id=1"),
    ("", "/"),
])
def test_an_ordinary_route_is_accepted(route, expected):
    assert browser_origin.safe_route(route) == expected


def test_run_checks_refuses_a_url_shaped_route_without_starting_anything():
    class DeadSession:
        def verify_ownership(self):
            raise AssertionError("ownership must not be consulted for a bad route")

    findings = browser_qa.run_checks(DeadSession(), "https://example.com/")
    assert findings.state is QaState.BLOCKED
    assert findings.available is False


# ---------------------------------------------------------------------------
# The engine: bounded discovery, and a command line that enforces the boundary
# ---------------------------------------------------------------------------

def test_engine_discovery_never_walks_the_disk():
    """The same rule legacy_migration follows: look, do not search."""
    import ast

    source = Path(browser_engine.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "walk" not in called, "browser_engine must not scan directories"
    assert "rglob" not in called, "browser_engine must not scan directories"


def test_the_launch_command_line_pins_the_only_resolvable_host():
    engine = browser_engine.Engine("edge", "Microsoft Edge", "C:/edge.exe")
    argv = browser_engine.launch_argv(engine, debug_port=0,
                                      profile_dir="C:/profile",
                                      allow_host="127.0.0.1")
    joined = " ".join(argv)
    assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1" in joined
    assert "--remote-debugging-address=127.0.0.1" in joined
    assert "--user-data-dir=C:/profile" in joined
    for flag in ("--disable-extensions", "--disable-sync", "--password-store=basic",
                 "--deny-permission-prompts", "--block-new-web-contents",
                 "--disable-background-networking"):
        assert flag in argv, f"{flag} must be on the command line"


def test_no_engine_reports_the_cause_and_the_step_that_fixes_it(monkeypatch):
    monkeypatch.setattr(browser_engine, "find_engine", lambda: None)
    monkeypatch.setattr(browser_qa.browser_engine, "find_engine", lambda: None)
    probe = browser_qa.availability()
    assert probe.available is False
    assert probe.reason
    assert browser_engine.unavailable_fix()


def test_an_engine_path_is_never_published():
    """A full Windows path contains the account name."""
    engine = browser_engine.Engine("edge", "Microsoft Edge",
                                   r"C:\Users\someone\Edge\msedge.exe", "1.2.3")
    assert "someone" not in str(engine.as_dict())


# ---------------------------------------------------------------------------
# States: a count is never invented
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", list(QaState))
def test_every_state_has_its_own_words(state):
    assert state.headline
    assert state.headline != "Not available"


def test_opened_is_recorded_by_the_code_that_opened_it_not_derived():
    """A findings object never starts life claiming a browser ran.

    `FAILED` covers both "the page has defects" and "the check fell over
    before it launched anything", so `opened` cannot be inferred from the
    state — it is set at one point in `browser_qa`, after the probes have
    returned.
    """
    import ast

    assert BrowserFindings().opened is False
    assert BrowserFindings().available is False
    assert not hasattr(QaState.PASSED, "ran"), \
        "opened must not be derivable from the state again"

    source = Path(browser_qa.__file__).read_text(encoding="utf-8")
    assignments = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "opened"
                for t in node.targets)
    ]
    assert len(assignments) == 1, "exactly one place may record that a browser ran"


@pytest.mark.parametrize("state", [
    QaState.BLOCKED, QaState.TIMED_OUT, QaState.ENGINE_UNAVAILABLE,
    QaState.PREVIEW_UNAVAILABLE, QaState.CANCELLED,
])
def test_a_check_that_did_not_run_reports_no_counts(state):
    from app.coding.browser_findings import unavailable

    findings = unavailable(state, "because")
    data = findings.as_dict()
    for field in ("console_errors", "page_errors", "failed_requests",
                  "broken_images", "accessibility_findings", "h1_count",
                  "http_status", "problem_count"):
        assert data[field] is None, f"{field} must be None, not 0, when nothing looked"
    assert data["available"] is False


def test_a_dead_preview_is_its_own_state_not_a_missing_browser():
    class DeadSession:
        state = preview.PreviewState(running=False, last_error="it exited")

        def verify_ownership(self):
            return False, "The preview process has exited."

    findings = browser_qa.run_checks(DeadSession(), "/")
    assert findings.state is QaState.PREVIEW_UNAVAILABLE
    assert "exited" in findings.reason


def test_the_accessibility_rules_are_published_so_zero_can_be_read_honestly():
    from app.coding.browser_findings import unavailable

    data = unavailable(QaState.PASSED, "").as_dict()
    published = {row["rule"] for row in data["accessibility_rules"]}
    assert published == {rule for rule, _ in browser_probe.ACCESSIBILITY_RULES}
    assert len(published) == 9


# ---------------------------------------------------------------------------
# Real browser checks
# ---------------------------------------------------------------------------

@pytest.fixture
def screenshots_go_to_a_scratch_directory(tmp_path, monkeypatch):
    from app.core import app_paths

    scratch = tmp_path / "appdata"
    scratch.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app_paths, "data_dir", lambda: scratch)
    return scratch


@pytest.fixture(scope="module")
def hostile_preview(tmp_path_factory):
    """The hostile fixture site, served once for the whole module."""
    root = tmp_path_factory.mktemp("hostile") / "site"
    hostile.hostile_site(root)

    port = preview.find_free_port()
    assert port is not None
    session = preview.PreviewSession()
    handle = CommandHandle(hostile.serve_argv(root, port), root, root.name)
    handle.start(build_environment())
    ledger.track(handle)
    session._handle = handle
    session._state = preview.PreviewState(
        running=True, port=port, url=f"http://127.0.0.1:{port}/",
        pid=handle.pid, script="serve", started_at=time.time())

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not preview.port_in_use(port):
        time.sleep(0.1)
    assert session.state.running, "the hostile fixture server never came up"

    yield session
    session.stop("test finished")
    ledger.forget(handle)


@needs_browser
def test_a_clean_page_passes_only_with_a_named_engine(
        hostile_preview, screenshots_go_to_a_scratch_directory):
    """Zero is earned. A `PASSED` without an engine name would mean the
    state was set by something other than a browser."""
    findings = browser_qa.run_checks(hostile_preview, "/clean.html", task_id="clean")
    assert findings.state is QaState.PASSED, findings.reason
    assert findings.engine, "a passing check must name the engine that ran it"
    assert findings.available is True
    assert findings.http_status == 200
    assert findings.title == "Clean page"
    assert findings.lang == "en"
    assert findings.h1_count == 1
    assert findings.console_errors == 0
    assert findings.page_errors == 0
    assert findings.failed_requests == 0
    assert findings.broken_images == 0
    assert findings.accessibility_findings == 0
    assert findings.problem_count() == 0
    assert all(not v["overflows"] for v in findings.horizontal_overflow.values())


@needs_browser
def test_a_defective_page_reports_the_specific_real_defects(
        hostile_preview, screenshots_go_to_a_scratch_directory):
    """Five different kinds of defect, each asserted exactly.

    Deliberately not `problem_count() > 0`: a check that found the two
    `<h1>`s and nothing else would satisfy that while missing the console
    error, the broken image and the overflow entirely.
    """
    findings = browser_qa.run_checks(hostile_preview, "/defective.html",
                                     task_id="defective")
    assert findings.state is QaState.FAILED
    assert findings.http_status == 200
    assert findings.title == "Corner Shop"
    assert findings.h1_count == 2
    assert findings.console_errors == 1
    assert "checkout total is NaN" in findings.console_messages[0]
    assert findings.broken_images == 1
    assert "shopfront.png" in findings.broken_image_details[0]
    assert findings.failed_requests == 1          # the 404 on the image
    assert "HTTP 404 /shopfront.png" in findings.http_error_details
    assert findings.horizontal_overflow["320"]["overflows"] is True
    assert findings.horizontal_overflow["320"]["scroll_width"] >= 1900
    rules = {row["rule"] for row in findings.accessibility_details}
    assert "one-h1" in rules
    assert "image-alt" in rules
    # And the URL of the broken image is reported as a path, not with the
    # loopback origin and this run's incidental port.
    assert "127.0.0.1" not in " ".join(findings.broken_image_details)
    # One missing image, counted once as a request and once as an image —
    # not a third time as the network layer's own console line.
    assert findings.problem_count() == 1 + 1 + 1 + 2 + 3, findings.summary()


@needs_browser
def test_the_screenshot_is_written_bounded_and_named_without_identity(
        hostile_preview, screenshots_go_to_a_scratch_directory):
    findings = browser_qa.run_checks(hostile_preview, "/clean.html", task_id="shot")
    assert findings.screenshot
    written = screenshots_go_to_a_scratch_directory / "coding_screenshots" / findings.screenshot
    assert written.is_file()
    assert 0 < written.stat().st_size <= browser_qa.MAX_SCREENSHOT_BYTES
    assert written.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    for leak in ("hostile", "site", "127.0.0.1", str(hostile_preview.state.port)):
        assert leak not in findings.screenshot


@needs_browser
@pytest.mark.parametrize("route", [
    "/redirect-external", "/meta-refresh.html", "/js-redirect.html",
    "/alias.html", "/alias-ipv6.html",
])
def test_a_page_that_navigates_off_the_preview_is_blocked_and_says_where(
        hostile_preview, screenshots_go_to_a_scratch_directory, route):
    findings = browser_qa.run_checks(hostile_preview, route, task_id="nav")
    assert findings.state is QaState.BLOCKED, findings.reason
    assert findings.blocked_origins, "the attempt must be recorded, not merely refused"
    # The message names where the page tried to go, not Chromium's error page.
    assert "chrome-error" not in findings.reason
    assert "navigate away" in findings.reason
    # And nothing was inspected: no counts were produced.
    assert findings.console_errors is None


@needs_browser
def test_external_subresources_are_blocked_and_reported_not_counted_as_defects(
        hostile_preview, screenshots_go_to_a_scratch_directory):
    findings = browser_qa.run_checks(hostile_preview, "/external-assets.html",
                                     task_id="assets")
    assert findings.opened, "the page itself loads; only its assets are blocked"
    blocked = " ".join(findings.blocked_origins)
    assert "example.com" in blocked
    assert "cdn.example.com" in blocked
    # The boundary refusing a fetch is not a defect in the page, and must
    # not be counted as one.
    assert "ERR_PROXY" not in " ".join(findings.console_messages)
    assert "ERR_NAME_NOT_RESOLVED" not in " ".join(findings.console_messages)


@needs_browser
def test_a_popup_to_another_origin_is_recorded(
        hostile_preview, screenshots_go_to_a_scratch_directory):
    findings = browser_qa.run_checks(hostile_preview, "/popup.html", task_id="popup")
    assert any("example.com" in entry for entry in findings.blocked_origins)
    assert "blocked from leaving loopback" in findings.summary()


@needs_browser
def test_a_download_is_refused(hostile_preview, screenshots_go_to_a_scratch_directory):
    findings = browser_qa.run_checks(hostile_preview, "/download.html", task_id="dl")
    assert findings.downloads_blocked >= 1
    assert findings.opened


@needs_browser
def test_navigation_to_the_local_filesystem_is_refused(
        hostile_preview, screenshots_go_to_a_scratch_directory):
    findings = browser_qa.run_checks(hostile_preview, "/file-nav.html", task_id="file")
    assert any("file_scheme" in entry for entry in findings.blocked_origins)
    # Chromium refuses it too, and says so on the console. Both mechanisms.
    assert any("local resource" in m for m in findings.console_messages)


@needs_browser
def test_dialogs_are_dismissed_rather_than_deadlocking_the_check(
        hostile_preview, screenshots_go_to_a_scratch_directory):
    """Three blocking dialogs. With `Page.enable` on, a client that waits
    for its own command reply before answering one never returns."""
    started = time.monotonic()
    findings = browser_qa.run_checks(hostile_preview, "/dialog.html", task_id="dlg")
    assert time.monotonic() - started < browser_qa.MAX_RUN_SECONDS
    assert findings.dialogs_dismissed >= 3
    assert findings.opened


@needs_browser
def test_a_console_flood_is_counted_truthfully_and_shown_boundedly(
        hostile_preview, screenshots_go_to_a_scratch_directory):
    findings = browser_qa.run_checks(hostile_preview, "/console-flood.html",
                                     task_id="flood")
    assert findings.console_errors == 20000, "the count must not come from a capped list"
    assert len(findings.console_messages) <= browser_qa.MAX_DETAIL_ROWS
    assert findings.truncated is True


@needs_browser
def test_an_endless_navigation_still_terminates(
        hostile_preview, screenshots_go_to_a_scratch_directory):
    started = time.monotonic()
    findings = browser_qa.run_checks(hostile_preview, "/endless.html", task_id="endless")
    assert time.monotonic() - started < browser_qa.MAX_RUN_SECONDS
    assert findings.state is not QaState.TIMED_OUT or findings.reason


@needs_browser
def test_an_enormous_page_produces_a_bounded_screenshot(
        hostile_preview, screenshots_go_to_a_scratch_directory):
    findings = browser_qa.run_checks(hostile_preview, "/huge.html", task_id="huge")
    if findings.screenshot:
        written = (screenshots_go_to_a_scratch_directory / "coding_screenshots"
                   / findings.screenshot)
        assert written.stat().st_size <= browser_qa.MAX_SCREENSHOT_BYTES
    assert findings.horizontal_overflow["320"]["overflows"] is True


@needs_browser
def test_a_credential_printed_to_the_console_never_reaches_the_findings(
        hostile_preview, screenshots_go_to_a_scratch_directory):
    findings = browser_qa.run_checks(hostile_preview, "/secrets.html", task_id="secret")
    everything = str(findings.as_dict())
    assert "sk-ant-api03-" not in everything
    assert "AKIA" not in everything
    assert "redacted" in everything, "the message must say something was removed"


@needs_browser
@pytest.mark.parametrize("route", sorted(hostile.PAGES))
def test_no_browser_process_survives_any_fixture(
        hostile_preview, screenshots_go_to_a_scratch_directory, route):
    """The rule the WebView2 leak produced: 'killed' means gone."""
    findings = browser_qa.run_checks(hostile_preview, f"/{route}", task_id="clean-up")
    cleanup = findings.cleanup or {}
    assert cleanup.get("survivors") == [], cleanup.get("summary")
    assert cleanup.get("error") != "cleanup_failed"


@needs_browser
def test_the_browser_profile_is_deleted_after_every_run(
        hostile_preview, screenshots_go_to_a_scratch_directory, monkeypatch):
    """A reused profile would accumulate the history and storage of every
    page a coding task ever opened."""
    import tempfile

    created = []
    real = tempfile.mkdtemp

    def spy(*args, **kwargs):
        path = real(*args, **kwargs)
        if "jarvis-qa-profile-" in str(path):
            created.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    browser_qa.run_checks(hostile_preview, "/clean.html", task_id="profile")
    assert created, "a profile directory should have been made"
    for path in created:
        assert not path.exists(), f"{path} was left behind"


@needs_browser
def test_a_cancelled_check_reports_cancelled_and_leaves_nothing_running(
        hostile_preview, screenshots_go_to_a_scratch_directory):
    import threading

    cancel = threading.Event()
    cancel.set()
    findings = browser_qa.run_checks(hostile_preview, "/clean.html",
                                     task_id="cancel", cancel=cancel)
    assert findings.state is QaState.CANCELLED
    assert findings.console_errors is None


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------

def test_a_preview_whose_process_has_gone_fails_ownership(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    port = preview.find_free_port()
    session = preview.PreviewSession()
    handle = CommandHandle([sys.executable, "-c", "pass"], root, root.name)
    handle.start(build_environment())
    handle._process.wait(timeout=10)
    session._handle = handle
    session._state = preview.PreviewState(running=True, port=port)

    ok, why = session.verify_ownership()
    assert ok is False
    assert "exited" in why


def test_ownership_is_checked_before_anything_navigates(monkeypatch):
    """The check must not be reachable without the ownership proof."""
    calls = []

    class Session:
        state = preview.PreviewState(running=True, port=5180)

        def verify_ownership(self):
            calls.append("asked")
            return False, "not ours"

    monkeypatch.setattr(browser_qa.browser_engine, "find_engine",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("an engine must not be sought first")))
    findings = browser_qa.run_checks(Session(), "/")
    assert calls == ["asked"]
    assert findings.state is QaState.PREVIEW_UNAVAILABLE


# ---------------------------------------------------------------------------
# When the browser will not start, say what it said
# ---------------------------------------------------------------------------

def test_a_browser_that_will_not_start_reports_its_own_error(tmp_path):
    """"The browser connection closed." is not a diagnosis.

    That was the entire evidence behind fifteen CI failures, because the
    browser's stderr went to DEVNULL. A browser that exits at startup
    says why — a missing shared library, a refused sandbox — and that
    sentence is the difference between a fixable report and a dead end.
    """
    import os
    import stat

    from app.coding import browser_engine, browser_qa

    stub = tmp_path / "not-really-a-browser.sh"
    stub.write_text(
        "#!/bin/sh\n"
        "echo 'error while loading shared libraries: libnss3.so: "
        "cannot open shared object file' >&2\n"
        "exit 127\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    if os.name == "nt":                       # pragma: no cover - POSIX shim
        pytest.skip("the stub is a shell script; the assertion is platform-neutral")

    engine = browser_engine.Engine("chromium", "Chromium", str(stub))
    owned = browser_qa._OwnedBrowser(engine, [str(stub)], str(tmp_path / "profile"))
    owned.start()
    owned._process.wait(timeout=15)
    try:
        detail = owned.startup_error()
    finally:
        owned.stop()

    assert owned.is_running() is False
    assert "libnss3.so" in detail, (
        f"the browser's own reason was lost; got {detail!r}"
    )


def test_the_reported_browser_error_carries_no_path(tmp_path):
    """These reasons reach a log file and the Coding Workspace page. A
    Windows profile directory and an executable path both contain the
    account name."""
    import os
    import stat

    from app.coding import browser_engine, browser_qa

    stub = tmp_path / "pathy.sh"
    stub.write_text(
        "#!/bin/sh\n"
        "echo 'cannot create /home/somebody/.cache/profile-1234: denied' >&2\n"
        "echo 'C:\\\\Users\\\\Somebody\\\\AppData\\\\Local\\\\Temp\\\\p refused' >&2\n"
        "exit 1\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    if os.name == "nt":                       # pragma: no cover - POSIX shim
        pytest.skip("the stub is a shell script; the assertion is platform-neutral")

    engine = browser_engine.Engine("chromium", "Chromium", str(stub))
    owned = browser_qa._OwnedBrowser(engine, [str(stub)], str(tmp_path / "profile"))
    owned.start()
    owned._process.wait(timeout=15)
    try:
        detail = owned.startup_error()
    finally:
        owned.stop()

    assert "somebody" not in detail.lower(), f"an account name reached the report: {detail!r}"
    assert "/home/" not in detail and "C:\\" not in detail, (
        f"a path reached the report: {detail!r}"
    )
    assert "<path>" in detail, "the path should be replaced, not merely dropped"
