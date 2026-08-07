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


# --- v0.2: typed input_model validation ---

def _make_def_with_input(name: str, input_model, level: PermissionLevel = PermissionLevel.SAFE) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Test tool: {name}",
        permission_level=level,
        category=ToolCategory.UTILITY,
        input_model=input_model,
    )


def test_execute_validates_input_model_and_rejects_bad_input():
    from pydantic import BaseModel

    class GreetInput(BaseModel):
        person_name: str

    reg = ToolRegistry()
    reg.register(
        _make_def_with_input("greet", GreetInput),
        lambda person_name: {"success": True, "message": f"Hello, {person_name}", "data": None},
    )
    # Missing the required field entirely.
    result = reg.execute("greet")
    assert result["success"] is False
    assert "greet" in result["message"]
    assert "validation error" in result["message"].lower()


def test_execute_validates_input_model_and_accepts_good_input():
    from pydantic import BaseModel

    class GreetInput(BaseModel):
        person_name: str

    reg = ToolRegistry()
    reg.register(
        _make_def_with_input("greet", GreetInput),
        lambda person_name: {"success": True, "message": f"Hello, {person_name}", "data": None},
    )
    result = reg.execute("greet", person_name="Ada")
    assert result["success"] is True
    assert result["message"] == "Hello, Ada"


def test_execute_input_model_coerces_types():
    """A validated field is passed to the handler as the model's declared
    type, not the raw untyped kwarg — e.g. a numeric string becomes a real
    int, matching what Pydantic itself parsed."""
    from pydantic import BaseModel

    class CountInput(BaseModel):
        count: int

    reg = ToolRegistry()
    reg.register(
        _make_def_with_input("counter", CountInput),
        lambda count: {"success": True, "message": str(count * 2), "data": count},
    )
    result = reg.execute("counter", count="5")
    assert result["success"] is True
    assert result["data"] == 5
    assert isinstance(result["data"], int)


def test_execute_without_input_model_is_unaffected():
    """Every tool registered before v0.2 has no input_model — must behave
    exactly as before, arbitrary kwargs passed straight through."""
    reg = ToolRegistry()
    reg.register(_make_def("legacy"), lambda **kw: {"success": True, "message": str(kw), "data": kw})
    result = reg.execute("legacy", anything="goes", another=123)
    assert result["success"] is True
    assert result["data"] == {"anything": "goes", "another": 123}


def test_execute_approved_also_validates_input_model():
    from pydantic import BaseModel

    class GreetInput(BaseModel):
        person_name: str

    reg = ToolRegistry()
    reg.register(
        _make_def_with_input("greet", GreetInput, PermissionLevel.APPROVAL_REQUIRED),
        lambda person_name: {"success": True, "message": f"Hello, {person_name}", "data": None},
    )
    bad = reg.execute_approved("greet")
    assert bad["success"] is False
    good = reg.execute_approved("greet", person_name="Ada")
    assert good["success"] is True
