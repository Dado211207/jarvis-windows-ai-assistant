"""Creating a new project is two steps, and the first one writes nothing.

The claim that matters most is negative, so it is tested twice: once by
counting what is on disk before and after planning, and once by walking
the module's AST for anything that could write. A behavioural test alone
would pass against a module that wrote to a path the test did not think
to look at.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from app.coding import project_plan, projects
from app.coding.workspace import WorkspaceViolation


@pytest.fixture(autouse=True)
def a_clean_store(tmp_path, monkeypatch):
    """Plans and the project registry both start empty, and stay out of
    the repository."""
    project_plan.clear()
    store = tmp_path / "registry"
    store.mkdir()
    monkeypatch.setattr(projects, "_registry_path", lambda: store / "projects.json")
    yield
    project_plan.clear()


def tree(root: Path):
    """Every path under root, so "nothing changed" can be asserted rather
    than assumed."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# ---------------------------------------------------------------------------
# Planning writes nothing
# ---------------------------------------------------------------------------

def test_planning_creates_nothing_at_all(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    before = tree(parent)

    plan = project_plan.build_plan(str(parent), "my-site", "static")

    assert tree(parent) == before, "planning wrote something"
    assert not (parent / "my-site").exists()
    assert plan.destination == str(parent / "my-site")


def test_cancelling_a_plan_creates_nothing(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    plan = project_plan.build_plan(str(parent), "my-site", "static")
    before = tree(parent)

    assert project_plan.cancel(plan.id) is True

    assert tree(parent) == before
    assert not (parent / "my-site").exists()
    assert project_plan.get(plan.id) is None


def test_the_planning_module_contains_no_write(tmp_path):
    """Behavioural proof plus structural proof. The first can only see the
    paths a test thought to check."""
    source = Path(project_plan.__file__).read_text(encoding="utf-8")
    tree_ = ast.parse(source)

    forbidden_methods = {"mkdir", "write_text", "write_bytes", "touch", "unlink",
                         "rmdir", "rename", "replace", "symlink_to", "chmod"}
    for node in ast.walk(tree_):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_methods, (
                f"project_plan calls {node.func.attr}() — planning must write nothing")
            # `templates.create` is the write path and lives in the other
            # module; only `execute()` may reach it.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "open", "project_plan must not open a file for writing"


def test_only_execute_may_reach_the_creating_function():
    source = Path(project_plan.__file__).read_text(encoding="utf-8")
    module = ast.parse(source)
    reached_in = []
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "create"
                        and isinstance(inner.func.value, ast.Name)
                        and inner.func.value.id == "templates"):
                    reached_in.append(node.name)
    assert reached_in == ["execute"], reached_in


# ---------------------------------------------------------------------------
# What the plan actually says
# ---------------------------------------------------------------------------

def test_the_plan_names_everything_a_person_needs_to_decide(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    plan = project_plan.build_plan(str(parent), "shop", "react-vite-ts").as_dict()

    assert plan["destination"] == str(parent / "shop")
    assert plan["project_name"] == "shop"
    assert plan["template"] == "react-vite-ts"
    assert plan["stack"]
    assert plan["files"], "the exact file list is the point of the plan"
    assert "package.json" in plan["files"]
    assert plan["git_init"] is True
    assert plan["initial_branch"], "a plan that initialises Git must say which branch"
    assert plan["dependencies"], "a template with dependencies must list them"
    assert plan["installs_dependencies"] is False
    assert plan["network_use"] == "none"
    assert plan["approximate_bytes"] > 0
    assert plan["protected_not_created"]
    assert plan["validation"]
    assert plan["conflicts"] == []
    assert plan["creatable"] is True


def test_dependencies_are_read_from_what_would_be_written(tmp_path):
    """Not from a hand-kept list, which would silently go stale the first
    time a template gained a dependency."""
    parent = tmp_path / "projects"
    parent.mkdir()
    plan = project_plan.build_plan(str(parent), "api", "fastapi")
    assert any(d.startswith("fastapi") for d in plan.dependencies)
    assert any(d.startswith("uvicorn") for d in plan.dependencies)


def test_a_template_with_nothing_to_install_says_so(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    plan = project_plan.build_plan(str(parent), "page", "static")
    assert plan.dependencies == []
    assert plan.installs_dependencies is False


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------

def test_an_existing_destination_is_a_conflict_not_a_merge(tmp_path):
    parent = tmp_path / "projects"
    (parent / "taken").mkdir(parents=True)
    (parent / "taken" / "their-work.txt").write_text("mine", encoding="utf-8")

    plan = project_plan.build_plan(str(parent), "taken", "static")
    assert plan.conflicts
    assert plan.creatable is False
    assert "already exists" in plan.conflicts[0]

    with pytest.raises(project_plan.PlanError):
        project_plan.execute(plan)
    assert (parent / "taken" / "their-work.txt").read_text(encoding="utf-8") == "mine"


def test_an_empty_existing_destination_is_also_refused(tmp_path):
    """"Empty" is a race. A folder that exists is not ours to fill."""
    parent = tmp_path / "projects"
    (parent / "empty").mkdir(parents=True)

    plan = project_plan.build_plan(str(parent), "empty", "static")
    assert plan.conflicts
    assert plan.creatable is False


def test_a_file_where_the_folder_would_go_is_a_conflict(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    (parent / "site").write_text("not a folder", encoding="utf-8")

    plan = project_plan.build_plan(str(parent), "site", "static")
    assert plan.conflicts
    assert "as a file" in plan.conflicts[0]


@pytest.mark.parametrize("name", ["..", "../escape", "a/b", "CON", "trailing.", ""])
def test_a_name_that_would_escape_or_collide_is_refused_at_plan_time(tmp_path, name):
    parent = tmp_path / "projects"
    parent.mkdir()
    with pytest.raises((project_plan.PlanError, WorkspaceViolation)):
        project_plan.build_plan(str(parent), name, "static")


def test_the_plan_shows_the_name_that_will_actually_be_used(tmp_path):
    """Surrounding whitespace is trimmed, which is what the user meant —
    but the plan must show the trimmed name and the destination it really
    produces, not the raw string they typed."""
    parent = tmp_path / "projects"
    parent.mkdir()
    plan = project_plan.build_plan(str(parent), "  my-site  ", "static")
    assert plan.project_name == "my-site"
    assert plan.destination == str(parent / "my-site")


def test_an_unknown_template_is_refused(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    with pytest.raises(project_plan.PlanError):
        project_plan.build_plan(str(parent), "site", "rails-with-webpack")


# ---------------------------------------------------------------------------
# Confirming
# ---------------------------------------------------------------------------

def test_confirming_a_plan_creates_exactly_what_it_described(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    plan = project_plan.build_plan(str(parent), "my-site", "static")

    project = project_plan.execute(plan)

    destination = Path(plan.destination)
    assert destination.is_dir()
    written = sorted(str(p.relative_to(destination)).replace("\\", "/")
                     for p in destination.rglob("*") if p.is_file())
    # git init adds a .git directory; the *files* are exactly the plan's.
    written = [w for w in written if not w.startswith(".git/")]
    assert written == plan.files
    assert project.root == plan.destination


def test_a_destination_that_appeared_since_the_plan_is_refused(tmp_path):
    """The gap between planning and confirming is exactly when somebody
    creates the folder."""
    parent = tmp_path / "projects"
    parent.mkdir()
    plan = project_plan.build_plan(str(parent), "racy", "static")
    assert plan.creatable is True

    (parent / "racy").mkdir()
    (parent / "racy" / "theirs.txt").write_text("do not overwrite", encoding="utf-8")

    with pytest.raises(project_plan.PlanError) as exc:
        project_plan.execute(plan)
    assert "changed" in str(exc.value) or "already exists" in str(exc.value)
    assert (parent / "racy" / "theirs.txt").read_text(encoding="utf-8") == "do not overwrite"


def test_a_plan_cannot_be_used_twice(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    plan = project_plan.build_plan(str(parent), "once", "static")
    project_plan.execute(plan)

    with pytest.raises(project_plan.PlanError):
        project_plan.execute(plan)


def test_an_expired_plan_is_refused(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    plan = project_plan.build_plan(str(parent), "stale", "static")
    plan.created_at = time.time() - project_plan.PLAN_TTL_SECONDS - 1

    with pytest.raises(project_plan.PlanError) as exc:
        project_plan.execute(plan)
    assert "expired" in str(exc.value)
    assert not Path(plan.destination).exists()


def test_a_failed_creation_leaves_the_plan_usable_again(tmp_path, monkeypatch):
    """The user should be able to fix the cause and confirm the same plan,
    rather than being told it is spent by an attempt that did nothing."""
    from app.coding import templates

    parent = tmp_path / "projects"
    parent.mkdir()
    plan = project_plan.build_plan(str(parent), "retry", "static")

    def explode(*args, **kwargs):
        raise OSError("the disk is full")

    monkeypatch.setattr(templates, "create", explode)
    with pytest.raises(OSError):
        project_plan.execute(plan)
    assert plan.consumed is False

    monkeypatch.undo()
    project_plan.execute(plan)
    assert Path(plan.destination).is_dir()


def test_only_a_bundled_template_is_ever_used(tmp_path):
    """Offline by construction: there is nothing in a template that could
    make a request."""
    from app.coding import templates

    parent = tmp_path / "projects"
    parent.mkdir()
    for key in templates._templates():
        plan = project_plan.build_plan(str(parent), f"p-{key}", key)
        assert plan.network_use == "none"
        assert plan.installs_dependencies is False
        assert all(command.startswith("git ") for command in plan.commands)
