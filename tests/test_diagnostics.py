"""Tests for the Diagnostics feature (app/core/diagnostics.py, its API routes,
and the /ui/diagnostics page).

Never touches production AppData paths — get_report() reads dev-mode paths
(the existing tests/data setup) exactly like every other API test in this
suite; is_frozen() stays False throughout.
"""

from unittest.mock import patch

import pytest

from app.core import diagnostics


def test_get_report_shape():
    report = diagnostics.get_report()
    assert "version" in report
    assert "phase" in report
    assert "os" in report
    assert "paths" in report
    assert "database" in report
    assert "brain" in report
    assert "secure_storage" in report
    assert "onboarding" in report
    assert "migration" in report
    assert "tools_registered" in report


def test_get_report_never_contains_secrets():
    report = diagnostics.get_report()
    import json
    text = json.dumps(report)
    assert "ANTHROPIC_API_KEY" not in text
    assert "sk-ant-" not in text
    assert "sk-" not in text


def test_get_report_frozen_false_in_dev():
    report = diagnostics.get_report()
    assert report["frozen"] is False


def test_get_report_reflects_actual_port_when_set():
    from app.core import runtime_state
    runtime_state.set_actual_port(12345)
    try:
        report = diagnostics.get_report()
        assert report["actual_port"] == 12345
    finally:
        runtime_state.set_actual_port(None)


def test_get_report_migration_none_when_never_run():
    with patch("app.core.migration.get_marker", return_value=None):
        report = diagnostics.get_report()
    assert report["migration"] is None


def test_open_logs_folder_success(tmp_path):
    with patch("app.core.diagnostics.paths.logs_dir", return_value=tmp_path), \
         patch("app.core.diagnostics.subprocess.Popen") as mock_popen:
        result = diagnostics.open_logs_folder()
    assert result["success"] is True
    mock_popen.assert_called_once()


def test_open_logs_folder_failure_is_reported_not_raised(tmp_path):
    with patch("app.core.diagnostics.paths.logs_dir", return_value=tmp_path), \
         patch("app.core.diagnostics.subprocess.Popen", side_effect=OSError("no file manager")):
        result = diagnostics.open_logs_folder()
    assert result["success"] is False


def test_open_logs_folder_failure_message_is_redacted(tmp_path):
    with patch("app.core.diagnostics.paths.logs_dir", return_value=tmp_path), \
         patch(
             "app.core.diagnostics.subprocess.Popen",
             side_effect=OSError(r"cannot launch: C:\Users\JohnDoe\explorer.exe"),
         ):
        result = diagnostics.open_logs_folder()
    assert result["success"] is False
    assert "JohnDoe" not in result["message"]


# --- API routes ---

@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield client


def test_diagnostics_endpoint_returns_200(api_client):
    r = api_client.get("/diagnostics")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body


def test_diagnostics_endpoint_no_secrets(api_client):
    r = api_client.get("/diagnostics")
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-" not in r.text


def test_diagnostics_open_logs_folder_endpoint(api_client):
    with patch("app.core.diagnostics.subprocess.Popen") as mock_popen:
        r = api_client.post("/diagnostics/open-logs-folder")
    assert r.status_code == 200
    assert r.json()["success"] is True
    mock_popen.assert_called_once()


# --- UI page ---

def test_ui_diagnostics_page_returns_200(api_client):
    r = api_client.get("/ui/diagnostics")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_ui_diagnostics_no_secrets(api_client):
    r = api_client.get("/ui/diagnostics")
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-" not in r.text


def test_ui_diagnostics_nav_link_present(api_client):
    r = api_client.get("/ui/")
    assert "/ui/diagnostics" in r.text


def test_ui_diagnostics_js_uses_textcontent_not_innerhtml(api_client):
    r = api_client.get("/ui/static/diagnostics.js")
    assert r.status_code == 200
    assert "innerHTML" not in r.text


def test_ui_diagnostics_redirects_when_onboarding_required(api_client):
    with patch("app.ui.routes.onboarding.is_required", return_value=True):
        r = api_client.get("/ui/diagnostics", follow_redirects=False)
    assert r.status_code in (302, 307)


# --- redact_text(): the redacted, copy/paste-safe rendering ---
# Privacy audit (Section 7): the raw get_report() dict is fine to *display*
# on the Diagnostics page itself (real paths are useful for the user's own
# troubleshooting on their own machine), but get_report_text() — what the
# "Copy report" button actually copies — must never let a Windows username,
# an API key, a bearer token, an Authorization header, or an email address
# reach whatever the user pastes it into next.

def test_redact_text_strips_windows_username_from_path():
    text = r"Database path: C:\Users\JohnDoe\AppData\Roaming\JARVIS\jarvis.db"
    result = diagnostics.redact_text(text)
    assert "JohnDoe" not in result
    assert r"C:\Users\<user>\AppData\Roaming\JARVIS\jarvis.db" in result


