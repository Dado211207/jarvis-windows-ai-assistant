"""Integration tests for the v0.2 CSRF/mutation session token, proven
against the real app (TestClient) — not just the token store in
isolation (see tests/test_session.py for that).

Covers: REST mutations reject missing/wrong/expired tokens and accept a
valid one; the cookie has the right attributes (SameSite=Strict, not
HttpOnly); a browser refresh (a second client reusing the same cookie)
keeps working; the WebSocket handshake independently enforces the same
token; the token is never logged.
"""

import logging

import pytest

from app.api.session import COOKIE_NAME, HEADER_NAME, session_tokens


@pytest.fixture(scope="module")
def raw_client():
    """An UN-primed client — deliberately does not call prime_session(),
    since these tests are specifically about what happens without it."""
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield client


@pytest.fixture(autouse=True)
def reset_pending_store():
    from app.core.pending_actions import pending_store
    with pending_store._lock:
        pending_store._actions.clear()
    yield
    with pending_store._lock:
        pending_store._actions.clear()


# --- the cookie itself ---

def test_get_request_sets_a_session_cookie(raw_client):
    r = raw_client.get("/health")
    assert COOKIE_NAME in r.cookies


def test_session_cookie_is_samesite_strict_and_not_httponly():
    """The middleware only sends Set-Cookie when the client doesn't
    already have a current, matching one — a fresh client (never seen
    before) is required to actually observe the header fire."""
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as fresh_client:
        r = fresh_client.get("/health")
    set_cookie_header = r.headers.get("set-cookie", "")
    assert "samesite=strict" in set_cookie_header.lower()
    assert "httponly" not in set_cookie_header.lower()


def test_repeated_requests_keep_the_same_cookie_value(raw_client):
    """Browser refresh must keep working: a client that already holds a
    valid cookie should see the exact same value on the next request, not
    get silently rotated out from under it."""
    raw_client.get("/health")
    first = raw_client.cookies.get(COOKIE_NAME)
    raw_client.get("/health")
    second = raw_client.cookies.get(COOKIE_NAME)
    assert first == second


# --- REST mutations: missing / wrong / valid token ---

def test_command_without_any_token_is_rejected(raw_client):
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    # A client that has never made a GET at all has no cookie and sends
    # no header — the absolute "absent" case.
    with TestClient(jarvis_app, raise_server_exceptions=True) as fresh_client:
        r = fresh_client.post("/command", json={"command": "status"})
    assert r.status_code == 403


def test_command_with_wrong_token_is_rejected(raw_client):
    raw_client.get("/health")  # primes the cookie
    r = raw_client.post(
        "/command",
        json={"command": "status"},
        headers={HEADER_NAME: "totally-made-up-token-value"},
    )
    assert r.status_code == 403


def test_command_with_cookie_but_no_header_is_rejected(raw_client):
    """Double-submit means the cookie alone is not enough — the header
    must also be explicitly attached, which only our own page's JS would
    naturally do."""
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        client.get("/health")  # cookie is now set and will auto-attach
        # deliberately do NOT set the header
        r = client.post("/command", json={"command": "status"})
    assert r.status_code == 403


def test_command_with_matching_valid_token_succeeds(raw_client):
    raw_client.get("/health")
    token = raw_client.cookies.get(COOKIE_NAME)
    r = raw_client.post(
        "/command",
        json={"command": "status"},
        headers={HEADER_NAME: token},
    )
    assert r.status_code == 200


def test_command_with_expired_token_is_rejected(raw_client, monkeypatch):
    raw_client.get("/health")
    token = raw_client.cookies.get(COOKIE_NAME)

    # Force the store to treat every token as expired without waiting a
    # real 24 hours.
    monkeypatch.setattr(session_tokens, "_ttl_seconds", -1)

    r = raw_client.post(
        "/command",
        json={"command": "status"},
        headers={HEADER_NAME: token},
    )
    assert r.status_code == 403


