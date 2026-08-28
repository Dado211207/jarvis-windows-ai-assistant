"""Tests for app/launcher/webview_window.py.

create_and_run() is exercised with an injected fake `webview` module — a
real pywebview window needs an actual WebView2-capable Windows desktop,
which this repo's Linux dev/CI cannot provide (matching tray.py's own
"native execution is untestable here, only its pure logic is" pattern).
The fake module lets these tests prove the actual decision logic (does a
close get cancelled? does on_quit fire? what does start() get called
with?) instead of only asserting a mock was invoked with the right
arguments.
"""

from unittest.mock import MagicMock

import pytest


class _FakeEvent:
    """Stand-in for webview.event.Event — just enough to register a
    handler via += and let the test invoke it directly, mirroring the
    real Event.__iadd__ contract (see webview/event.py)."""

    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, *args, **kwargs):
        results = [h(*args, **kwargs) for h in self.handlers]
        return results[-1] if results else None


class _FakeEvents:
    def __init__(self) -> None:
        self.closing = _FakeEvent()
        self.closed = _FakeEvent()


class _FakeWindow:
    def __init__(self) -> None:
        self.events = _FakeEvents()
        self.hide = MagicMock()
        self.show = MagicMock()
        self.restore = MagicMock()
        self.destroy = MagicMock()


class _FakeWebviewModule:
    def __init__(self) -> None:
        self.created_window = None
        self.create_window_kwargs = None
        self.start_kwargs = None

    def create_window(self, title, **kwargs):
        self.create_window_kwargs = {"title": title, **kwargs}
        self.created_window = _FakeWindow()
        return self.created_window

    def start(self, **kwargs):
        self.start_kwargs = kwargs


@pytest.fixture(autouse=True)
def _reset_module_window():
    """create_and_run() sets a module-level _window, and request_shutdown()
    sets a module-level Event — reset both around every test so one
    test's state can never leak into another's assertions."""
    from app.launcher import webview_window
    webview_window._window = None
    webview_window._shutdown_requested.clear()
    yield
    webview_window._window = None
    webview_window._shutdown_requested.clear()


# ---------------------------------------------------------------------------
# resolve_close_action
# ---------------------------------------------------------------------------

def test_resolve_close_action_accepts_valid_values():
    from app.launcher.webview_window import resolve_close_action
    assert resolve_close_action("tray") == "tray"
    assert resolve_close_action("quit") == "quit"


def test_resolve_close_action_is_case_and_whitespace_insensitive():
    from app.launcher.webview_window import resolve_close_action
    assert resolve_close_action(" Quit ") == "quit"
    assert resolve_close_action("TRAY") == "tray"


def test_resolve_close_action_falls_back_to_tray_for_anything_else():
    from app.launcher.webview_window import resolve_close_action
    assert resolve_close_action("") == "tray"
    assert resolve_close_action("close") == "tray"
    assert resolve_close_action("exit") == "tray"


# ---------------------------------------------------------------------------
# is_supported
# ---------------------------------------------------------------------------

def test_is_supported_true_on_win32(monkeypatch):
    from app.launcher import webview_window
    monkeypatch.setattr(webview_window.sys, "platform", "win32", raising=False)
    assert webview_window.is_supported() is True


def test_is_supported_false_off_windows(monkeypatch):
    from app.launcher import webview_window
    monkeypatch.setattr(webview_window.sys, "platform", "linux", raising=False)
    assert webview_window.is_supported() is False


# ---------------------------------------------------------------------------
# create_and_run — window construction
# ---------------------------------------------------------------------------

def test_create_and_run_builds_window_with_expected_title_url_and_sizing():
    from app.launcher import webview_window

    fake = _FakeWebviewModule()
    webview_window.create_and_run(
        url="http://127.0.0.1:5555/ui/",
        icon_path="/some/icon.ico",
        close_action="tray",
        on_quit=MagicMock(),
        webview_module=fake,
    )

    kwargs = fake.create_window_kwargs
    assert kwargs["title"] == "JARVIS"
    assert kwargs["url"] == "http://127.0.0.1:5555/ui/"
    assert kwargs["min_size"] == (webview_window.MIN_WIDTH, webview_window.MIN_HEIGHT)
    assert kwargs["resizable"] is True


