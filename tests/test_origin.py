"""Tests for app/api/origin.py — the local-origin allowlist shared by
CORS middleware and the WebSocket handshake."""

from unittest.mock import patch

from app.api.origin import allowed_origins, is_allowed_origin


def test_allowed_origins_includes_127_0_0_1_and_localhost():
    origins = allowed_origins()
    assert any(o.startswith("http://127.0.0.1:") for o in origins)
    assert any(o.startswith("http://localhost:") for o in origins)


def test_allowed_origins_uses_configured_port():
    with patch("app.api.origin.settings") as mock_settings:
        mock_settings.jarvis_port = 9999
        origins = allowed_origins()
    assert "http://127.0.0.1:9999" in origins
    assert "http://localhost:9999" in origins


def test_is_allowed_origin_accepts_own_origin():
    origin = allowed_origins()[0]
    assert is_allowed_origin(origin) is True


def test_is_allowed_origin_rejects_foreign_origin():
    assert is_allowed_origin("http://evil.example.com") is False


def test_is_allowed_origin_rejects_foreign_origin_on_same_port():
    """A malicious page can't just match the port number — the scheme+host
    must be exactly the loopback address."""
    from app.config import settings
    assert is_allowed_origin(f"http://attacker.example.com:{settings.jarvis_port}") is False


def test_is_allowed_origin_allows_missing_origin():
    """No Origin header (non-browser tools, curl, direct scripts) is
    allowed — a browser cannot forge a cross-origin WS handshake without
    sending a real, foreign Origin, so only a *present but foreign* value
    is actually a threat."""
    assert is_allowed_origin(None) is True
