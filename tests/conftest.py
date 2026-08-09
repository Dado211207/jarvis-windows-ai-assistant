"""Shared pytest helpers across the test suite."""

import pytest

SESSION_TOKEN_HEADER = "X-JARVIS-Session-Token"


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    """Every test gets its own preferences file.

    Autouse and unconditional: app/core/preferences.py is written to by
    ordinary product code (turning on spoken replies, picking an AI
    provider), so without this a test run would edit the developer's real
    settings, and — worse — one test's choice would silently change what
    the next test observes. Redirecting only the preferences module's own
    reference leaves app_paths' own tests measuring the real thing.
    """
    monkeypatch.setattr("app.core.preferences.config_dir", lambda: tmp_path)
    yield tmp_path


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