def test_create_and_run_forces_edgechromium_and_passes_icon():
    """The one non-negotiable part of this module: never let pywebview
    silently pick its own backend (which could be the legacy mshtml/IE
    engine on a machine without the WebView2 Runtime — see the module
    docstring)."""
    from app.launcher import webview_window

    fake = _FakeWebviewModule()
    webview_window.create_and_run(
        url="http://127.0.0.1:5555/ui/",
        icon_path="/some/icon.ico",
        close_action="tray",
        on_quit=MagicMock(),
        webview_module=fake,
    )

    assert fake.start_kwargs["gui"] == "edgechromium"
    assert fake.start_kwargs["icon"] == "/some/icon.ico"


# ---------------------------------------------------------------------------
# create_and_run — close_action="tray": closing hides instead of quitting
# ---------------------------------------------------------------------------

def test_tray_close_action_cancels_close_and_hides_window():
    from app.launcher import webview_window

    fake = _FakeWebviewModule()
    on_quit = MagicMock()
    webview_window.create_and_run(
        url="http://x/", icon_path=None, close_action="tray", on_quit=on_quit, webview_module=fake,
    )

    result = fake.created_window.events.closing.fire()

    assert result is False  # False cancels the close — see webview/event.py::Event.set()
    fake.created_window.hide.assert_called_once()


def test_tray_close_action_does_not_call_on_quit_when_window_closes():
    """closed only fires once a close is *not* cancelled — with
    close_action="tray" the closing handler always cancels, so on_quit
    must never fire even if closed somehow still runs."""
    from app.launcher import webview_window

    fake = _FakeWebviewModule()
    on_quit = MagicMock()
    webview_window.create_and_run(
        url="http://x/", icon_path=None, close_action="tray", on_quit=on_quit, webview_module=fake,
    )

    fake.created_window.events.closed.fire()

    on_quit.assert_not_called()


# ---------------------------------------------------------------------------
# create_and_run — close_action="quit": closing proceeds and on_quit fires
# ---------------------------------------------------------------------------

def test_quit_close_action_lets_close_proceed():
    from app.launcher import webview_window

    fake = _FakeWebviewModule()
    webview_window.create_and_run(
        url="http://x/", icon_path=None, close_action="quit", on_quit=MagicMock(), webview_module=fake,
    )

    result = fake.created_window.events.closing.fire()

    assert result is not False  # not cancelled
    fake.created_window.hide.assert_not_called()


def test_quit_close_action_calls_on_quit_when_window_closes():
    from app.launcher import webview_window

    fake = _FakeWebviewModule()
    on_quit = MagicMock()
    webview_window.create_and_run(
        url="http://x/", icon_path=None, close_action="quit", on_quit=on_quit, webview_module=fake,
    )

    fake.created_window.events.closed.fire()

    on_quit.assert_called_once()


def test_window_reference_cleared_after_closed():
    from app.launcher import webview_window

    fake = _FakeWebviewModule()
    webview_window.create_and_run(
        url="http://x/", icon_path=None, close_action="quit", on_quit=MagicMock(), webview_module=fake,
    )
    assert webview_window.show_existing() is True  # window exists before closed fires

    fake.created_window.events.closed.fire()

    assert webview_window.show_existing() is False  # gone after closed


# ---------------------------------------------------------------------------
# show_existing / destroy_existing
# ---------------------------------------------------------------------------

def test_show_existing_returns_false_when_no_window_exists():
    from app.launcher import webview_window
    assert webview_window.show_existing() is False


def test_show_existing_shows_and_restores_the_real_window():
    from app.launcher import webview_window

    fake = _FakeWebviewModule()
    webview_window.create_and_run(
        url="http://x/", icon_path=None, close_action="tray", on_quit=MagicMock(), webview_module=fake,
    )

    assert webview_window.show_existing() is True
    fake.created_window.show.assert_called_once()
    fake.created_window.restore.assert_called_once()


def test_show_existing_returns_false_instead_of_raising_when_show_fails():
    from app.launcher import webview_window

    fake = _FakeWebviewModule()
    webview_window.create_and_run(
        url="http://x/", icon_path=None, close_action="tray", on_quit=MagicMock(), webview_module=fake,
    )
    fake.created_window.show.side_effect = RuntimeError("window handle is gone")

    assert webview_window.show_existing() is False  # must not raise


