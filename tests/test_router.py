"""Tests for the command router."""

import pytest
from unittest.mock import MagicMock, patch
from app.core.router import CommandRouter, ROUTES, Route
from app.core.tool_registry import ToolRegistry
from app.core.models import PermissionLevel, ToolCategory, ToolDefinition


def _make_registry(*tool_names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for name in tool_names:
        reg.register(
            ToolDefinition(
                name=name,
                description=f"Mock {name}",
                permission_level=PermissionLevel.SAFE,
                category=ToolCategory.UTILITY,
            ),
            lambda: {"success": True, "message": f"{name} executed", "data": None},
        )
    return reg


# --- Route pattern matching ---

def test_route_matches_help():
    route = next(r for r in ROUTES if r.tool_name == "help")
    assert route.match("help") == {}
    assert route.match("HELP") == {}


def test_route_matches_system_status():
    route = next(r for r in ROUTES if r.tool_name == "system_status")
    assert route.match("system status") == {}
    assert route.match("System Status") == {}


def test_route_matches_open_app():
    route = next(r for r in ROUTES if r.tool_name == "open_app")
    result = route.match("open chrome")
    assert result == {"app_name": "chrome"}
    result2 = route.match("open notepad")
    assert result2 == {"app_name": "notepad"}


def test_route_matches_screenshot():
    route = next(r for r in ROUTES if r.tool_name == "take_screenshot")
    assert route.match("screenshot") == {}
    assert route.match("take screenshot") == {}


def test_route_matches_memory_add():
    route = next(r for r in ROUTES if r.tool_name == "add_memory")
    result = route.match("memory add my important note")
    assert result == {"content": "my important note"}


def test_route_matches_memory_search():
    route = next(r for r in ROUTES if r.tool_name == "search_memory")
    result = route.match("memory search project ideas")
    assert result == {"query": "project ideas"}


def test_route_no_match_returns_none():
    route = next(r for r in ROUTES if r.tool_name == "help")
    assert route.match("open chrome") is None
    assert route.match("totally unknown") is None


# --- CommandRouter integration ---

def test_router_empty_command_returns_failure():
    reg = _make_registry()
    router = CommandRouter(reg)
    resp = router.route("")
    assert resp.success is False


def test_router_unknown_command_returns_failure():
    reg = _make_registry()
    router = CommandRouter(reg)
    resp = router.route("launch spaceship")
    assert resp.success is False
    assert "Unknown command" in resp.message


def test_router_routes_help_command():
    reg = _make_registry("help")
    router = CommandRouter(reg)
    with patch("db.database.get_db") as mock_db:
        mock_db.return_value.log_action = MagicMock()
        resp = router.route("help")
    assert resp.tool_used == "help"


def test_router_routes_system_status():
    reg = _make_registry("system_status")
    router = CommandRouter(reg)
    with patch("db.database.get_db") as mock_db:
        mock_db.return_value.log_action = MagicMock()
        resp = router.route("system status")
    assert resp.tool_used == "system_status"


def test_router_routes_open_chrome():
    reg = _make_registry("open_app")
    router = CommandRouter(reg)
    with patch("db.database.get_db") as mock_db:
        mock_db.return_value.log_action = MagicMock()
        resp = router.route("open chrome")
    assert resp.tool_used == "open_app"


def test_router_routes_screenshot():
    reg = _make_registry("take_screenshot")
    router = CommandRouter(reg)
    with patch("db.database.get_db") as mock_db:
        mock_db.return_value.log_action = MagicMock()
        resp = router.route("screenshot")
    assert resp.tool_used == "take_screenshot"


def test_router_routes_memory_add():
    reg = _make_registry("add_memory")
    router = CommandRouter(reg)
    with patch("db.database.get_db") as mock_db:
        mock_db.return_value.log_action = MagicMock()
        resp = router.route("memory add buy milk")
    assert resp.tool_used == "add_memory"


def test_router_routes_exit():
    reg = _make_registry("exit")
    router = CommandRouter(reg)
    with patch("db.database.get_db") as mock_db:
        mock_db.return_value.log_action = MagicMock()
        resp = router.route("exit")
    assert resp.tool_used == "exit"
