"""
Tests for Phase 6: Safe Windows Actions Expansion

Coverage:
- open_website: accepts http/https, adds https when missing, blocks dangerous schemes
- open_dashboard: always uses 127.0.0.1 URL
- open_app: file_explorer and settings added; cmd/powershell NOT allowed
- open_folder: known folders accepted, arbitrary paths blocked
- create_note: writes inside JARVIS notes dir, sanitizes filenames
- disk_space: returns required fields
- network_info: returns hostname, no external requests
- battery_status: handles unavailable gracefully
- command router: all new phrases route to correct tools
- regression: safe actions don't create pending approvals
- regression: clear_logs still requires approval
- no secrets in responses
- help page documents new commands
"""

import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def api_client():
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


# ── open_website ──────────────────────────────────────────────────────────────

def test_open_website_accepts_https():
    from app.desktop.web import open_website
    with patch("webbrowser.open") as mock_open:
        result = open_website("https://github.com")
    assert result["success"] is True
    mock_open.assert_called_once_with("https://github.com")


def test_open_website_accepts_http():
    from app.desktop.web import open_website
    with patch("webbrowser.open") as mock_open:
        result = open_website("http://localhost:8080")
    assert result["success"] is True
    mock_open.assert_called_once_with("http://localhost:8080")


def test_open_website_adds_https_when_missing_scheme():
    from app.desktop.web import open_website
    with patch("webbrowser.open") as mock_open:
        result = open_website("github.com")
    assert result["success"] is True
    call_url = mock_open.call_args[0][0]
    assert call_url.startswith("https://")
    assert "github.com" in call_url


def test_open_website_blocks_file_scheme():
    from app.desktop.web import open_website
    result = open_website("file:///etc/passwd")
    assert result["success"] is False
    assert "blocked" in result["message"].lower() or "file" in result["message"].lower()


def test_open_website_blocks_javascript_scheme():
    from app.desktop.web import open_website
    result = open_website("javascript:alert(1)")
    assert result["success"] is False
    assert "blocked" in result["message"].lower() or "javascript" in result["message"].lower()


def test_open_website_blocks_data_scheme():
    from app.desktop.web import open_website
    result = open_website("data:text/html,<script>alert(1)</script>")
    assert result["success"] is False
    assert "blocked" in result["message"].lower() or "data" in result["message"].lower()


def test_open_website_blocks_powershell_scheme():
    from app.desktop.web import open_website
    result = open_website("powershell:calc")
    assert result["success"] is False
    assert "blocked" in result["message"].lower() or "powershell" in result["message"].lower()


def test_open_website_blocks_cmd_scheme():
    from app.desktop.web import open_website
    result = open_website("cmd:del C:\\important.txt")
    assert result["success"] is False
    assert "blocked" in result["message"].lower() or "cmd" in result["message"].lower()


def test_open_website_blocks_vbscript_scheme():
    from app.desktop.web import open_website
    result = open_website("vbscript:MsgBox(1)")
    assert result["success"] is False


def test_open_website_empty_url_fails():
    from app.desktop.web import open_website
    result = open_website("")
    assert result["success"] is False


def test_open_website_no_host_fails():
    from app.desktop.web import open_website
    result = open_website("https://")
    assert result["success"] is False


def test_open_website_unknown_scheme_blocked():
    from app.desktop.web import open_website
    result = open_website("ftp://files.example.com")
    assert result["success"] is False
    assert "https" in result["message"].lower() or "blocked" in result["message"].lower()


# ── open_dashboard ────────────────────────────────────────────────────────────

def test_open_dashboard_uses_127_url():
    from app.desktop.web import open_dashboard, JARVIS_DASHBOARD_URL
    with patch("webbrowser.open") as mock_open:
        result = open_dashboard()
    assert result["success"] is True
    assert "127.0.0.1" in JARVIS_DASHBOARD_URL
    mock_open.assert_called_once_with(JARVIS_DASHBOARD_URL)


def test_open_dashboard_url_is_http_not_https():
    from app.desktop.web import JARVIS_DASHBOARD_URL
    assert JARVIS_DASHBOARD_URL.startswith("http://127.0.0.1")


# ── open_app additions (file_explorer, settings) ─────────────────────────────

def test_open_app_file_explorer_in_allowlist():
    from app.desktop.apps import APP_ALLOWLIST
    assert "file_explorer" in APP_ALLOWLIST


def test_open_app_settings_in_uri_apps():
    from app.desktop.apps import _URI_APPS
    assert "settings" in _URI_APPS


def test_open_app_cmd_not_allowed():
    from app.desktop.apps import open_app, APP_ALLOWLIST, _URI_APPS
    assert "cmd" not in APP_ALLOWLIST
    assert "cmd" not in _URI_APPS
    result = open_app("cmd")
    assert result["success"] is False