def test_actions_confirm_requires_valid_token(raw_client):
    from tests.conftest import prime_session
    prime_session(raw_client)

    create = raw_client.post("/command", json={"command": "clear logs"})
    action_id = create.json()["pending_action_id"]

    # Per-call header override beats the client default set by
    # prime_session, simulating a forged request that lacks a real token
    # — without permanently clobbering raw_client's own valid header.
    r = raw_client.post(f"/actions/{action_id}/confirm", headers={HEADER_NAME: ""})
    assert r.status_code == 403


def test_actions_cancel_requires_valid_token(raw_client):
    from tests.conftest import prime_session
    prime_session(raw_client)

    create = raw_client.post("/command", json={"command": "clear logs"})
    action_id = create.json()["pending_action_id"]

    r = raw_client.post(f"/actions/{action_id}/cancel", headers={HEADER_NAME: ""})
    assert r.status_code == 403


def test_health_never_requires_a_session_token(raw_client):
    """Health is the bootstrap that issues the token, so it must stay open."""
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as fresh_client:
        r = fresh_client.get("/health")
    assert r.status_code == 200


# --- WebSocket: same token, independently enforced ---

def test_ws_connect_without_cookie_is_rejected():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as fresh_client:
        with pytest.raises(WebSocketDisconnect):
            with fresh_client.websocket_connect("/ws/events"):
                pass


def test_ws_connect_with_valid_cookie_succeeds(raw_client):
    raw_client.get("/health")  # primes the cookie the WS handshake will carry
    with raw_client.websocket_connect("/ws/events") as ws:
        first = ws.receive_text()
    assert first  # connection succeeded and produced the snapshot event


# --- the token must never be logged ---

def test_session_token_never_appears_in_logs(raw_client, caplog):
    raw_client.get("/health")
    token = raw_client.cookies.get(COOKIE_NAME)

    with caplog.at_level(logging.DEBUG):
        raw_client.post("/command", json={"command": "status"}, headers={HEADER_NAME: token})
        raw_client.post("/command", json={"command": "status"}, headers={HEADER_NAME: "wrong-token-value-xyz"})

    assert token not in caplog.text
    assert "wrong-token-value-xyz" not in caplog.text

# --- Host validation and sensitive reads ---

def _canonical_base_url() -> str:
    from app.config import settings
    return f"http://127.0.0.1:{settings.jarvis_port}"


def test_dns_rebinding_host_is_rejected_before_cookie_issuance():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app

    with TestClient(jarvis_app, base_url=_canonical_base_url(), raise_server_exceptions=True) as client:
        response = client.get(
            "/health",
            headers={"host": "attacker.example:5555"},
        )

    assert response.status_code == 400
    assert "set-cookie" not in response.headers
    assert COOKIE_NAME not in response.cookies


def test_dns_rebinding_host_cannot_reach_a_sensitive_get_even_with_fabricated_tokens():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app

    guessed = "attacker-controlled-token"
    with TestClient(jarvis_app, base_url=_canonical_base_url(), raise_server_exceptions=True) as client:
        response = client.get(
            "/logs",
            headers={
                "host": "attacker.example:5555",
                "cookie": f"{COOKIE_NAME}={guessed}",
                HEADER_NAME: guessed,
            },
        )

    # Host validation runs before routing/session validation and must not
    # refresh the attacker's cookie.
    assert response.status_code == 400
    assert "set-cookie" not in response.headers


def test_noncanonical_host_variants_never_receive_a_session_cookie():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app
    from app.config import settings

    hostile_hosts = (
        f"attacker.example:{settings.jarvis_port}",
        f"localhost.attacker.example:{settings.jarvis_port}",
        f"127.0.0.1:{settings.jarvis_port}@attacker.example",
        f"127.0.0.1:{settings.jarvis_port + 1}",
        "localhost",
        "",
    )
    for host in hostile_hosts:
        with TestClient(
            jarvis_app, base_url=_canonical_base_url(), raise_server_exceptions=True,
        ) as client:
            response = client.get("/health", headers={"host": host})
        assert response.status_code == 400, host
        assert "set-cookie" not in response.headers, host


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_canonical_hosts_preserve_health_bootstrap(host):
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    from app.config import settings

    with TestClient(jarvis_app, base_url=_canonical_base_url(), raise_server_exceptions=True) as client:
        response = client.get("/health", headers={"host": f"{host}:{settings.jarvis_port}"})

    assert response.status_code == 200
    assert COOKIE_NAME in response.cookies


