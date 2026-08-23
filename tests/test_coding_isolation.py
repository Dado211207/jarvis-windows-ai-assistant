"""Coding Workspace must not widen what the ordinary assistant can do.

This is the invariant the whole feature rests on, and the one a reviewer
should be able to check first: adding a coding agent to JARVIS must not
mean the chat box on /ui/chat can now write files or run commands.

`app/coding/registry.py`'s docstring promises these assertions by name.
If this file is deleted or weakened, that promise becomes a comment.
"""

import ast
from pathlib import Path

import pytest

from app.coding import registry

REPO_ROOT = Path(__file__).resolve().parent.parent
CODING_DIR = REPO_ROOT / "app" / "coding"


# ---------------------------------------------------------------------------
# The separation itself
# ---------------------------------------------------------------------------

def test_no_coding_capability_is_in_the_global_tool_registry():
    """The mechanism, not the intention: enumerate what chat can reach."""
    from app.core.brain import brain
    from app.core.tool_registry import registry as global_registry

    brain.initialise()
    global_names = {tool.definition.name for tool in global_registry.list_tools()}
    overlap = global_names & set(registry.names())
    assert overlap == set(), (
        "these Coding Workspace capabilities are reachable from ordinary chat: "
        f"{sorted(overlap)}"
    )


def test_no_coding_capability_is_reachable_through_the_router():
    """A deterministic route to a coding capability would bypass the
    Coding Workspace mode entirely."""
    from app.core.router import find_route

    reachable = [name for name in registry.names() if find_route(name) is not None]
    assert reachable == [], f"find_route() matched: {reachable}"


def test_no_coding_capability_declares_itself_reachable_from_chat():
    assert [c.name for c in registry.capabilities() if c.reachable_from_chat] == []


def test_the_coding_package_never_registers_a_tool_globally():
    """An AST walk, because a grep for 'register' misses `r = registry;
    r.register(...)` and a runtime check only covers the paths a test
    happens to import."""
    offenders = []
    for path in sorted(CODING_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "app.core.tool_registry":
                    offenders.append(f"{path.name}: imports app.core.tool_registry")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.core.tool_registry":
                        offenders.append(f"{path.name}: imports app.core.tool_registry")
    assert offenders == [], (
        "app/coding must never touch the global tool registry:\n" + "\n".join(offenders)
    )


def test_the_chat_router_does_not_import_the_coding_package():
    for name in ("router.py", "brain.py"):
        source = (REPO_ROOT / "app" / "core" / name).read_text(encoding="utf-8")
        assert "app.coding" not in source, (
            f"app/core/{name} imports app.coding — the two must stay separate"
        )


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------

def test_coding_workspace_is_disabled_until_a_project_is_added(tmp_path, monkeypatch):
    from app.coding import projects

    monkeypatch.setattr(projects, "_registry_path", lambda: tmp_path / "projects.json")
    assert projects.list_projects() == []
    assert projects.is_enabled() is False


def test_the_ordinary_assistant_still_works_with_no_coding_project(tmp_path, monkeypatch):
    """The rest of the product must not depend on Coding Workspace at all."""
    from fastapi.testclient import TestClient

    from app.api.server import create_app

    client = TestClient(create_app())
    assert client.get("/health").status_code == 200
    assert client.get("/ui/chat").status_code == 200
    assert client.get("/coding/status").json()["enabled"] in (True, False)


# ---------------------------------------------------------------------------
# The shell prohibition, applied to the new package specifically
# ---------------------------------------------------------------------------

def test_no_coding_module_ever_asks_for_a_shell():
    """CLAUDE.md's rule, checked against app/coding on its own.

    The repository-wide test already covers this, but a command runner is
    exactly the module where somebody would add `shell=True` "just for
    Windows", so it is worth failing here with a message that says why.
    """
    offenders = []
    for path in sorted(CODING_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "shell":
                    continue
                literal_false = (
                    isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                )
                if not literal_false:
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], (
        "shell=<not literally False> in the coding package: " + ", ".join(offenders)
    )


def test_no_coding_module_evaluates_a_string():
    forbidden = {"eval", "exec", "compile", "__import__"}
    offenders = []
    for path in sorted(CODING_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in forbidden:
                    offenders.append(f"{path.name}:{node.lineno}: {node.func.id}()")
    assert offenders == [], "\n".join(offenders)


# ---------------------------------------------------------------------------
# The capability declarations are honest
# ---------------------------------------------------------------------------

def test_every_declared_capability_matches_a_schema_action():
    """The registry the UI renders and the vocabulary the model may use
    must be the same set. A capability shown to the user that no action
    can invoke is a promise the product cannot keep, and an action with
    no declaration is a power nobody was told about."""
    from app.coding import schema

    assert sorted(registry.names()) == sorted(schema.known_actions())


def test_delete_always_requires_approval():
    capability = registry.get("delete_file")
    assert capability is not None
    assert capability.always_requires_approval is True


# ---------------------------------------------------------------------------
# Lifecycle: nothing outlives the server
# ---------------------------------------------------------------------------

def test_shutdown_ends_every_process_coding_workspace_owns(tmp_path):
    """A dev server that outlives JARVIS holds its port and its file
    handles, and the user has no way left to stop it."""
    import sys
    import time

    import psutil
    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.coding.runner import CommandHandle, build_environment, ledger

    handle = CommandHandle(
        [sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"],
        tmp_path, "project")
    handle.start(build_environment())
    ledger.track(handle)
    pid = handle.pid

    with TestClient(create_app()) as client:
        client.get("/health")
    # Leaving the context manager runs the shutdown half of the lifespan.

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            process = psutil.Process(pid)
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.1)
    else:
        ledger.stop_all("test cleanup")
        pytest.fail(f"a coding process (pid {pid}) survived server shutdown")


def test_startup_reclassifies_a_task_that_was_running_when_the_process_died(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.coding import tasks

    monkeypatch.setattr(tasks, "_tasks_path", lambda: tmp_path / "tasks.json")
    record = tasks.create("p1", "a long task")
    tasks.set_state(record, tasks.TaskState.RUNNING)

    with TestClient(create_app()) as client:
        client.get("/health")

    assert tasks.get(record.id).state == tasks.TaskState.INTERRUPTED.value


def test_an_interrupted_task_is_offered_not_resumed(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.coding import sessions, tasks

    monkeypatch.setattr(tasks, "_tasks_path", lambda: tmp_path / "tasks.json")
    record = tasks.create("p1", "a long task")
    tasks.set_state(record, tasks.TaskState.RUNNING)

    with TestClient(create_app()) as client:
        client.get("/health")
        listed = client.get("/coding/tasks").json()
        assert record.id in listed["interrupted"]
        # Nothing is running: it was reported, not restarted.
        assert sessions.live_ids() == []
        assert client.get(f"/coding/tasks/{record.id}/live").json()["live"] is False