def test_open_app_powershell_not_allowed():
    from app.desktop.apps import open_app, APP_ALLOWLIST, _URI_APPS
    assert "powershell" not in APP_ALLOWLIST
    assert "powershell" not in _URI_APPS
    result = open_app("powershell")
    assert result["success"] is False


def test_open_app_unknown_returns_failure():
    from app.desktop.apps import open_app
    result = open_app("totally_unknown_app_xyz")
    assert result["success"] is False
    assert "allowlist" in result["message"].lower()


def test_open_app_no_shell_true():
    """Verify subprocess.Popen in apps.py is never called with shell=True."""
    import app.desktop.apps as apps_module
    # All Popen calls use shell=False explicitly
    with patch("platform.system", return_value="Windows"), \
         patch("subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        apps_module.open_app("notepad")
    _, kwargs = mock_popen.call_args
    assert kwargs.get("shell", False) is False


def test_open_app_file_explorer_windows():
    from app.desktop.apps import open_app
    with patch("platform.system", return_value="Windows"), \
         patch("subprocess.Popen") as mock_popen:
        result = open_app("file_explorer")
    assert result["success"] is True
    args = mock_popen.call_args[0][0]
    assert "explorer" in args[0].lower()


def test_open_app_settings_windows():
    from app.desktop.apps import open_app
    with patch("platform.system", return_value="Windows"), \
         patch("subprocess.Popen") as mock_popen:
        result = open_app("settings")
    assert result["success"] is True
    args = mock_popen.call_args[0][0]
    assert "explorer" in args[0].lower()
    assert "ms-settings:" in args


def test_open_app_settings_non_windows():
    from app.desktop.apps import open_app
    with patch("platform.system", return_value="Linux"):
        result = open_app("settings")
    assert result["success"] is False


# ── open_folder ───────────────────────────────────────────────────────────────

def _mock_folder_open(folder_name: str) -> dict:
    from app.desktop.folders import open_folder
    with patch("subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        result = open_folder(folder_name)
    return result


def test_open_folder_downloads():
    result = _mock_folder_open("downloads")
    assert result["success"] is True
    assert "downloads" in result["data"]["folder"]


def test_open_folder_documents():
    result = _mock_folder_open("documents")
    assert result["success"] is True


def test_open_folder_desktop():
    result = _mock_folder_open("desktop")
    assert result["success"] is True


def test_open_folder_notes():
    result = _mock_folder_open("notes")
    assert result["success"] is True
    assert "JARVIS_Notes" in result["data"]["path"]


def test_open_folder_notes_with_suffix():
    result = _mock_folder_open("notes folder")
    assert result["success"] is True
    assert "JARVIS_Notes" in result["data"]["path"]


def test_open_folder_jarvis_folder():
    result = _mock_folder_open("jarvis folder")
    assert result["success"] is True
    assert result["data"]["folder"] == "jarvis"


def test_open_folder_arbitrary_path_blocked():
    from app.desktop.folders import open_folder
    result = open_folder("/etc/passwd")
    assert result["success"] is False
    assert "allowed" in result["message"].lower()


def test_open_folder_absolute_path_blocked():
    from app.desktop.folders import open_folder
    result = open_folder("C:\\Windows\\System32")
    assert result["success"] is False


def test_open_folder_dotdot_blocked():
    from app.desktop.folders import open_folder
    result = open_folder("../../../etc")
    assert result["success"] is False


def test_open_folder_no_shell_true():
    import inspect
    import app.desktop.folders as folders_module
    source = inspect.getsource(folders_module)
    assert "shell=True" not in source


# ── create_note ───────────────────────────────────────────────────────────────

def test_create_note_writes_inside_notes_dir(tmp_path):
    from app.desktop import notes as notes_module
    fake_notes_dir = tmp_path / "JARVIS_Notes"
    with patch.object(notes_module, "NOTES_DIR", fake_notes_dir):
        result = notes_module.create_note("remember to buy milk")
    assert result["success"] is True
    note_path = Path(result["data"]["path"])
    assert note_path.parent == fake_notes_dir
    assert note_path.exists()


def test_create_note_content_is_written(tmp_path):
    from app.desktop import notes as notes_module
    fake_notes_dir = tmp_path / "JARVIS_Notes"
    with patch.object(notes_module, "NOTES_DIR", fake_notes_dir):
        result = notes_module.create_note("test content here")
    note_path = Path(result["data"]["path"])
    text = note_path.read_text(encoding="utf-8")
    assert "test content here" in text


def test_create_note_sanitizes_filename():
    from app.desktop.notes import _sanitize_filename
    assert "/" not in _sanitize_filename("test/slash")
    assert "\\" not in _sanitize_filename("test\\backslash")
    assert ":" not in _sanitize_filename("C: drive")
    assert "*" not in _sanitize_filename("a*b")
    assert "?" not in _sanitize_filename("what?")
    assert "<" not in _sanitize_filename("<html>")
    assert ">" not in _sanitize_filename("<html>")
    assert "|" not in _sanitize_filename("pipe|here")


def test_create_note_empty_content_fails():
    from app.desktop.notes import create_note
    result = create_note("")
    assert result["success"] is False
    assert "empty" in result["message"].lower()


def test_create_note_filename_has_timestamp(tmp_path):
    from app.desktop import notes as notes_module
    fake_notes_dir = tmp_path / "JARVIS_Notes"
    with patch.object(notes_module, "NOTES_DIR", fake_notes_dir):
        result = notes_module.create_note("timestamped note")
    filename = result["data"]["filename"]
    # Timestamp format: YYYYMMDD_HHMMSS_...
    assert len(filename) > 15
    assert filename[8] == "_"


def test_create_note_path_not_outside_notes_dir(tmp_path):
    """Path traversal attempt: content with special chars cannot escape NOTES_DIR."""
    from app.desktop import notes as notes_module
    fake_notes_dir = tmp_path / "JARVIS_Notes"
    with patch.object(notes_module, "NOTES_DIR", fake_notes_dir):
        result = notes_module.create_note("normal note ../../etc/passwd")
    if result["success"]:
        note_path = Path(result["data"]["path"])
        assert note_path.resolve().is_relative_to(fake_notes_dir)


# ── disk_space ────────────────────────────────────────────────────────────────

def test_disk_space_returns_success():
    from app.desktop.system import get_disk_space
    result = get_disk_space()
    assert result["success"] is True


def test_disk_space_has_required_fields():
    from app.desktop.system import get_disk_space
    result = get_disk_space()
    data = result["data"]
    assert "free_gb" in data
    assert "used_gb" in data
    assert "total_gb" in data
    assert "percent_used" in data


def test_disk_space_values_are_positive():
    from app.desktop.system import get_disk_space
    result = get_disk_space()
    data = result["data"]
    assert data["total_gb"] > 0
    assert data["free_gb"] >= 0
    assert 0 <= data["percent_used"] <= 100


# ── network_info ──────────────────────────────────────────────────────────────

def test_network_info_returns_success():
    from app.desktop.system import get_network_info
    result = get_network_info()
    assert result["success"] is True


def test_network_info_has_hostname():
    from app.desktop.system import get_network_info
    result = get_network_info()
    assert "hostname" in result["data"]
    assert result["data"]["hostname"] != ""


def test_network_info_has_local_ip():
    from app.desktop.system import get_network_info
    result = get_network_info()
    assert "local_ip" in result["data"]


def test_network_info_no_external_call():
    """get_network_info must use only socket — no outbound HTTP."""
    import inspect
    import app.desktop.system as system_module
    source = inspect.getsource(system_module.get_network_info)
    assert "urllib.request" not in source
    assert "httpx" not in source
    # Must rely on socket, not HTTP clients
    assert "socket" in source


# ── battery_status ────────────────────────────────────────────────────────────

def test_battery_status_graceful_when_unavailable():
    from app.desktop.system import get_battery_status
    with patch("psutil.sensors_battery", return_value=None):
        result = get_battery_status()
    assert result["success"] is True
    assert result["data"]["available"] is False


def test_battery_status_graceful_on_notimplemented():
    from app.desktop.system import get_battery_status
    with patch("psutil.sensors_battery", side_effect=NotImplementedError):
        result = get_battery_status()
    assert result["success"] is True


def test_battery_status_returns_data_when_available():
    from app.desktop.system import get_battery_status
    mock_batt = MagicMock()
    mock_batt.percent = 75.0
    mock_batt.power_plugged = True
    with patch("psutil.sensors_battery", return_value=mock_batt):
        result = get_battery_status()
    assert result["success"] is True
    assert result["data"]["available"] is True
    assert result["data"]["percent"] == 75.0
    assert result["data"]["plugged_in"] is True


# ── Router mapping ────────────────────────────────────────────────────────────

def _router_tool(command: str) -> str:
    """Return the tool_name that the ROUTES list resolves a command to."""
    from app.core.router import ROUTES
    for route in ROUTES:
        kwargs = route.match(command)
        if kwargs is not None:
            return route.tool_name
    return ""


def test_router_open_website_url():
    assert _router_tool("open website github.com") == "open_website"


def test_router_open_url():
    assert _router_tool("open url https://example.com") == "open_website"


def test_router_open_site():
    assert _router_tool("open site example.com") == "open_website"


def test_router_open_dashboard():
    assert _router_tool("open dashboard") == "open_dashboard"


def test_router_open_jarvis_dashboard():
    assert _router_tool("open jarvis dashboard") == "open_dashboard"


def test_router_open_jarvis():
    assert _router_tool("open jarvis") == "open_dashboard"


def test_router_open_downloads():
    assert _router_tool("open downloads") == "open_folder"


def test_router_open_documents():
    assert _router_tool("open documents") == "open_folder"


def test_router_open_desktop():
    assert _router_tool("open desktop") == "open_folder"


def test_router_open_notes():
    assert _router_tool("open notes") == "open_folder"


def test_router_open_notes_folder():
    assert _router_tool("open notes folder") == "open_folder"


def test_router_open_jarvis_folder():
    assert _router_tool("open jarvis folder") == "open_folder"


def test_router_open_file_explorer():
    assert _router_tool("open file explorer") == "open_app"


def test_router_disk_space():
    assert _router_tool("disk space") == "disk_space"


def test_router_disk_usage():
    assert _router_tool("disk usage") == "disk_space"


def test_router_show_disk_usage():
    assert _router_tool("show disk usage") == "disk_space"


def test_router_network_status():
    assert _router_tool("network status") == "network_info"


def test_router_network_info():
    assert _router_tool("network info") == "network_info"


def test_router_show_my_ip():
    assert _router_tool("show my ip") == "network_info"


def test_router_show_network_info():
    assert _router_tool("show network info") == "network_info"


def test_router_battery_status():
    assert _router_tool("battery status") == "battery_status"


def test_router_power_status():
    assert _router_tool("power status") == "battery_status"


def test_router_create_note():
    assert _router_tool("create note buy milk") == "create_note"


def test_router_write_note():
    assert _router_tool("write note remember dentist") == "create_note"


def test_router_create_note_extracts_content():
    from app.core.router import ROUTES
    for route in ROUTES:
        if route.tool_name == "create_note":
            kwargs = route.match("create note buy milk tomorrow")
            assert kwargs == {"content": "buy milk tomorrow"}
            break


def test_router_open_website_extracts_url():
    from app.core.router import ROUTES
    for route in ROUTES:
        if route.tool_name == "open_website":
            kwargs = route.match("open website github.com")
            assert kwargs == {"url": "github.com"}
            break


# ── Safe actions don't create pending approvals ───────────────────────────────

def test_safe_actions_no_pending_approval(api_client):
    for cmd in ("disk space", "network status", "battery status"):
        r = api_client.post("/command", json={"command": cmd})
        assert r.status_code == 200
        body = r.json()
        assert body.get("requires_approval", False) is False, f"Unexpected approval for: {cmd}"
        assert body.get("pending_action_id") is None


def test_clear_logs_still_requires_approval(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    assert r.status_code == 200
    assert r.json()["requires_approval"] is True


# ── Regression: existing approved routes unaffected ──────────────────────────

def test_open_chrome_still_routes_to_open_app():
    assert _router_tool("open chrome") == "open_app"


def test_open_notepad_still_routes_to_open_app():
    assert _router_tool("open notepad") == "open_app"


def test_system_status_unaffected():
    assert _router_tool("system status") == "system_status"


# ── No secrets in responses ──────────────────────────────────────────────────

def test_no_secrets_in_disk_space_response(api_client):
    r = api_client.post("/command", json={"command": "disk space"})
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-" not in r.text


def test_no_secrets_in_network_info_response(api_client):
    r = api_client.post("/command", json={"command": "network status"})
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-" not in r.text


# ── Help page ─────────────────────────────────────────────────────────────────

def test_help_page_includes_disk_space(api_client):
    r = api_client.get("/ui/help")
    assert r.status_code == 200
    assert "disk space" in r.text.lower() or "disk" in r.text.lower()


def test_help_page_includes_open_website(api_client):
    r = api_client.get("/ui/help")
    assert "open website" in r.text.lower() or "website" in r.text.lower()


def test_help_page_includes_create_note(api_client):
    r = api_client.get("/ui/help")
    assert "create note" in r.text.lower() or "note" in r.text.lower()


def test_help_page_no_real_api_key(api_client):
    """Help page may show the variable name as instructional text but never a real key."""
    r = api_client.get("/ui/help")
    # Real Anthropic keys start with "sk-ant-" — none should appear
    assert "sk-ant-" not in r.text


# ── Version / phase ───────────────────────────────────────────────────────────

def test_version_is_020(api_client):
    r = api_client.get("/")
    assert "0.2.0" in r.json()["version"]


def test_health_reports_v02(api_client):
    r = api_client.get("/health")
    body = r.json()
    assert "v0.2" in body["phase"]
