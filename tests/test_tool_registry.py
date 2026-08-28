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
    assert "unexpected error" in result["message"].lower()


def test_tool_handler_exception_never_leaks_raw_text():
    """The handler's raw exception message must never reach the caller —
    only a safe category + correlation ID. See app/core/errors.py."""
    reg = ToolRegistry()

    def bad_handler():
        raise RuntimeError("simulated crash with a secret token sk-ant-abc123xyz and /home/realuser/private/path")

    reg.register(_make_def("crasher"), bad_handler)
    result = reg.execute("crasher")

    assert result["success"] is False
    assert "sk-ant-abc123xyz" not in result["message"]
    assert "/home/realuser/private/path" not in result["message"]
    assert "simulated crash" not in result["message"]
    assert result["data"]["error_category"] == "tool_error"
    assert result["data"]["correlation_id"]


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


# --- v0.2 release-gate: real bounded tool execution timeout ---

def _make_hanging_def(name: str, timeout_seconds: float, level: PermissionLevel = PermissionLevel.SAFE) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Deliberately hanging test tool: {name}",
        permission_level=level,
        category=ToolCategory.UTILITY,
        timeout_seconds=timeout_seconds,
    )


def test_execute_returns_promptly_when_handler_hangs():
    """A handler that sleeps far longer than its declared timeout must
    not block execute() for anywhere near that long — this is the actual
    bounded-wait guarantee, proven with a real clock, not just a mock."""
    import time

    reg = ToolRegistry()

    def hang_forever(**kwargs):
        time.sleep(30)
        return {"success": True, "message": "should never be observed in time", "data": None}

    reg.register(_make_hanging_def("hanger", timeout_seconds=0.2), hang_forever)

    start = time.monotonic()
    result = reg.execute("hanger")
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"execute() blocked for {elapsed}s despite a 0.2s timeout"
    assert result["success"] is False
    assert result["data"]["timed_out"] is True
    assert result["data"]["error_category"] == "tool_timeout"
    assert result["data"]["correlation_id"]


def test_execute_timeout_message_is_clear_and_safe():
    import time

    reg = ToolRegistry()
    reg.register(_make_hanging_def("hanger2", timeout_seconds=0.1), lambda **kw: time.sleep(30))
    result = reg.execute("hanger2")

    assert "did not finish in time" in result["message"].lower() or "time" in result["message"].lower()
    assert "hanger2" not in result["message"]  # no raw internals, just the safe category message


def test_execute_approved_also_enforces_timeout():
    import time

    reg = ToolRegistry()
    reg.register(
        _make_hanging_def("hanger3", timeout_seconds=0.2, level=PermissionLevel.APPROVAL_REQUIRED),
        lambda **kw: time.sleep(30),
    )

    start = time.monotonic()
    result = reg.execute_approved("hanger3")
    elapsed = time.monotonic() - start

    assert elapsed < 5.0
    assert result["success"] is False
    assert result["data"]["timed_out"] is True


def test_fast_handler_is_unaffected_by_timeout_machinery():
    """The timeout wrapper must not slow down or otherwise change the
    outcome of a normal, fast tool call."""
    reg = ToolRegistry()
    reg.register(_make_hanging_def("fast", timeout_seconds=5.0), lambda: {"success": True, "message": "quick", "data": 42})
    result = reg.execute("fast")
    assert result == {"success": True, "message": "quick", "data": 42}


def test_orphaned_handler_result_is_never_observed_after_timeout():
    """Python cannot forcibly kill the background thread running a timed-
    out handler, so it may keep running until it finishes naturally. This
    proves the discarded-result guarantee this codebase actually offers:
    even after the orphaned thread completes and mutates a shared flag,
    nothing about the already-returned timeout result changes, and no
    exception escapes into the caller's process because of it."""
    import threading
    import time

    reg = ToolRegistry()
    finished = threading.Event()

    def slow_but_eventually_finishes(**kwargs):
        time.sleep(0.5)
        finished.set()
        return {"success": True, "message": "late arrival", "data": None}

    reg.register(_make_hanging_def("late", timeout_seconds=0.1), slow_but_eventually_finishes)

    result = reg.execute("late")
    assert result["data"]["timed_out"] is True

    # Give the orphaned background thread time to actually complete.
    assert finished.wait(timeout=3.0), "background thread never completed"
    # The already-returned result is unaffected by the late completion.
    assert result["data"]["timed_out"] is True
