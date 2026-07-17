"""Tests for the default-private GET classification and exact-active-port
Host/Origin pinning in app/api/local_guard.py — the two pieces added on top
of the state-changing-request protection from the previous hardening pass.

Every private GET below is called with an explicitly WRONG or MISSING token
(overriding the tests/conftest.py shim that would otherwise auto-attach a
valid one) to prove the protection is real, not just present-but-inert.
"""

from unittest.mock import patch

import pytest

from app.api import local_guard
from app.core import session_token


@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield client


# --- central classification: is_public_path ---

@pytest.mark.parametrize("path", [
    "/health",
    "/ui/",
    "/ui/dashboard",
    "/ui/chat",
    "/ui/settings",
    "/ui/memory",
    "/ui/diagnostics",
    "/ui/onboarding",
    "/ui/static/style.css",
    "/ui/static/app.js",
    "/docs",
    "/redoc",
    "/openapi.json",
])
def test_is_public_path_true_for_bootstrap_surface(path):
    assert local_guard.is_public_path(path) is True


@pytest.mark.parametrize("path", [
    "/", "/system", "/conversation", "/tools", "/memory", "/memory/search",
    "/logs", "/voice/status", "/settings", "/settings/defaults",
    "/preferences", "/preferences/search", "/actions/pending",
    "/actions/some-id", "/onboarding/state", "/diagnostics", "/update/check",
])
def test_is_public_path_false_for_private_data(path):
    assert local_guard.is_public_path(path) is False


# --- every private GET actually rejects a missing/wrong token ---

PRIVATE_GET_PATHS = [
    "/", "/system", "/conversation", "/tools", "/memory", "/memory/search?q=x",
    "/logs", "/voice/status", "/settings", "/settings/defaults",
    "/preferences", "/preferences/search?q=x", "/actions/pending",
    "/onboarding/state", "/diagnostics", "/update/check",
]


@pytest.mark.parametrize("path", PRIVATE_GET_PATHS)
def test_private_get_without_token_rejected(api_client, path):
    r = api_client.get(path, headers={"X-Jarvis-Token": ""})
    assert r.status_code == 401, f"{path} did not require a token"


@pytest.mark.parametrize("path", PRIVATE_GET_PATHS)
def test_private_get_with_wrong_token_rejected(api_client, path):
    r = api_client.get(path, headers={"X-Jarvis-Token": "not-the-real-token"})
    assert r.status_code == 401, f"{path} accepted an incorrect token"


@pytest.mark.parametrize("path", PRIVATE_GET_PATHS)
def test_private_get_with_valid_token_succeeds(api_client, path):
    token = session_token.get_token()
    r = api_client.get(path, headers={"X-Jarvis-Token": token})
    assert r.status_code == 200, f"{path} rejected the real current token"


def test_no_private_endpoint_accidentally_public():
    """Enumerates every @router.get(...) path registered in the app's own
    OpenAPI schema and asserts each one is covered by the classification —
    catches a new route being added without anyone updating local_guard.py."""
    from app.api.server import app as jarvis_app

    known_intentionally_public = {"/health", "/openapi.json"}
    for route in jarvis_app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path or "GET" not in methods:
            continue
        if path.startswith("/ui") or path in ("/docs", "/redoc"):
            continue  # UI shells and API docs are deliberately public — covered above
        if path in known_intentionally_public:
            continue
        assert local_guard.is_public_path(path) is False, (
            f"{path} is a GET route not covered by the private-by-default classification"
        )


# --- health stays reachable without a token, and reveals nothing else ---

def test_health_reachable_without_token(api_client):
    r = api_client.get("/health", headers={"X-Jarvis-Token": ""})
    assert r.status_code == 200


def test_health_reachable_with_wrong_token_too(api_client):
    """A wrong token must not make health WORSE — it's public regardless."""
    r = api_client.get("/health", headers={"X-Jarvis-Token": "garbage"})
    assert r.status_code == 200


# --- Cache-Control / Pragma on token-bearing responses ---

def test_private_endpoint_response_is_not_cacheable(api_client):
    token = session_token.get_token()
    r = api_client.get("/settings", headers={"X-Jarvis-Token": token})
    assert r.headers.get("cache-control") == "no-store, private"
    assert r.headers.get("pragma") == "no-cache"