@pytest.mark.parametrize("path", [
    "/conversation",
    "/memory",
    "/memory/search?q=coffee",
    "/logs",
    "/diagnostics",
    "/voice/diagnostics",
    "/privacy/data",
    "/actions/pending",
    "/actions/history",
    "/actions/not-a-real-action",
])
def test_sensitive_gets_reject_a_client_without_the_session_header(path):
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app

    with TestClient(jarvis_app, base_url=_canonical_base_url(), raise_server_exceptions=True) as client:
        response = client.get(path)

    assert response.status_code == 403


@pytest.mark.parametrize("path, expected", [
    ("/conversation", 200),
    ("/memory", 200),
    ("/memory/search?q=coffee", 200),
    ("/logs", 200),
    ("/diagnostics", 200),
    ("/voice/diagnostics", 200),
    ("/privacy/data", 200),
    ("/actions/pending", 200),
    ("/actions/history", 200),
    ("/actions/not-a-real-action", 404),
])
def test_dashboard_session_can_read_sensitive_gets(path, expected):
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session

    with TestClient(jarvis_app, base_url=_canonical_base_url(), raise_server_exceptions=True) as client:
        prime_session(client)
        response = client.get(path)

    assert response.status_code == expected


def test_onboarding_and_health_boot_without_a_session_header():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app

    with TestClient(jarvis_app, base_url=_canonical_base_url(), raise_server_exceptions=True) as client:
        assert client.get("/health").status_code == 200
        # The cookie now exists, but deliberately no echo header: these
        # routes must stay usable before dashboard JavaScript is ready.
        assert client.get("/onboarding/readiness").status_code == 200
        assert client.get("/ui/setup").status_code == 200


# ---------------------------------------------------------------------------
# The Host allowlist must follow the port the server really bound
# ---------------------------------------------------------------------------

def test_a_loopback_host_on_the_real_bound_port_is_accepted():
    """`allowed_hosts()` is built from the *configured* port, but the
    server does not always serve on it: `start_server_in_background`
    takes an explicit port, and the screenshot, smoke and diagnostic
    scripts under scripts/ all bind an ephemeral one.

    Pinning to the configured port alone rejected a perfectly ordinary
    `Host: 127.0.0.1:50167` — sixteen tests and three errors, none of
    them about security, because the server answered nothing at all.
    """
    from app.api.session import is_allowed_host
    from app.config import settings

    other = settings.jarvis_port + 4242
    # Rejected when we are not actually listening there...
    assert is_allowed_host(f"127.0.0.1:{other}", None, settings.jarvis_port) is False
    # ...and accepted when we are.
    for hostname in ("127.0.0.1", "localhost", "[::1]"):
        assert is_allowed_host(f"{hostname}:{other}", None, other) is True


def test_the_real_bound_port_does_not_admit_a_rebinding_hostname():
    """The second rule keys on the *hostname* being a loopback literal.
    A rebound attacker keeps their own hostname in Host, so knowing the
    port buys them nothing."""
    from app.api.session import is_allowed_host

    port = 50167
    for host in (
        f"attacker.example:{port}",
        f"localhost.attacker.example:{port}",
        f"127.0.0.1.attacker.example:{port}",
        f"127.0.0.1:{port}@attacker.example",
        f"[::1].attacker.example:{port}",
    ):
        assert is_allowed_host(host, None, port) is False, host


def test_without_a_known_server_port_only_the_configured_allowlist_applies():
    """No `scope["server"]` (some ASGI transports omit it) must not open
    the door — it falls back to exactly the previous behaviour."""
    from app.api.session import is_allowed_host
    from app.config import settings

    assert is_allowed_host(f"127.0.0.1:{settings.jarvis_port}", None, None) is True
    assert is_allowed_host("127.0.0.1:50167", None, None) is False
