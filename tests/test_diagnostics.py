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