def test_ui_bootstrap_page_response_is_not_cacheable(api_client):
    r = api_client.get("/ui/")
    assert r.headers.get("cache-control") == "no-store, private"
    assert r.headers.get("pragma") == "no-cache"


def test_health_response_has_no_no_store_requirement(api_client):
    """Public, non-token-bearing — no reason to force no-store (though it's
    harmless if a future change adds it; this just documents the current,
    deliberately looser policy for the public endpoint)."""
    r = api_client.get("/health")
    assert r.status_code == 200


def test_static_asset_is_not_forced_no_store(api_client):
    r = api_client.get("/ui/static/style.css")
    assert r.headers.get("cache-control") != "no-store, private"


# --- exact-active-port pinning (Host + Origin) ---

@pytest.fixture
def _pinned_port():
    with patch("app.api.local_guard._expected_port", return_value=5555):
        yield 5555


def test_host_with_correct_port_allowed(_pinned_port):
    assert local_guard.is_allowed_host("127.0.0.1:5555") is True


def test_host_with_wrong_port_rejected(_pinned_port):
    assert local_guard.is_allowed_host("127.0.0.1:9999") is False


def test_host_with_no_port_rejected_when_port_is_pinned(_pinned_port):
    assert local_guard.is_allowed_host("127.0.0.1") is False


def test_origin_with_correct_port_allowed(_pinned_port):
    assert local_guard.is_allowed_origin("http://127.0.0.1:5555") is True


def test_origin_with_wrong_port_rejected(_pinned_port):
    assert local_guard.is_allowed_origin("http://127.0.0.1:6000") is False


def test_end_to_end_request_from_wrong_port_origin_rejected(api_client, _pinned_port):
    token = session_token.get_token()
    r = api_client.get(
        "/settings",
        headers={"X-Jarvis-Token": token, "Origin": "http://127.0.0.1:6000"},
    )
    assert r.status_code == 403


def test_end_to_end_request_from_correct_port_origin_succeeds(api_client, _pinned_port):
    token = session_token.get_token()
    r = api_client.get(
        "/settings",
        headers={"X-Jarvis-Token": token, "Origin": "http://127.0.0.1:5555"},
    )
    assert r.status_code == 200


# --- malicious lookalike origins / hosts ---

@pytest.mark.parametrize("origin", [
    "http://127.0.0.1.attacker.example",
    "http://localhost.attacker.example",
    "http://attacker.example/?localhost",
    "http://attacker.example?127.0.0.1",
    "null",
    "",
    "file:///etc/passwd",
    "http://127.0.0.1:5555.attacker.example",
    "http://[::1]",
    "https://127.0.0.1",  # wrong scheme
])
def test_lookalike_origins_all_rejected(origin):
    assert local_guard.is_allowed_origin(origin) is False


@pytest.mark.parametrize("host", [
    "127.0.0.1.attacker.example",
    "localhost.attacker.example",
    "attacker.example",
    "[::1]",
    "[::1]:5555",
    "0.0.0.0",
    "0.0.0.0:5555",
    "",
    "127.0.0.1:5555:6000",
])
def test_lookalike_hosts_all_rejected(host):
    assert local_guard.is_allowed_host(host) is False


def test_ipv6_loopback_not_supported(api_client):
    r = api_client.get("/health", headers={"Host": "[::1]:5555"})
    assert r.status_code == 400


def test_malicious_lookalike_origin_end_to_end_rejected(api_client):
    token = session_token.get_token()
    r = api_client.get(
        "/settings",
        headers={"X-Jarvis-Token": token, "Origin": "http://127.0.0.1.attacker.example"},
    )
    assert r.status_code == 403


# --- CORS preflight restrictions ---

def test_preflight_advertises_only_needed_methods(api_client):
    r = api_client.options(
        "/settings",
        headers={
            "Origin": "http://127.0.0.1:5555",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-jarvis-token",
        },
    )
    allowed = r.headers.get("access-control-allow-methods", "")
    for m in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        assert m in allowed
    assert "TRACE" not in allowed
    assert "CONNECT" not in allowed


def test_preflight_never_allows_credentials(api_client):
    r = api_client.options(
        "/settings",
        headers={
            "Origin": "http://127.0.0.1:5555",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-credentials" not in {k.lower() for k in r.headers.keys()}


def test_preflight_has_bounded_max_age(api_client):
    r = api_client.options(
        "/settings",
        headers={
            "Origin": "http://127.0.0.1:5555",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.headers.get("access-control-max-age") == "600"
