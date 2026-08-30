"""The desktop readiness contract.

"The server answers /health" and "JARVIS is running and can be
controlled" are different claims, and treating them as one caused a real
CI failure: the acceptance test read health as fully-started, sent a
graceful close a second before the tray's message loop existed, and
nothing received it.

The first fix waited for a line in the boot trace. That was acceptable as
evidence in a failure report and wrong as a contract — a human-readable
log line is not an interface. So readiness is now a real signal published
by the parent, and these tests hold it to the property that makes it
worth having: every fact is *proved*, never assumed.
"""

import pytest

from app.launcher.desktop_ready import DesktopReadyPublisher, DesktopReadyState


def _publisher(**kwargs):
    """A publisher whose network call is captured rather than made."""
    sent = []
    publisher = DesktopReadyPublisher(
        host="127.0.0.1", port=5555, session_secret="test-secret",
        post=lambda url, payload, headers: sent.append((url, payload, headers)),
        **kwargs,
    )
    return publisher, sent


# ---------------------------------------------------------------------------
# ready is derived, not settable
# ---------------------------------------------------------------------------

def test_nothing_is_ready_before_anything_is_proved():
    assert DesktopReadyState().ready is False


def test_all_four_facts_are_required():
    facts = ("server_healthy", "window_alive", "tray_listening", "parent_running")
    for omitted in facts:
        state = DesktopReadyState(**{name: True for name in facts if name != omitted})
        assert state.ready is False, f"ready must not be true without {omitted}"

    assert DesktopReadyState(**{name: True for name in facts}).ready is True


def test_ready_is_four_process_facts_and_claims_nothing_about_rendering():
    """`ready` is about processes, not pixels — a documented limit, not an
    oversight, and this pins it so it cannot be widened silently.

    Two findings sit behind it. `window_alive` is answered by the window
    child's IPC pump, which starts as soon as
    `webview_window.current_window()` is non-None — and `create_and_run()`
    publishes that object from `webview.create_window()`, which returns
    *before* `webview.start()`. And pywebview's `window.events.loaded` is
    not the missing evidence: in 6.2.1's edgechromium backend
    `on_navigation_completed(self, sender, _)` throws away the
    `NavigationCompletedEventArgs` without reading `IsSuccess` and calls
    `inject_pywebview()` regardless, while `util.py` sets `events.loaded`
    both at the end of the injection and inside its own `except` handler.
    An error page fires it exactly like a healthy dashboard.

    So a state can be fully ready with no evidence whatsoever that the
    dashboard rendered. Adding such evidence means proving it at the page
    (an in-page beacon or an `evaluate_js` probe for a known element),
    never inferring it here. Until then it is a manual real-PC check —
    see docs/physical-pc-checklist.md.
    """
    import inspect

    facts = ("server_healthy", "window_alive", "tray_listening", "parent_running")
    source = inspect.getsource(DesktopReadyState.ready.fget)

    assert {name for name in facts if name in source} == set(facts), (
        "ready must be derived from all four proved process facts"
    )

    other_fields = set(vars(DesktopReadyState())) - set(facts)
    for name in sorted(other_fields):
        assert name not in source, (
            f"{name!r} now feeds `ready`. If that is a rendering claim, it must be "
            "proved at the page, and this module's docstring plus "
            "docs/physical-pc-checklist.md must stop calling it unverified."
        )

    # The limitation, stated as an assertion rather than a comment.
    assert DesktopReadyState(**{name: True for name in facts}).ready is True, (
        "ready is reachable with zero evidence that WebView2 painted anything"
    )


def test_ready_cannot_be_set_directly():
    """A flag the parent sets because it believes it started correctly
    would report exactly the state that already failed on real hardware."""
    publisher, _ = _publisher()

    publisher.update(ready=True)

    assert publisher.state.ready is False


def test_an_unknown_fact_is_ignored_not_stored():
    publisher, _ = _publisher()

    publisher.update(server_healthy=True, something_invented=True)

    assert not hasattr(publisher.state, "something_invented")
    assert publisher.state.server_healthy is True


def test_missing_names_what_is_still_outstanding():
    """So a caller waiting on readiness can say what it is waiting for
    instead of timing out against a bare False."""
    state = DesktopReadyState(server_healthy=True, parent_running=True)

    assert set(state.missing()) == {"window_alive", "tray_listening"}


# ---------------------------------------------------------------------------
# The window fact is a real round trip
# ---------------------------------------------------------------------------

def test_the_window_fact_comes_from_an_actual_probe():
    publisher, _ = _publisher(probe_window=lambda: True)

    assert publisher.verify_window() is True
    assert publisher.state.window_alive is True


def test_a_window_that_does_not_answer_is_not_alive():
    publisher, _ = _publisher(probe_window=lambda: False)

    assert publisher.verify_window() is False
    assert publisher.state.window_alive is False


