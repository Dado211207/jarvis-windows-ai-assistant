"""Shared pytest helpers across the test suite."""

import contextlib

import pytest

SESSION_TOKEN_HEADER = "X-JARVIS-Session-Token"


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    """Every test gets its own preferences file and pronunciation store.

    Autouse and unconditional: app/core/preferences.py is written to by
    ordinary product code (turning on spoken replies, picking an AI
    provider), so without this a test run would edit the developer's real
    settings, and — worse — one test's choice would silently change what
    the next test observes. Redirecting only the preferences module's own
    reference leaves app_paths' own tests measuring the real thing.

    app/voice/pronunciations.py is redirected for exactly the same
    reason and needs its own line: it holds its own reference to
    config_dir, so patching the preferences module does not reach it.
    Both are per-test user data written by ordinary use of the product,
    and a test that saves how to say a name must not change how the next
    test hears it.
    """
    monkeypatch.setattr("app.core.preferences.config_dir", lambda: tmp_path)
    monkeypatch.setattr("app.voice.pronunciations.config_dir", lambda: tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def _forget_runtime_credential_downgrade():
    """Reset app/core/providers.py's process-local "this credential was
    rejected" note between tests.

    It is deliberately module-global — it has to outlive a request, because
    it exists for the case where the observation could not be written to
    disk — and anything module-global is shared state a test can leak into
    the next one. Cleared on both sides so neither the test that sets it nor
    the test that runs after it depends on ordering.
    """
    from app.core.ai import credential_view
    from app.core.providers import clear_runtime_downgrade

    # credential_view keeps the last coherent snapshot module-globally, to
    # hand back when a reader cannot get into the gate. A test that seeds
    # the stores directly never moves the coordinator's revision, so that
    # published snapshot can outlive the test that produced it. Dropping it
    # on both sides keeps one test's credential out of the next test's
    # request.
    clear_runtime_downgrade()
    credential_view.invalidate()
    yield
    clear_runtime_downgrade()
    credential_view.invalidate()


def prime_session(client):
    """Perform a GET to receive the v0.2 CSRF/mutation session cookie
    (see app/api/session.py), then set the matching header as a default
    on the client so every subsequent mutating REST call and WebSocket
    connect automatically carries it — exactly what the real dashboard's
    own JS does by reading the (deliberately non-HttpOnly) cookie and
    echoing it back. Returns the same client for convenient chaining in
    a fixture's `yield prime_session(client)`.
    """
    client.get("/health")
    token = client.cookies.get("jarvis_session")
    if token:
        client.headers[SESSION_TOKEN_HEADER] = token
    return client


# ---------------------------------------------------------------------------
# The in-process server the real-browser suites talk to.
#
# Defined here, once, rather than in one test module and imported into
# the other. Importing a session-scoped fixture into a second module
# registers a *second* FixtureDef with its own cache, so both copies try
# to bind the same port and the second dies with `SystemExit: 3` from
# uvicorn — which is exactly what happened the first time
# tests/test_clap_detection.py and tests/test_playwright_e2e.py were run
# in one session. One definition, one server, one port.
# ---------------------------------------------------------------------------

BROWSER_TEST_PORT = 5557
BROWSER_BASE_URL = f"http://127.0.0.1:{BROWSER_TEST_PORT}"


@pytest.fixture(scope="session")
def live_server():
    import threading
    import time

    try:
        import uvicorn
    except ImportError:
        pytest.skip("uvicorn is required to run the live server for browser tests")

    from app.voice.stt import FakeSTTAdapter, stt_service
    stt_service.set_adapter_override(FakeSTTAdapter(transcript="system status"))

    # app/api/origin.py's allowlist is derived from settings.jarvis_port —
    # it must match the port this test server actually runs on, or the
    # browser's real Origin header gets correctly rejected by the app's
    # own origin check.
    from app.config import settings
    settings.jarvis_port = BROWSER_TEST_PORT

    from app.api.server import app as jarvis_app

    config = uvicorn.Config(
        jarvis_app, host="127.0.0.1", port=BROWSER_TEST_PORT, log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="jarvis-browser-test-server")
    thread.start()

    import httpx
    for _ in range(75):
        try:
            if httpx.get(f"{BROWSER_BASE_URL}/health", timeout=1.0).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.2)
    else:
        pytest.fail("live_server did not become ready in time")

    yield BROWSER_BASE_URL

    server.should_exit = True
    thread.join(timeout=5.0)
    stt_service.set_adapter_override(None)


# ---------------------------------------------------------------------------
# One Playwright driver for the whole session.
#
# Playwright's sync API runs on its own greenlet dispatcher and cannot be
# nested: entering a second `sync_playwright()` while one is already open
# fails with "you are using Playwright Sync API inside the asyncio loop".
# Both browser suites need a browser, and tests/test_clap_detection.py
# needs a *differently launched* one per test (its microphone is a file
# chosen at launch), so the driver is shared and the browsers are not.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def playwright_instance():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright is not installed — pip install playwright && playwright install chromium")

    with sync_playwright() as p:
        yield p


def chromium_executable_path():
    """Normally None: `playwright install chromium` puts the browser where
    the `playwright` package itself expects it. The override exists only
    for environments with a browser pre-installed at a nonstandard path
    outside Playwright's own version-matched cache — this repo's sandboxed
    dev container, not a real deployment concern."""
    import os

    return os.environ.get("JARVIS_TEST_CHROMIUM_PATH") or None


@contextlib.contextmanager
def credential_present(key: str = "sk-test-key"):
    """Make the real credential snapshot report *key* as configured.

    Since round 7 the provider's credential comes from one coherent
    snapshot (`app/core/ai/credential_view.py`) rather than from separate
    `settings` reads, so a test that mocks `app.core.brain.settings` no
    longer supplies the key — the snapshot reads `app.config.settings`.

    Setting the environment-variable field is deliberately how this is
    done: it is the real precedence rule (`effective_api_key` prefers
    ANTHROPIC_API_KEY over the credential store), so a test using this
    exercises the production read path rather than a stub of it.
    """
    from app.config import settings
    from app.core.ai import credential_view

    previous = settings.anthropic_api_key
    settings.anthropic_api_key = key
    credential_view.invalidate()
    try:
        yield
    finally:
        settings.anthropic_api_key = previous
        credential_view.invalidate()
