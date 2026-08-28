"""The native folder dialog, end to end without a native window.

The dialog itself is Windows'. What is testable — and what actually
carries the security — is the brokering around it: who may ask for one,
who may answer, what happens to a Cancel, and whether a selection can be
replayed. All of that runs on this repo's Linux CI through the same
injection seam `webview_window.create_and_run` already uses, so the
authenticated path is exercised rather than described.

The one thing these tests deliberately do not prove is that Windows draws
a dialog. Nothing on a Linux runner can prove that, and a test that
mocked its way to claiming it would be worse than no test.
"""

from __future__ import annotations

import time

import pytest

from app.coding import folder_requests
from app.launcher import folder_picker


@pytest.fixture(autouse=True)
def a_clean_broker():
    folder_requests.broker.clear()
    folder_picker.abandon()
    yield
    folder_requests.broker.clear()
    folder_picker.abandon()


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------

def test_a_request_starts_pending_and_carries_a_prompt():
    request = folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)
    assert request.state == folder_requests.PENDING
    assert request.id
    payload = request.as_dict(include_path=True)
    assert payload["prompt"]
    assert payload["path"] == ""


def test_only_a_purpose_jarvis_knows_may_be_asked_for():
    with pytest.raises(folder_requests.FolderRequestError):
        folder_requests.broker.create("read_the_whole_disk")


def test_a_second_dialog_is_refused_while_one_is_pending():
    """Two modals owned by the same window is a reliable way to produce
    one nobody can dismiss."""
    folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)
    with pytest.raises(folder_requests.FolderRequestError) as exc:
        folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)
    assert "already open" in str(exc.value)


def test_a_settled_request_frees_the_slot(tmp_path):
    first = folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)
    folder_requests.broker.resolve(first.id, cancelled=True)
    second = folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)
    assert second.state == folder_requests.PENDING


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------

def test_a_real_selection_is_canonicalised_and_returned(tmp_path):
    chosen = tmp_path / "projects" / "my-site"
    chosen.mkdir(parents=True)
    request = folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)

    folder_requests.broker.resolve(request.id, path=str(chosen))
    assert request.state == folder_requests.SELECTED
    assert request.path == str(chosen.resolve())
    assert folder_requests.broker.consume(request.id) == str(chosen.resolve())


def test_cancel_creates_nothing_and_changes_nothing(tmp_path):
    before = sorted(p.name for p in tmp_path.iterdir())
    request = folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)
    folder_requests.broker.resolve(request.id, cancelled=True)

    assert request.state == folder_requests.CANCELLED
    assert request.path == ""
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    with pytest.raises(folder_requests.FolderRequestError):
        folder_requests.broker.consume(request.id)


def test_a_request_may_only_be_answered_once(tmp_path):
    """A second result is either a bug or an attempt to replace a chosen
    folder with a different one."""
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    request = folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)
    folder_requests.broker.resolve(request.id, path=str(first))

    with pytest.raises(folder_requests.FolderRequestError):
        folder_requests.broker.resolve(request.id, path=str(second))
    assert request.path == str(first.resolve())


def test_an_unknown_request_cannot_be_answered():
    with pytest.raises(folder_requests.FolderRequestError):
        folder_requests.broker.resolve("not-a-request", path="/tmp")


def test_a_selection_is_spent_once(tmp_path):
    chosen = tmp_path / "one"
    chosen.mkdir()
    request = folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)
    folder_requests.broker.resolve(request.id, path=str(chosen))

    folder_requests.broker.consume(request.id)
    with pytest.raises(folder_requests.FolderRequestError) as exc:
        folder_requests.broker.consume(request.id)
    assert "already been used" in str(exc.value)


def test_an_expired_request_cannot_be_answered(tmp_path, monkeypatch):
    chosen = tmp_path / "late"
    chosen.mkdir()
    request = folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)
    request.created_at = time.time() - folder_requests.REQUEST_TTL_SECONDS - 1

    with pytest.raises(folder_requests.FolderRequestError):
        folder_requests.broker.resolve(request.id, path=str(chosen))
    assert request.state == folder_requests.EXPIRED


