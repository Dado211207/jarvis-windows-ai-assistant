"""
Smoke tests for CI — verify imports, routing, tool registry, permissions,
and the FastAPI layer all work end-to-end on a clean Linux environment.

Windows-only capabilities (app launcher, screenshot) are exercised only
at the routing/registration level; no real system calls are made.
"""

import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# 1. Import smoke tests
# ---------------------------------------------------------------------------

def test_app_package_imports():
    import app
    assert app.__version__
    assert app.__phase__


def test_config_imports():
    from app.config import settings
    assert settings.jarvis_host == "127.0.0.1"
    assert settings.jarvis_port == 5555
    assert not settings.has_anthropic_key  # no key in CI


def test_models_import():
    from app.core.models import (
        PermissionLevel, ToolCategory, ToolDefinition,
        CommandRequest, CommandResponse, MemoryEntry,
    )


def test_core_modules_import():
    from app.core.permissions import check_permission, PermissionDeniedError, ApprovalRequiredError
    from app.core.tool_registry import ToolRegistry
    from app.core.router import CommandRouter, ROUTES
    from app.core.brain import brain


def test_desktop_modules_import():
    from app.desktop import apps, screenshots, system


def test_db_modules_import():
    from db.database import Database, get_db
    from db.migrations import create_tables


# ---------------------------------------------------------------------------
# 2. Router smoke tests (routing only — no real system calls)
# ---------------------------------------------------------------------------

def _route(cmd: str, expected_tool: str) -> None:
    """Assert that *cmd* is routed to *expected_tool*, mocking DB logging."""
    from app.core.tool_registry import ToolRegistry
    from app.core.models import PermissionLevel, ToolCategory, ToolDefinition

    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name=expected_tool,
            description=f"smoke:{expected_tool}",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.UTILITY,
        ),
        lambda: {"success": True, "message": "ok", "data": None},
    )

    from app.core.router import CommandRouter
    with patch("db.database.get_db") as mock_db:
        mock_db.return_value.log_action = MagicMock()
        resp = CommandRouter(reg).route(cmd)

    assert resp.tool_used == expected_tool, (
        f"'{cmd}' routed to '{resp.tool_used}', expected '{expected_tool}'"
    )


def test_router_smoke_help():
    _route("help", "help")


def test_router_smoke_status():
    _route("status", "status")


def test_router_smoke_system_status():
    _route("system status", "system_status")


def test_router_smoke_memory_add():
    _route("memory add test memory", "add_memory")


def test_router_smoke_memory_search():
    _route("memory search test", "search_memory")


def test_router_smoke_open_app_routing_only():
    """open_app is in the allowlist but we only test the routing, not the launch."""
    _route("open chrome", "open_app")


def test_router_smoke_screenshot_routing_only():
    """take_screenshot is safe to route; actual capture is Windows/X11 only."""
    _route("screenshot", "take_screenshot")


def test_router_smoke_exit():
    _route("exit", "exit")


# ---------------------------------------------------------------------------
# 3. Tool registry smoke tests
# ---------------------------------------------------------------------------

def test_registry_has_tools_after_brain_init():
    from app.core.brain import brain
    from app.core.tool_registry import registry
    brain.initialise()
    assert len(registry) >= 9, f"Expected ≥9 tools, got {len(registry)}"


def test_registry_contains_expected_tool_names():
    from app.core.brain import brain
    from app.core.tool_registry import registry
    brain.initialise()
    registered = {t.name for t in registry.list_definitions()}
    required = {
        "help", "status", "exit",
        "system_status", "open_app", "list_apps",
        "take_screenshot", "add_memory", "search_memory",
    }
    missing = required - registered
    assert not missing, f"Tools missing from registry: {missing}"


# ---------------------------------------------------------------------------
# 4. Permission smoke tests (subset — full coverage in test_permissions.py)
# ---------------------------------------------------------------------------

