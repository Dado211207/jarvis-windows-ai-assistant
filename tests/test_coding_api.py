"""The Coding Workspace HTTP surface.

What is checked here is not that the endpoints work — the modules under
them are tested directly. It is the things only visible at the boundary:
that mutations need the session token, that a refusal explains itself,
that removing a project deletes nothing, and that no response ever
carries a secret or a raw exception.
"""

import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests import coding_fixtures as fx


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client whose project registry and task history live in tmp_path."""
    from app.coding import projects, tasks

    monkeypatch.setattr(projects, "_registry_path", lambda: tmp_path / "projects.json")
    monkeypatch.setattr(tasks, "_tasks_path", lambda: tmp_path / "tasks.json")

    from app.api.server import create_app

    test_client = TestClient(create_app())
    test_client.get("/health")          # sets the session cookie
    return test_client


def token_headers(client) -> dict:
    return {"X-JARVIS-Session-Token": client.cookies.get("jarvis_session")}


@pytest.fixture
def project(tmp_path):
    root = fx.static_site(tmp_path / "demo", with_defect=False)
    fx.with_secrets(root)
    fx.init_repo(root)
    return root


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

MUTATING = [
    ("post", "/coding/projects", {"path": "/tmp", "name": "x"}),
    ("post", "/coding/projects/create", {"parent_path": "/tmp", "name": "x",
                                         "template": "static"}),
    ("post", "/coding/tasks/plan", {"project_id": "x", "request": "y"}),
    ("post", "/coding/tasks/start", {"task_id": "x"}),
    ("post", "/coding/tasks/decide", {"task_id": "x", "granted": True}),
    ("post", "/coding/tasks/stop", {"task_id": "x"}),
    ("post", "/coding/tasks/commit", {"task_id": "x", "message": "m"}),
    ("post", "/coding/tasks/undo", {"task_id": "x"}),
    ("post", "/coding/tasks/clear", {}),
    ("post", "/coding/processes/stop-all", {}),
    ("delete", "/coding/projects/anything", None),
    ("delete", "/coding/tasks/anything", None),
]


@pytest.mark.parametrize("method,path,body", MUTATING)
def test_every_mutating_endpoint_refuses_without_the_session_token(client, method, path, body):
    call = getattr(client, method)
    response = call(path, json=body) if body is not None else call(path)
    assert response.status_code in (401, 403), (
        f"{method.upper()} {path} accepted a request with no session token "
        f"({response.status_code})"
    )


@pytest.mark.parametrize("path", [
    "/coding/status", "/coding/projects", "/coding/templates", "/coding/browser-check",
])
def test_read_endpoints_work_without_a_token(client, path):
    assert client.get(path).status_code == 200


def test_a_wrong_token_is_refused(client):
    response = client.post("/coding/tasks/clear",
                           headers={"X-JARVIS-Session-Token": "not-the-real-one"})
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def test_adding_a_project_detects_its_stack_and_repository(client, project):
    response = client.post("/coding/projects", json={"path": str(project), "name": "demo"},
                           headers=token_headers(client))
    assert response.status_code == 200
    payload = response.json()["project"]
    assert payload["stack"]["label"]
    assert payload["git"]["is_repository"] is True
    assert payload["available"] is True


def test_adding_a_folder_that_does_not_exist_is_refused_with_a_reason(client, tmp_path):
    response = client.post("/coding/projects",
                           json={"path": str(tmp_path / "nope"), "name": "x"},
                           headers=token_headers(client))
    assert response.status_code == 400
    assert response.json()["detail"]
    assert "Traceback" not in response.json()["detail"]


def test_adding_a_file_rather_than_a_folder_is_refused(client, tmp_path):
    target = tmp_path / "a-file.txt"
    target.write_text("x\n", encoding="utf-8")
    response = client.post("/coding/projects", json={"path": str(target), "name": "x"},
                           headers=token_headers(client))
    assert response.status_code == 400


def test_removing_a_project_deletes_nothing_and_says_so(client, project):
    added = client.post("/coding/projects", json={"path": str(project), "name": "demo"},
                        headers=token_headers(client)).json()["project"]
    before = sorted(p.name for p in project.rglob("*") if p.is_file())

    response = client.delete(f"/coding/projects/{added['id']}", headers=token_headers(client))
    assert response.status_code == 200
    body = response.json()
    assert body["files_deleted"] is False
    assert "No files were deleted" in body["message"]

    after = sorted(p.name for p in project.rglob("*") if p.is_file())
    assert after == before
    assert (project / ".env").exists(), "removing a project deleted the user's files"


def test_the_status_endpoint_publishes_what_is_disabled_and_what_is_protected(client):
    body = client.get("/coding/status").json()
    disabled = " ".join(body["disabled_in_this_version"]).lower()
    for forbidden in ("push", "pull request", "merge", "deploy"):
        assert forbidden in disabled
    assert body["protected"]["filenames"]
    assert ".env" in body["protected"]["filenames"]
    assert body["capabilities"]
    assert body["risk_matrix"]


def test_status_is_disabled_until_a_project_exists(client, project):
    assert client.get("/coding/status").json()["enabled"] is False
    client.post("/coding/projects", json={"path": str(project), "name": "demo"},
                headers=token_headers(client))
    assert client.get("/coding/status").json()["enabled"] is True


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_the_plan_endpoint_runs_nothing_and_changes_nothing(client, project):
    added = client.post("/coding/projects", json={"path": str(project), "name": "demo"},
                        headers=token_headers(client)).json()["project"]
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}

    plan = client.post("/coding/tasks/plan",
                       json={"project_id": added["id"], "request": "add a footer"},
                       headers=token_headers(client)).json()["plan"]

    assert plan["task_id"]
    assert plan["isolation"]["strategy"] == "worktree"
    assert plan["operations_requiring_approval"]
    assert plan["validation_plan"]
    assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == before


def test_the_plan_discloses_the_provider_and_whether_content_leaves_the_device(client, project):
    added = client.post("/coding/projects", json={"path": str(project), "name": "demo"},
                        headers=token_headers(client)).json()["project"]
    plan = client.post("/coding/tasks/plan",
                       json={"project_id": added["id"], "request": "x"},
                       headers=token_headers(client)).json()["plan"]
    provider = plan["provider"]
    assert "content_leaves_device" in provider
    assert provider["location"] in ("cloud", "local", "none")
    assert "blocked" in provider


def test_privacy_mode_blocks_a_cloud_provider_and_explains_why(client, project, monkeypatch):
    from app.core.privacy import privacy_mode

    monkeypatch.setattr(privacy_mode, "_active", True, raising=False)
    privacy_mode.set(True)
    try:
        body = client.get("/coding/status").json()
        assert body["privacy_mode"] is True
        assert "local" in body["privacy_note"].lower()
    finally:
        privacy_mode.set(False)


def test_starting_a_task_that_does_not_exist_is_a_clean_refusal(client):
    response = client.post("/coding/tasks/start", json={"task_id": "nope"},
                           headers=token_headers(client))
    assert response.status_code in (404, 409)
    assert "Traceback" not in json.dumps(response.json())


def test_a_live_state_for_a_task_that_is_not_running_says_so(client, project):
    added = client.post("/coding/projects", json={"path": str(project), "name": "demo"},
                        headers=token_headers(client)).json()["project"]
    plan = client.post("/coding/tasks/plan",
                       json={"project_id": added["id"], "request": "x"},
                       headers=token_headers(client)).json()["plan"]

    live = client.get(f"/coding/tasks/{plan['task_id']}/live").json()
    assert live["live"] is False
    assert live["pending_approval"] is None
    assert live["preview"] is None


# ---------------------------------------------------------------------------
# The diff, and who made each change
# ---------------------------------------------------------------------------

def test_the_diff_distinguishes_the_users_own_changes_from_jarviss(client, project):
    added = client.post("/coding/projects", json={"path": str(project), "name": "demo"},
                        headers=token_headers(client)).json()["project"]
    (project / "index.html").write_text("<h1>the user edited this</h1>\n", encoding="utf-8")
    fx.write(project, "user-scratch.txt", "the user made this\n")

    body = client.get(f"/coding/projects/{added['id']}/diff").json()
    authors = {entry["path"]: entry["changed_by"] for entry in body["changed"]}
    assert authors["index.html"] == "you"
    assert authors["user-scratch.txt"] == "you"
    assert body["jarvis_paths"] == []


# ---------------------------------------------------------------------------
# Nothing leaks
# ---------------------------------------------------------------------------

def test_no_endpoint_returns_a_secret_from_the_project(client, project):
    added = client.post("/coding/projects", json={"path": str(project), "name": "demo"},
                        headers=token_headers(client)).json()["project"]
    plan = client.post("/coding/tasks/plan",
                       json={"project_id": added["id"], "request": "x"},
                       headers=token_headers(client)).json()

    responses = [
        client.get("/coding/status").text,
        client.get("/coding/projects").text,
        client.get(f"/coding/projects/{added['id']}/diff").text,
        client.get(f"/coding/projects/{added['id']}/git").text,
        client.get("/coding/tasks").text,
        json.dumps(plan),
        client.get(f"/coding/tasks/{plan['plan']['task_id']}/report").text,
    ]
    everything = "\n".join(responses)
    for secret in fx.SECRET_VALUES:
        assert secret not in everything, f"a secret was served over HTTP: {secret[:12]}…"


def test_a_screenshot_name_cannot_escape_the_screenshot_directory(client):
    for name in ("../../etc/passwd", "..%2F..%2Fsecret.png", "/etc/passwd", "....//x.png"):
        response = client.get(f"/coding/screenshots/{name}")
        assert response.status_code in (404, 400), name


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def test_the_template_list_describes_each_one_without_promising_a_download(client):
    templates = client.get("/coding/templates").json()["templates"]
    assert templates
    for template in templates:
        assert template["key"] and template["title"] and template["description"]


def test_creating_a_project_writes_files_and_installs_nothing(client, tmp_path):
    response = client.post("/coding/projects/create",
                           json={"parent_path": str(tmp_path), "name": "my-new-site",
                                 "template": "static"},
                           headers=token_headers(client))
    assert response.status_code == 200
    created = Path(response.json()["project"]["root"])
    assert created.is_dir()
    assert (created / "index.html").is_file()
    assert not (created / "node_modules").exists(), "creating a project installed something"


@pytest.mark.parametrize("name", [
    "../escape", "..", "with/slash", "with\\backslash", "CON", "  ", "a" * 300,
    "name:stream", "trailing.",
])
def test_an_unsafe_project_name_is_refused(client, tmp_path, name):
    response = client.post("/coding/projects/create",
                           json={"parent_path": str(tmp_path), "name": name,
                                 "template": "static"},
                           headers=token_headers(client))
    assert response.status_code in (400, 422), f"{name!r} was accepted"
    assert not (tmp_path.parent / "escape").exists()


def test_creating_into_a_parent_that_does_not_exist_is_refused(client, tmp_path):
    response = client.post("/coding/projects/create",
                           json={"parent_path": str(tmp_path / "nope"), "name": "x",
                                 "template": "static"},
                           headers=token_headers(client))
    assert response.status_code == 400


def test_an_unknown_template_is_refused(client, tmp_path):
    response = client.post("/coding/projects/create",
                           json={"parent_path": str(tmp_path), "name": "x",
                                 "template": "definitely-not-a-template"},
                           headers=token_headers(client))
    assert response.status_code == 400