# ---------------------------------------------------------------------------
# What may come back through the wire
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hostile", [
    "\\\\server\\share",                 # UNC
    "\\\\?\\C:\\Windows",                # device path
    "C:\\Windows\\System32\\config:$DATA",   # alternate data stream
    "CON",                                    # reserved device name
    "",                                       # nothing at all
])
def test_a_path_the_dialog_could_not_have_returned_is_still_screened(hostile):
    """The value arrives over a socket. A boundary that trusts its input
    because of where it is supposed to have come from is not a boundary."""
    request = folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)
    folder_requests.broker.resolve(request.id, path=hostile)
    assert request.state in (folder_requests.FAILED, folder_requests.CANCELLED)
    assert request.path == ""


def test_a_file_rather_than_a_folder_is_refused(tmp_path):
    a_file = tmp_path / "notes.txt"
    a_file.write_text("hello", encoding="utf-8")
    request = folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)
    folder_requests.broker.resolve(request.id, path=str(a_file))
    assert request.state == folder_requests.FAILED
    assert request.path == ""


def test_a_folder_that_is_not_there_is_refused(tmp_path):
    request = folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)
    folder_requests.broker.resolve(request.id, path=str(tmp_path / "nowhere"))
    assert request.state == folder_requests.FAILED


def test_the_chosen_path_is_never_given_to_anyone_but_the_page(tmp_path):
    chosen = tmp_path / "private"
    chosen.mkdir()
    request = folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)
    folder_requests.broker.resolve(request.id, path=str(chosen))

    assert request.as_dict(include_path=False)["path"] == ""
    assert request.as_dict(include_path=True)["path"] == str(chosen.resolve())


def test_no_chosen_path_is_written_to_a_log(tmp_path, caplog):
    import logging

    chosen = tmp_path / "SecretProjectFolder"
    chosen.mkdir()
    with caplog.at_level(logging.DEBUG):
        request = folder_requests.broker.create(folder_requests.PURPOSE_ADD_PROJECT)
        folder_requests.broker.resolve(request.id, path=str(chosen))
    assert "SecretProjectFolder" not in caplog.text


# ---------------------------------------------------------------------------
# The window side
# ---------------------------------------------------------------------------

class _FakeWebview:
    FOLDER_DIALOG = 20


class _FakeWindow:
    """Stands in for the pywebview window, and records what it was asked."""

    def __init__(self, returns):
        self.returns = returns
        self.calls = []

    def create_file_dialog(self, kind, **kwargs):
        self.calls.append((kind, kwargs))
        if isinstance(self.returns, Exception):
            raise self.returns
        return self.returns


@pytest.fixture
def a_window(monkeypatch):
    def install(returns):
        from app.launcher import webview_window

        window = _FakeWindow(returns)
        monkeypatch.setattr(webview_window, "current_window", lambda: window)
        return window
    return install


@pytest.fixture
def a_recording_poster(monkeypatch):
    """Captures what the window would post, instead of posting it."""
    posted = []

    def poster(url, payload, headers):
        posted.append({"url": url, "payload": payload, "headers": headers})

    monkeypatch.setenv("JARVIS_SESSION_SECRET", "test-desktop-secret")
    monkeypatch.setenv("JARVIS_WINDOW_URL", "http://127.0.0.1:8123")
    return posted, poster


def test_the_window_reports_a_selection_with_the_desktop_secret(
        tmp_path, a_window, a_recording_poster):
    chosen = tmp_path / "picked"
    chosen.mkdir()
    a_window([str(chosen)])
    posted, poster = a_recording_poster

    result = folder_picker.choose_folder("req-1", "Choose a folder",
                                         webview_module=_FakeWebview, poster=poster)
    assert result["state"] == "selected"
    assert len(posted) == 1
    assert posted[0]["url"].endswith("/coding/folder-dialog/req-1/result")
    assert posted[0]["payload"] == {"path": str(chosen)}
    assert posted[0]["headers"]["X-JARVIS-Desktop-Secret"] == "test-desktop-secret"


def test_the_window_reports_a_cancel_as_a_cancel(a_window, a_recording_poster):
    a_window([])                       # pywebview returns falsy for Cancel
    posted, poster = a_recording_poster

    result = folder_picker.choose_folder("req-2", webview_module=_FakeWebview, poster=poster)
    assert result["state"] == "cancelled"
    assert posted[0]["payload"] == {"cancelled": True}


