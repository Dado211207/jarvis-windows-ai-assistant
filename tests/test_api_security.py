"""Tests for the local API request-protection layer:
- app/core/session_token.py (per-launch random token)
- app/api/local_guard.py (Host/Origin allowlist + token enforcement + native
  CORS, anchored to the exact active port — see test_private_endpoint_protection.py
  for the default-private GET classification and exact-port pinning tests)

See docs/SECURITY.md for the full threat model and exact boundaries. These
tests intentionally issue raw requests with explicit headers (bypassing the
conftest.py shim that auto-attaches a valid token) to exercise the actual
rejection paths a real attacker would hit.
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


# --- session_token.py ---

def test_token_is_generated_and_nonempty():
    token = session_token.generate_token()
    assert isinstance(token, str)
    assert len(token) >= 32


def test_token_has_at_least_256_bits_of_entropy():
    """generate_token() must use secrets.token_urlsafe(32) — 32 random bytes
    (256 bits) — not just a long-looking string. Decodes the actual
    base64url payload back to bytes and checks its length directly, rather
    than trusting the string length (a weaker/shorter encoding could still
    produce a long string)."""
    import base64

    token = session_token.generate_token()
    padded = token + "=" * (-len(token) % 4)
    raw = base64.urlsafe_b64decode(padded)
    assert len(raw) * 8 >= 256


def test_token_uses_cryptographic_rng_not_arbitrary_random():
    """generate_token() must call secrets.token_urlsafe specifically (a
    CSPRNG), not random/uuid or anything seedable — patches the exact call
    site to prove it's what's actually used, not just plausible-looking
    output."""
    with patch("app.core.session_token.secrets.token_urlsafe") as mock_gen:
        mock_gen.return_value = "fake-token-value"
        result = session_token.generate_token()
    mock_gen.assert_called_once_with(32)
    assert result == "fake-token-value"


def test_token_changes_every_time_it_is_generated():
    first = session_token.generate_token()
    second = session_token.generate_token()
    assert first != second


def test_is_valid_true_for_current_token():
    token = session_token.generate_token()
    assert session_token.is_valid(token) is True


def test_is_valid_false_for_wrong_token():
    session_token.generate_token()
    assert session_token.is_valid("not-the-token") is False


def test_is_valid_false_for_empty_or_none():
    session_token.generate_token()
    assert session_token.is_valid("") is False
    assert session_token.is_valid(None) is False


def test_token_value_never_appears_in_logs(api_client, caplog):
    """Rejections are the highest-risk moment for a token to leak into logs
    (an unwary implementation might log "expected X got Y") — hit several
    rejection paths with a *wrong* token, and confirm the real one never
    shows up in anything captured at any log level."""
    real_token = session_token.get_token()
    with caplog.at_level("DEBUG"):
        api_client.get("/settings", headers={"X-Jarvis-Token": "attacker-guess"})
        api_client.post(
            "/command",
            json={"command": "status"},
            headers={"X-Jarvis-Token": "another-wrong-value"},
        )
        api_client.get(
            "/settings",
            headers={"X-Jarvis-Token": real_token, "Origin": "http://evil.example"},
        )
    for record in caplog.records:
        assert real_token not in record.getMessage()


def test_is_valid_false_when_no_token_ever_generated():
    session_token._token = ""  # simulate a fresh, never-started process
    assert session_token.is_valid("anything") is False


# --- local_guard.py pure functions ---

@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True),
    ("127.0.0.1:5555", True),
    ("localhost", True),
    ("localhost:8080", True),
    ("testserver", True),  # Starlette TestClient's fixed default — see module docstring
    ("evil.com", False),
    ("evil.com:5555", False),
    ("", False),
    ("127.0.0.1.evil.com", False),
])
def test_is_allowed_host(host, expected):
    assert local_guard.is_allowed_host(host) is expected


@pytest.mark.parametrize("origin,expected", [
    ("http://127.0.0.1", True),
    ("http://127.0.0.1:5555", True),
    ("http://localhost:5555", True),
    ("https://127.0.0.1:5555", False),   # wrong scheme
    ("http://evil.com", False),
    ("http://evil.com:5555", False),
    ("null", False),
    ("file://", False),
    ("", False),
])
def test_is_allowed_origin(origin, expected):
    assert local_guard.is_allowed_origin(origin) is expected


# --- integration: trusted request succeeds ---

def test_trusted_request_with_valid_token_succeeds(api_client):
    token = session_token.get_token()
    r = api_client.post(
        "/command",
        json={"command": "status"},
        headers={"X-Jarvis-Token": token, "Origin": "http://127.0.0.1:5555"},
    )
    assert r.status_code == 200
    assert r.json()["success"] is True


# --- missing / incorrect token fails ---

def test_missing_token_rejected(api_client):
    r = api_client.post("/command", json={"command": "status"}, headers={"X-Jarvis-Token": ""})
    assert r.status_code == 401


def test_incorrect_token_rejected(api_client):
    r = api_client.post("/command", json={"command": "status"}, headers={"X-Jarvis-Token": "wrong-token-value"})
    assert r.status_code == 401


def test_incorrect_token_rejected_on_settings_patch(api_client):
    r = api_client.patch("/settings", json={"values": {"assistant_name": "Nope"}}, headers={"X-Jarvis-Token": "wrong"})
    assert r.status_code == 401


def test_incorrect_token_rejected_on_onboarding_complete(api_client):
    r = api_client.post("/onboarding/complete", headers={"X-Jarvis-Token": "wrong"})
    assert r.status_code == 401


def test_incorrect_token_rejected_on_preferences_delete(api_client):
    r = api_client.delete("/preferences/1", headers={"X-Jarvis-Token": "wrong"})
    assert r.status_code == 401


def test_incorrect_token_rejected_on_action_confirm(api_client):
    r = api_client.post("/actions/does-not-exist/confirm", headers={"X-Jarvis-Token": "wrong"})
    assert r.status_code == 401


def test_incorrect_token_rejected_on_diagnostics_open_logs(api_client):
    r = api_client.post("/diagnostics/open-logs-folder", headers={"X-Jarvis-Token": "wrong"})
    assert r.status_code == 401


# --- foreign Origin fails ---

def test_foreign_origin_rejected_even_with_valid_token(api_client):
    token = session_token.get_token()
    r = api_client.post(
        "/command",
        json={"command": "status"},
        headers={"X-Jarvis-Token": token, "Origin": "https://evil.com"},
    )
    assert r.status_code == 403


def test_null_origin_rejected(api_client):
    token = session_token.get_token()
    r = api_client.post(
        "/command",
        json={"command": "status"},
        headers={"X-Jarvis-Token": token, "Origin": "null"},
    )
    assert r.status_code == 403


# --- unexpected Host fails ---

def test_unexpected_host_rejected(api_client):
    r = api_client.get("/health", headers={"Host": "evil.com"})
    assert r.status_code == 400


def test_unexpected_host_rejected_for_state_changing_request(api_client):
    token = session_token.get_token()
    r = api_client.post(
        "/command",
        json={"command": "status"},
        headers={"X-Jarvis-Token": token, "Host": "evil.com"},
    )
    assert r.status_code == 400


# --- no wildcard CORS ---

def test_cors_reflects_specific_origin_not_wildcard(api_client):
    r = api_client.options(
        "/command",
        headers={
            "Origin": "http://127.0.0.1:5555",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-jarvis-token",
        },
    )
    acao = r.headers.get("access-control-allow-origin")
    assert acao is not None
    assert acao != "*"
    assert acao == "http://127.0.0.1:5555"


def test_cors_rejects_foreign_origin_preflight(api_client):
    r = api_client.options(
        "/command",
        headers={
            "Origin": "https://evil.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-jarvis-token",
        },
    )
    assert r.headers.get("access-control-allow-origin") is None


def test_no_route_ever_sets_wildcard_cors_header(api_client):
    # Even for an allowed same-origin request, the reflected value must
    # never be the literal wildcard.
    r = api_client.get("/health", headers={"Origin": "http://127.0.0.1:5555"})
    assert r.headers.get("access-control-allow-origin") != "*"


# --- token changes after restart ---

def test_token_differs_across_separate_app_lifespans():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app

    with TestClient(jarvis_app):
        first = session_token.get_token()
    with TestClient(jarvis_app):
        second = session_token.get_token()
    assert first != second
    assert first and second


# --- token is never logged ---

def test_token_never_appears_in_logs(api_client, caplog):
    caplog.set_level("DEBUG")
    token = session_token.get_token()
    api_client.post("/command", json={"command": "status"}, headers={"X-Jarvis-Token": token})
    api_client.post("/command", json={"command": "status"}, headers={"X-Jarvis-Token": "wrong-value-xyz"})
    assert token not in caplog.text


# --- read-only health check remains usable ---

def test_health_check_works_without_any_token(api_client):
    r = api_client.get("/health", headers={"X-Jarvis-Token": ""})
    assert r.status_code == 200


def test_health_check_works_with_no_origin_header(api_client):
    r = api_client.get("/health")
    assert r.status_code == 200


# --- CLI path remains functional (no HTTP layer involved at all) ---

def test_cli_brain_process_bypasses_http_entirely():
    from app.core.brain import brain
    response = brain.process("status")
    assert response.success is True


# --- a malicious webpage cannot submit a state-changing request ---

def test_malicious_webpage_simple_request_is_rejected(api_client):
    """Simulates the actual bypass a malicious site would attempt: a
    cross-origin fetch with a CORS-safelisted content type (no preflight
    triggered) and no way to know the real token."""
    r = api_client.post(
        "/command",
        content='{"command": "open notepad"}',
        headers={
            "Content-Type": "text/plain",  # safelisted -> browser sends it without a preflight
            "Origin": "https://evil.com",
            "X-Jarvis-Token": "",  # attacker cannot read this page's token
        },
    )
    assert r.status_code in (401, 403)


def test_malicious_webpage_preflight_never_succeeds(api_client):
    r = api_client.options(
        "/onboarding/complete",
        headers={
            "Origin": "https://evil.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-jarvis-token,content-type",
        },
    )
    assert r.headers.get("access-control-allow-origin") is None


# --- security response headers (app/api/local_guard.py's SECURITY_HEADERS) ---

@pytest.mark.parametrize("path", ["/ui/", "/health", "/settings"])
def test_security_headers_present_on_every_response(api_client, path):
    """Success responses — HTML page shells, the public JSON endpoint, and a
    private JSON endpoint alike — all carry the full header set. One place
    sets these (local_guard.py), so there's no route-by-route opt-in to get
    wrong."""
    r = api_client.get(path)
    assert r.status_code == 200
    for header in local_guard.SECURITY_HEADERS:
        assert header in r.headers, f"{path} is missing {header}"


def test_security_headers_present_on_rejection_responses(api_client):
    """A 401/403/400 rejection is still a response a browser renders (e.g.
    the JSON body in dev tools) — it must be hardened too, not just the
    happy path."""
    r = api_client.get("/settings", headers={"X-Jarvis-Token": "wrong"})
    assert r.status_code == 401
    for header in local_guard.SECURITY_HEADERS:
        assert header in r.headers, f"401 response is missing {header}"


def test_security_headers_present_on_cors_preflight(api_client):
    r = api_client.options(
        "/settings",
        headers={
            "Origin": "http://127.0.0.1:5555",
            "Access-Control-Request-Method": "GET",
        },
    )
    for header in local_guard.SECURITY_HEADERS:
        assert header in r.headers, f"preflight response is missing {header}"


def test_csp_blocks_iframe_embedding_two_ways(api_client):
    """Both the modern (frame-ancestors) and legacy (X-Frame-Options)
    mechanisms are set, so an older engine that ignores CSP is still
    covered."""
    r = api_client.get("/ui/")
    csp = r.headers.get("content-security-policy", "")
    assert "frame-ancestors 'none'" in csp
    assert r.headers.get("x-frame-options") == "DENY"


def test_csp_has_no_unsafe_eval_and_no_wildcard_script_source(api_client):
    r = api_client.get("/ui/")
    csp = r.headers.get("content-security-policy", "")
    assert "unsafe-eval" not in csp
    assert "script-src 'self'" in csp
    # Only style-src carries 'unsafe-inline' (existing inline style="" attrs
    # — see the comment on SECURITY_HEADERS); script-src must never have it.
    script_src_clause = [c.strip() for c in csp.split(";") if c.strip().startswith("script-src")][0]
    assert "unsafe-inline" not in script_src_clause


def test_csp_locks_down_base_uri_object_src_form_action(api_client):
    r = api_client.get("/ui/")
    csp = r.headers.get("content-security-policy", "")
    assert "base-uri 'none'" in csp
    assert "object-src 'none'" in csp
    assert "form-action 'self'" in csp


def test_permissions_policy_denies_microphone_and_camera(api_client):
    """Phase 3 TTS is output-only — JARVIS never requests browser
    microphone/camera access, so both are denied outright, not merely
    restricted to 'self' (see CLAUDE.md's Phase 3 rules)."""
    r = api_client.get("/ui/")
    pp = r.headers.get("permissions-policy", "")
    assert "microphone=()" in pp
    assert "camera=()" in pp


def test_referrer_policy_and_content_type_options(api_client):
    r = api_client.get("/ui/")
    assert r.headers.get("referrer-policy") == "no-referrer"
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_no_csp_reporting_endpoint_configured():
    """No report-uri/report-to directive exists anywhere — so there is no
    channel through which a CSP violation report (which could otherwise
    include page context) is ever sent anywhere, local or remote."""
    csp = local_guard.SECURITY_HEADERS["Content-Security-Policy"]
    assert "report-uri" not in csp
    assert "report-to" not in csp


@pytest.mark.parametrize("path", [
    "/ui/", "/ui/chat", "/ui/actions", "/ui/voice", "/ui/logs",
    "/ui/memory", "/ui/settings", "/ui/help", "/ui/diagnostics", "/ui/onboarding",
])
def test_no_ui_page_has_inline_script_tag(api_client, path):
    """CSP's script-src is 'self' only, no unsafe-inline — every page must
    load JS exclusively via <script src="/ui/static/...">, never an inline
    <script> block (the session token itself moved to a data-* attribute
    for exactly this reason — see templates/base.html)."""
    r = api_client.get(path)
    assert r.status_code == 200
    assert "<script>" not in r.text


@pytest.mark.parametrize("filename", ["app.js", "onboarding.js", "diagnostics.js"])
def test_served_js_has_no_eval_or_document_write(api_client, filename):
    r = api_client.get(f"/ui/static/{filename}")
    assert r.status_code == 200
    js = r.text
    assert "eval(" not in js
    assert "new Function(" not in js
    assert "document.write(" not in js