def test_redact_text_strips_unix_home_username_from_path():
    text = "Log folder: /home/johndoe/.local/share/jarvis/logs"
    result = diagnostics.redact_text(text)
    assert "johndoe" not in result
    assert "/home/<user>/.local/share/jarvis/logs" in result


def test_redact_text_strips_anthropic_style_api_key():
    text = "Legacy DB migration: source used key sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
    result = diagnostics.redact_text(text)
    assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in result
    assert "<redacted-api-key>" in result


def test_redact_text_strips_generic_sk_style_key():
    text = "error: rejected key sk-abcdefghijklmnopqrstuvwxyz"
    result = diagnostics.redact_text(text)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in result
    assert "<redacted-api-key>" in result


def test_redact_text_strips_bearer_token():
    text = "request failed: Authorization header was Bearer abc123.def456-ghi789"
    result = diagnostics.redact_text(text)
    assert "abc123.def456-ghi789" not in result
    assert "Bearer <redacted>" in result


def test_redact_text_strips_authorization_header_line():
    text = "headers: {'Authorization': 'Basic dXNlcjpwYXNz', 'Host': '127.0.0.1'}"
    result = diagnostics.redact_text(text)
    assert "dXNlcjpwYXNz" not in result


def test_redact_text_strips_email_address():
    text = "reported by doncicdragan2112@gmail.com during setup"
    result = diagnostics.redact_text(text)
    assert "doncicdragan2112@gmail.com" not in result
    assert "<redacted-email>" in result


def test_redact_text_handles_multiline_exception_with_embedded_path():
    """Redaction must not be line-anchored — a real Python traceback is
    exactly this shape: multiple lines, with a username-bearing path buried
    partway through."""
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\Users\\JohnDoe\\AppData\\Roaming\\JARVIS\\db.py", line 12\n'
        "    raise OSError('permission denied')\n"
        "OSError: permission denied"
    )
    result = diagnostics.redact_text(text)
    assert "JohnDoe" not in result
    assert "OSError: permission denied" in result  # rest of the traceback is preserved


def test_redact_text_leaves_html_js_like_values_unmangled():
    """Not a targeted category — must not crash or get corrupted by the
    redaction passes (defends against a future field accidentally
    containing markup, e.g. from a malformed path)."""
    text = '<script>alert(1)</script> onerror="alert(2)"'
    result = diagnostics.redact_text(text)
    assert result == text


def test_redact_text_passthrough_for_none_and_empty():
    assert diagnostics.redact_text(None) is None
    assert diagnostics.redact_text("") == ""


def test_redact_text_leaves_ordinary_text_unchanged():
    text = "Version: 0.1.7-alpha (Phase 8), tools_registered: 12"
    assert diagnostics.redact_text(text) == text


# --- get_report_text(): the assembled, redacted report ---

def test_get_report_text_redacts_username_in_db_and_log_paths(monkeypatch):
    # db_path/log_file are read-only @property wrappers around these plain
    # string fields (app/config.py) — set the underlying fields directly,
    # the same pattern tests/test_onboarding.py's _settings_db fixture uses.
    from app.config import settings as app_settings

    monkeypatch.setattr(
        app_settings, "jarvis_db_path", r"C:\Users\JohnDoe\AppData\Roaming\JARVIS\jarvis.db"
    )
    monkeypatch.setattr(
        app_settings,
        "jarvis_log_file",
        r"C:\Users\JohnDoe\AppData\Roaming\JARVIS\logs\jarvis.log",
    )
    text = diagnostics.get_report_text()
    assert "JohnDoe" not in text
    assert "<user>" in text


def test_get_report_text_redacts_migration_marker_source_and_error():
    marker = {
        "status": "failed",
        "source": r"C:\Users\JohnDoe\Documents\old\data\jarvis.db",
        "error": "PermissionError: [Errno 13] Permission denied: 'C:\\Users\\JohnDoe\\data\\jarvis.db'",
    }
    with patch("app.core.migration.get_marker", return_value=marker):
        text = diagnostics.get_report_text()
    assert "JohnDoe" not in text


def test_get_report_text_never_contains_secrets():
    text = diagnostics.get_report_text()
    assert "ANTHROPIC_API_KEY" not in text
    assert "sk-ant-" not in text
    assert "sk-" not in text


def test_get_report_text_starts_with_report_header():
    text = diagnostics.get_report_text()
    assert text.startswith("JARVIS Diagnostics Report")


# --- /diagnostics/report-text API route ---

def test_diagnostics_report_text_endpoint_returns_200(api_client):
    r = api_client.get("/diagnostics/report-text")
    assert r.status_code == 200
    body = r.json()
    assert "text" in body
    assert body["text"].startswith("JARVIS Diagnostics Report")


def test_diagnostics_report_text_endpoint_requires_token(api_client):
    r = api_client.get("/diagnostics/report-text", headers={"X-Jarvis-Token": ""})
    assert r.status_code == 401


def test_diagnostics_report_text_endpoint_no_secrets(api_client):
    r = api_client.get("/diagnostics/report-text")
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-" not in r.text