def test_a_probe_that_raises_reports_not_alive_rather_than_crashing():
    def _explode():
        raise RuntimeError("the control channel is gone")

    publisher, _ = _publisher(probe_window=_explode)

    assert publisher.verify_window() is False


def test_readiness_reporting_never_breaks_what_it_reports_on():
    """A failed publish must not take down startup."""
    def _explode(url, payload, headers):
        raise OSError("the server is not listening yet")

    publisher = DesktopReadyPublisher(
        host="127.0.0.1", port=5555, session_secret="s", post=_explode,
    )

    publisher.update(server_healthy=True)  # must not raise
    assert publisher.state.server_healthy is True


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def test_every_update_is_published_with_the_shared_secret():
    publisher, sent = _publisher()

    publisher.update(server_healthy=True)

    url, payload, headers = sent[-1]
    assert url.endswith("/desktop/ready")
    assert headers["X-JARVIS-Desktop-Secret"] == "test-secret"
    assert payload["server_healthy"] is True
    assert payload["ready"] is False


def test_the_session_id_is_not_derived_from_the_secret():
    """Reading readiness is unauthenticated, so anything published here
    is public. A prefix of the secret would have handed part of it to
    anything that asked."""
    publisher, _ = _publisher()

    session_id = publisher.state.session_id
    assert session_id
    assert session_id not in "test-secret"
    assert "test-secret" not in session_id


def test_each_publisher_gets_its_own_session_id():
    """One publisher per server child, so a restart is distinguishable
    from the run before it — which is what makes "the restart produced a
    genuinely fresh session" checkable rather than asserted."""
    first, _ = _publisher()
    second, _ = _publisher()

    assert first.state.session_id != second.state.session_id


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app

    import app.api.routes as routes
    routes._desktop_ready_state.clear()
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield client
    routes._desktop_ready_state.clear()


@pytest.fixture
def desktop_secret(monkeypatch):
    from app.launcher.server_process import SESSION_SECRET_ENV

    monkeypatch.setenv(SESSION_SECRET_ENV, "the-real-desktop-secret")
    return "the-real-desktop-secret"


def test_readiness_reads_as_not_ready_before_the_parent_says_otherwise(api_client):
    body = api_client.get("/desktop/ready").json()

    assert body["ready"] is False
    assert set(body["missing"]) == {"server_healthy", "window_alive", "tray_listening", "parent_running"}


def test_the_parent_can_publish_and_it_reads_back(api_client, desktop_secret):
    published = api_client.post(
        "/desktop/ready",
        json={
            "server_healthy": True, "window_alive": True,
            "tray_listening": True, "parent_running": True,
            "session_id": "abc123", "detail": "JARVIS is running.",
        },
        headers={"X-JARVIS-Desktop-Secret": desktop_secret},
    )

    assert published.status_code == 200
    assert published.json()["ready"] is True

    body = api_client.get("/desktop/ready").json()
    assert body["ready"] is True
    assert body["session_id"] == "abc123"
    assert body["missing"] == []


def test_publishing_without_the_secret_is_refused(api_client, desktop_secret):
    r = api_client.post("/desktop/ready", json={"server_healthy": True})

    assert r.status_code == 403
    assert api_client.get("/desktop/ready").json()["ready"] is False


def test_publishing_with_the_wrong_secret_is_refused(api_client, desktop_secret):
    r = api_client.post(
        "/desktop/ready",
        json={"server_healthy": True},
        headers={"X-JARVIS-Desktop-Secret": "guessed"},
    )

    assert r.status_code == 403


def test_a_server_with_no_secret_accepts_nothing(api_client, monkeypatch):
    """Dev and test runs have no inherited secret. Refusing every publish
    is the safe direction — the alternative turns a missing secret into
    an open endpoint."""
    from app.launcher.server_process import SESSION_SECRET_ENV

    monkeypatch.delenv(SESSION_SECRET_ENV, raising=False)

    r = api_client.post(
        "/desktop/ready",
        json={"server_healthy": True},
        headers={"X-JARVIS-Desktop-Secret": ""},
    )
    assert r.status_code == 403


def test_reading_readiness_needs_no_credential(api_client):
    """It carries no user data, and answering "not yet" to anything that
    asks is harmless — while requiring a token would make it useless to
    the acceptance test that has to poll it."""
    assert api_client.get("/desktop/ready").status_code == 200


def test_the_signal_never_carries_a_secret(api_client, desktop_secret, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-appear")
    api_client.post(
        "/desktop/ready",
        json={"server_healthy": True, "detail": "up"},
        headers={"X-JARVIS-Desktop-Secret": desktop_secret},
    )

    text = api_client.get("/desktop/ready").text
    assert "sk-" not in text
    assert desktop_secret not in text