def test_smoke_blocked_by_keyword():
    from app.core.permissions import check_permission, PermissionDeniedError
    from app.core.models import PermissionLevel
    for name in ("steal_password", "read_browser_passwords", "keylogger"):
        with pytest.raises(PermissionDeniedError):
            check_permission(name, PermissionLevel.SAFE)


def test_smoke_approval_required_paused():
    from app.core.permissions import check_permission, ApprovalRequiredError
    from app.core.models import PermissionLevel
    with pytest.raises(ApprovalRequiredError):
        check_permission("delete_files", PermissionLevel.APPROVAL_REQUIRED)


def test_smoke_safe_tools_pass():
    from app.core.permissions import check_permission
    from app.core.models import PermissionLevel
    for name in ("system_status", "add_memory", "take_screenshot", "help"):
        check_permission(name, PermissionLevel.SAFE)  # must not raise


# ---------------------------------------------------------------------------
# 5. FastAPI / API smoke tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def api_client():
    """Start the full FastAPI app via TestClient (triggers lifespan / brain init)."""
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield prime_session(client)


def test_fastapi_app_loads(api_client):
    """TestClient construction succeeds — app instantiates without error."""
    assert api_client is not None


def test_root_returns_200(api_client):
    r = api_client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert body["tools_registered"] >= 9


def test_health_returns_200(api_client):
    r = api_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["healthy"] is True
    assert body["status"] == "ok"


def test_health_is_degraded_when_the_database_is_unreachable(api_client):
    with patch("db.database.get_db", side_effect=OSError("database unavailable")):
        body = api_client.get("/health").json()

    assert body["healthy"] is False
    assert body["status"] == "degraded"
    assert body["db"] == "error"
    assert body["db_accessible"] is False


def test_health_reports_a_ready_selected_ollama_provider(api_client):
    from app.core.providers import ProviderStatus

    ready = ProviderStatus(
        name="ollama",
        display_name="Ollama (local models)",
        kind="local",
        available=True,
        detail="ready",
        models=["qwen2.5-coder:latest"],
    )
    with patch("app.core.providers.selected_provider", return_value="ollama"), \
         patch("app.core.providers.ollama_status", return_value=ready):
        body = api_client.get("/health").json()

    assert body["brain"] == "ollama"
    assert body["brain_configured"] is True


def test_health_keeps_the_unavailable_selected_provider_visible(api_client):
    from app.core.providers import ProviderStatus

    unavailable = ProviderStatus(
        name="ollama",
        display_name="Ollama (local models)",
        kind="local",
        available=False,
        detail="not running",
    )
    with patch("app.core.providers.selected_provider", return_value="ollama"), \
         patch("app.core.providers.ollama_status", return_value=unavailable):
        body = api_client.get("/health").json()

    assert body["brain"] == "ollama"
    assert body["brain_configured"] is False
    # Deterministic local commands still make the service operational.
    assert body["healthy"] is True
    assert body["status"] == "ok"


def test_tools_returns_200_and_non_empty(api_client):
    r = api_client.get("/tools")
    assert r.status_code == 200
    tools = r.json()
    assert isinstance(tools, list)
    assert len(tools) >= 9


def test_command_status_returns_200(api_client):
    r = api_client.post("/command", json={"command": "status"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True


def test_command_help_returns_200(api_client):
    r = api_client.post("/command", json={"command": "help"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True


def test_command_unknown_returns_200_via_brain(api_client):
    """Unknown commands are handled by the brain (Phase 2) and return success=True."""
    r = api_client.post("/command", json={"command": "launch_rocket"})
    assert r.status_code == 200
    body = r.json()
    # Brain handles unknown commands; no API key in CI so local fallback is used.
    assert body["success"] is True
    assert body["tool_used"] == "brain"


def test_memory_search_endpoint(api_client):
    r = api_client.get("/memory/search", params={"q": "ci_smoke_test"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_logs_endpoint(api_client):
    r = api_client.get("/logs")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