def test_without_the_desktop_secret_nothing_is_claimed(a_window, monkeypatch, tmp_path):
    """Claiming a folder was picked without proof is the thing this design
    exists to stop."""
    chosen = tmp_path / "unprovable"
    chosen.mkdir()
    a_window([str(chosen)])
    monkeypatch.delenv("JARVIS_SESSION_SECRET", raising=False)
    posted = []

    result = folder_picker.choose_folder(
        "req-3", webview_module=_FakeWebview,
        poster=lambda *a: posted.append(a))
    assert result["state"] == "failed"
    assert posted == []


def test_the_window_refuses_a_second_dialog_while_one_is_open(a_recording_poster):
    """Belt and braces: the server refuses to mint a second pending
    request, and this refuses to open a second modal if one arrived
    anyway."""
    import threading

    posted, poster = a_recording_poster
    entered = threading.Event()
    release = threading.Event()

    class SlowWindow:
        def create_file_dialog(self, kind, **kwargs):
            entered.set()
            release.wait(5)
            return []

    from app.launcher import webview_window
    original = webview_window.current_window
    webview_window.current_window = lambda: SlowWindow()
    try:
        first = threading.Thread(
            target=folder_picker.choose_folder,
            args=("slow-1",), kwargs={"webview_module": _FakeWebview, "poster": poster})
        first.start()
        assert entered.wait(5), "the first dialog never opened"

        second = folder_picker.choose_folder("slow-2", webview_module=_FakeWebview,
                                             poster=poster)
        assert second["state"] == "failed"
        assert "already open" in second["error"]
    finally:
        release.set()
        first.join(timeout=5)
        webview_window.current_window = original

    assert folder_picker.is_open() is False


def test_a_dialog_with_no_window_to_own_it_is_refused(monkeypatch, a_recording_poster):
    from app.launcher import webview_window

    monkeypatch.setattr(webview_window, "current_window", lambda: None)
    posted, poster = a_recording_poster
    result = folder_picker.choose_folder("no-window", webview_module=_FakeWebview,
                                         poster=poster)
    assert result["state"] == "failed"
    assert posted[0]["payload"]["error"]


def test_a_dialog_failure_releases_the_guard(a_window, a_recording_poster):
    a_window(RuntimeError("the shell refused"))
    posted, poster = a_recording_poster

    result = folder_picker.choose_folder("boom", webview_module=_FakeWebview, poster=poster)
    assert result["state"] == "failed"
    assert folder_picker.is_open() is False, "a failed dialog must not wedge the guard"


def test_a_forged_request_id_shape_is_rejected_before_a_window_is_touched(monkeypatch):
    from app.launcher import webview_window

    monkeypatch.setattr(webview_window, "current_window", lambda: (_ for _ in ()).throw(
        AssertionError("no window should be sought for a malformed id")))
    for bad in ("", None, 123, "x" * 200):
        result = folder_picker.choose_folder(bad, webview_module=_FakeWebview)
        assert result["state"] == "failed"


def test_the_bridge_exposes_exactly_one_method():
    """This object is the entire attack surface the page has on the native
    process. Every method added to it is another thing a page can do."""
    public = [name for name in dir(folder_picker.WindowApi)
              if not name.startswith("_")]
    assert public == ["choose_folder"]


def test_closing_the_window_releases_the_guard(a_window, a_recording_poster):
    """A window that went away while a dialog was open must not leave the
    next window believing one is still up."""
    from app.launcher import webview_window

    folder_picker._open = True
    _, on_closed = webview_window._make_handlers("tray", lambda: None)
    on_closed()
    assert folder_picker.is_open() is False


def test_no_directory_is_suggested_to_the_dialog(tmp_path, a_window, a_recording_poster):
    """Starting the dialog at "the last folder you picked" would mean
    JARVIS remembering, and displaying, a list of the user's directories."""
    chosen = tmp_path / "somewhere"
    chosen.mkdir()
    window = a_window([str(chosen)])
    _, poster = a_recording_poster

    folder_picker.choose_folder("req", webview_module=_FakeWebview, poster=poster)
    kind, kwargs = window.calls[0]
    assert kind == _FakeWebview.FOLDER_DIALOG
    assert "directory" not in kwargs
    assert kwargs.get("allow_multiple") is False