def test_destroy_existing_is_a_safe_no_op_when_no_window_exists():
    from app.launcher import webview_window
    webview_window.destroy_existing()  # must not raise


def test_destroy_existing_destroys_the_real_window():
    from app.launcher import webview_window

    fake = _FakeWebviewModule()
    webview_window.create_and_run(
        url="http://x/", icon_path=None, close_action="tray", on_quit=MagicMock(), webview_module=fake,
    )

    webview_window.destroy_existing()

    fake.created_window.destroy.assert_called_once()


def test_destroy_existing_never_raises_even_if_destroy_fails():
    from app.launcher import webview_window

    fake = _FakeWebviewModule()
    webview_window.create_and_run(
        url="http://x/", icon_path=None, close_action="tray", on_quit=MagicMock(), webview_module=fake,
    )
    fake.created_window.destroy.side_effect = RuntimeError("already gone")

    webview_window.destroy_existing()  # must not raise


# ---------------------------------------------------------------------------
# request_shutdown — the close-to-tray veto must never block a real shutdown
# ---------------------------------------------------------------------------

def test_tray_close_action_stops_cancelling_the_close_once_shutdown_is_requested():
    """Regression test for a real windows-latest CI failure: with
    close_action="tray", the window's close handler cancelled the
    WM_CLOSE that a graceful `taskkill` sends, so the packaged app
    refused to exit and the clean-install test timed out waiting for it
    (the graceful-taskkill phase). A user's X click and an OS/installer shutdown request
    are the same WM_CLOSE message and can't be told apart at the
    pywebview level — request_shutdown() is the explicit signal that
    distinguishes them."""
    from app.launcher import webview_window

    fake = _FakeWebviewModule()
    webview_window.create_and_run(
        url="http://x/", icon_path=None, close_action="tray", on_quit=MagicMock(), webview_module=fake,
    )
    assert fake.created_window.events.closing.fire() is False  # normal X click: still minimizes to tray

    webview_window.request_shutdown()

    assert fake.created_window.events.closing.fire() is not False, (
        "a close arriving after request_shutdown() must proceed, not be converted to a hide"
    )


def test_destroy_existing_requests_shutdown_first():
    """The tray's Quit path calls destroy_existing(); that must itself
    disarm the close-to-tray veto, so a tray-initiated quit can never be
    silently swallowed by the window's own close handler."""
    from app.launcher import webview_window

    fake = _FakeWebviewModule()
    webview_window.create_and_run(
        url="http://x/", icon_path=None, close_action="tray", on_quit=MagicMock(), webview_module=fake,
    )

    webview_window.destroy_existing()

    assert webview_window._shutdown_requested.is_set()


def test_quit_is_the_default_close_action_in_settings():
    """Not a style preference: "quit" is the default specifically because
    a graceful taskkill must be able to stop the packaged app (see
    request_shutdown()'s docstring and app/config.py's own comment).
    Flipping this default back to "tray" would reintroduce the exact CI
    failure above, so it's worth failing loudly on."""
    from app.config import Settings
    assert Settings().jarvis_close_action == "quit"


# ---------------------------------------------------------------------------
# force_exit_after — the "JARVIS must never become unclosable" guarantee
# ---------------------------------------------------------------------------

def test_force_exit_after_schedules_a_daemon_timer_that_has_not_fired_yet():
    """Deliberately never lets the real timer elapse: its callback is
    os._exit(), which would take the test runner down with it. Proving
    the timer is armed and daemonic is the testable part; that
    os._exit() ends a process needs no test."""
    from app.launcher import webview_window

    timer = webview_window.force_exit_after(grace_seconds=30)
    try:
        assert timer.is_alive()
        assert timer.daemon is True, "the watchdog must never itself keep the process alive"
    finally:
        timer.cancel()


def test_force_exit_after_callback_calls_os_exit(monkeypatch):
    """Runs the watchdog's callback directly (never via a real elapsed
    timer) with os._exit patched, so the actual "does it force the
    process to exit" behaviour is proven rather than assumed from the
    source reading."""
    import os

    from app.launcher import webview_window

    exit_calls = []
    monkeypatch.setattr(os, "_exit", lambda code: exit_calls.append(code))

    timer = webview_window.force_exit_after(grace_seconds=0.01)
    timer.join(timeout=5)

    assert exit_calls == [0]
