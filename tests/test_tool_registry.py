"""Tests for the tool registry."""

import pytest
from app.core.models import PermissionLevel, ToolCategory, ToolDefinition
from app.core.tool_registry import ToolRegistry


def _make_def(name: str, level: PermissionLevel = PermissionLevel.SAFE) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Test tool: {name}",
        permission_level=level,
        category=ToolCategory.UTILITY,
    )


def test_register_and_list():
    reg = ToolRegistry()
    reg.register(_make_def("ping"), lambda: {"success": True, "message": "pong", "data": None})
    assert len(reg) == 1
    tools = reg.list_definitions()
    assert tools[0].name == "ping"


def test_get_tool_returns_none_for_unknown():
    reg = ToolRegistry()
    assert reg.get("nonexistent") is None


def test_execute_safe_tool_succeeds():
    reg = ToolRegistry()
    reg.register(_make_def("echo"), lambda: {"success": True, "message": "hello", "data": "hello"})
    result = reg.execute("echo")
    assert result["success"] is True
    assert result["message"] == "hello"


def test_execute_unknown_tool_returns_failure():
    reg = ToolRegistry()
    result = reg.execute("does_not_exist")
    assert result["success"] is False
    assert "Unknown tool" in result["message"]


def test_execute_blocked_tool_is_refused():
    reg = ToolRegistry()
    reg.register(
        _make_def("steal_password", PermissionLevel.BLOCKED),
        lambda: {"success": True, "message": "SHOULD NOT RUN", "data": None},
    )
    result = reg.execute("steal_password")
    assert result["success"] is False
    assert "blocked" in result["message"].lower() or "permitted" in result["message"].lower()


def test_execute_approval_required_tool_is_paused():
    reg = ToolRegistry()
    reg.register(
        _make_def("delete_files", PermissionLevel.APPROVAL_REQUIRED),
        lambda: {"success": True, "message": "SHOULD NOT RUN", "data": None},
    )
    result = reg.execute("delete_files")
    assert result["success"] is False
    assert "approval" in result["message"].lower()


def test_multiple_tools_listed():
    reg = ToolRegistry()
    for name in ("a", "b", "c"):
        reg.register(_make_def(name), lambda: None)
    assert len(reg.list_tools()) == 3


def test_tool_handler_exception_is_caught():
    reg = ToolRegistry()

    def bad_handler():
        raise RuntimeError("simulated crash")

    reg.register(_make_def("crasher"), bad_handler)
    result = reg.execute("crasher")
    assert result["success"] is False
    assert "Tool error" in result["message"]
