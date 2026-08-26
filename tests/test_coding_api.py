"""The Coding Workspace HTTP surface.

What is checked here is not that the endpoints work — the modules under
them are tested directly. It is the things only visible at the boundary:
that every browser-facing read and mutation needs the session token, that a refusal explains itself,
that removing a project deletes nothing, and that no response ever
carries a secret or a raw exception.
"""

import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests import coding_fixtures as fx


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client whose project registry and task history live in tmp_path."""
    from app.coding import delivery, projects, tasks, undo

    monkeypatch.setattr(projects, "_registry_path", lambda: tmp_path / "projects.json")
    monkeypatch.setattr(tasks, "_tasks_path", lambda: tmp_path / "tasks.json")
    monkeypatch.setattr(undo, "_undo_root", lambda: tmp_path / "undo")
    monkeypatch.setattr(delivery, "data_dir", lambda: tmp_path / "data")

    from app.api.server import create_app
    from tests.conftest import prime_session

    return prime_session(TestClient(create_app()))


def token_headers(client) -> dict:
    return {"X-JARVIS-Session-Token": client.cookies.get("jarvis_session")}

def auth_get(client, path: str, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.update(token_headers(client))
    return client.request("GET", path, headers=headers, **kwargs)



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
    ("post", "/coding/projects/plan", {"parent_path": "/tmp", "name": "x",
                                       "template": "static"}),
    ("post", "/coding/projects/create", {"plan_id": "x"}),
    ("post", "/coding/projects/plan/anything/cancel", {}),
    ("post", "/coding/folder-dialog", {"purpose": "add_project"}),
    ("post", "/coding/folder-dialog/anything/cancel", {}),
    ("post", "/coding/tasks/plan", {"project_id": "x", "request": "y"}),
    ("post", "/coding/tasks/start", {"task_id": "x"}),
    ("post", "/coding/tasks/decide", {"task_id": "x", "granted": True}),
    ("post", "/coding/tasks/stop", {"task_id": "x"}),
    ("post", "/coding/tasks/commit", {"task_id": "x", "message": "m"}),
    ("post", "/coding/tasks/undo", {"task_id": "x"}),
    ("post", "/coding/tasks/clear", {}),
    ("post", "/coding/processes/stop-all", {}),
    ("post", "/coding/preview/plan", {"project_id": "x", "script": "dev"}),
    ("post", "/coding/preview/start", {"plan_id": "x"}),
    ("post", "/coding/tasks/export/plan", {"task_id": "x"}),
    ("post", "/coding/tasks/export", {"task_id": "x", "plan_id": "x"}),
    ("delete", "/coding/projects/anything", None),
    ("delete", "/coding/tasks/anything", None),
]


@pytest.mark.parametrize("method,path,body", MUTATING)
def test_every_mutating_endpoint_refuses_without_the_session_token(client, method, path, body):
    call = getattr(client, method)
    headers = {"X-JARVIS-Session-Token": ""}
    response = call(path, json=body, headers=headers) if body is not None else call(path, headers=headers)
    assert response.status_code in (401, 403), (
        f"{method.upper()} {path} accepted a request with no session token "
        f"({response.status_code})"
    )


@pytest.mark.parametrize("path", [
    "/coding/status", "/coding/projects", "/coding/tasks", "/coding/toolchain",
    "/coding/folder-dialog",
])
def test_sensitive_read_endpoints_require_the_session_token(client, path):
    response = client.get(path, headers={"X-JARVIS-Session-Token": ""})
    assert response.status_code in (401, 403)


@pytest.mark.parametrize("path", [
    "/coding/templates", "/coding/browser-check",
])
def test_capability_reads_are_protected_too(client, path):
    """These two were public on codex/coding-workspace-hardening, which
    branched before the security pass, and its test asserted so — reading
    them as generic capability metadata rather than personal data.

    They are protected here instead, because
    `test_optional_voice_and_coding_integration_gets_are_protected_when_present`
    is a blanket structural invariant over *every* GET in
    app.api.coding_routes and app.api.voice_routes. It exists so a route
    added later cannot quietly ship without authentication, and an
    invariant with two hand-maintained exceptions is not an invariant.

    Nothing is lost: neither is a bootstrap endpoint (only /health has to
    be public, because it is what issues the token), and the Coding page
    holds the session before it asks for either.
    """
    response = client.get(path, headers={"X-JARVIS-Session-Token": ""})
    assert response.status_code in (401, 403)


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
    body = auth_get(client, "/coding/status").json()
    disabled = " ".join(body["disabled_in_this_version"]).lower()
    for forbidden in ("push", "pull request", "merge", "deploy"):
        assert forbidden in disabled
    assert body["protected"]["filenames"]
    assert ".env" in body["protected"]["filenames"]
    assert body["capabilities"]
    assert body["risk_matrix"]


def test_status_is_disabled_until_a_project_exists(client, project):
    assert auth_get(client, "/coding/status").json()["enabled"] is False
    client.post("/coding/projects", json={"path": str(project), "name": "demo"},
                headers=token_headers(client))
    assert auth_get(client, "/coding/status").json()["enabled"] is True



class _FinishCodingProvider:
    def stream(self, messages, system_prompt, cancel=None):
        yield json.dumps({
            "thinking": "The setup path is healthy.",
            "proposals": [{
                "action": "finish_task",
                "summary": "Started and finished without changing files.",
                "unresolved": [],
            }],
        })


def _use_ready_coding_provider(monkeypatch):
    from app.coding import loop, service

    choice = loop.ProviderChoice(
        "scripted", "Scripted", "scripted-model",
        is_cloud=False, ready=True,
    )
    monkeypatch.setattr(
        service.loop,
        "resolve_provider",
        lambda: (_FinishCodingProvider(), choice),
    )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_a_planned_task_can_start_successfully_through_the_http_boundary(
    client, project, monkeypatch,
):
    from app.coding import tasks

    _use_ready_coding_provider(monkeypatch)
    added = client.post(
        "/coding/projects",
        json={"path": str(project), "name": "demo"},
        headers=token_headers(client),
    ).json()["project"]
    plan = client.post(
        "/coding/tasks/plan",
        json={"project_id": added["id"], "request": "inspect and finish"},
        headers=token_headers(client),
    ).json()["plan"]

    response = client.post(
        "/coding/tasks/start",
        json={"task_id": plan["task_id"]},
        headers=token_headers(client),
    )

    assert response.status_code == 200, response.text
    assert response.json()["started"] is True

    deadline = time.monotonic() + 5.0
    record = tasks.get(plan["task_id"])
    while record is not None and record.state == tasks.TaskState.RUNNING.value:
        assert time.monotonic() < deadline, "the scripted task did not finish"
        time.sleep(0.02)
        record = tasks.get(plan["task_id"])
    assert record is not None
    assert record.state == tasks.TaskState.COMPLETED.value


def test_a_setup_failure_removes_the_worktree_and_branch_it_just_created(
    client, project, monkeypatch,
):
    from app.coding import service

    _use_ready_coding_provider(monkeypatch)
    added = client.post(
        "/coding/projects",
        json={"path": str(project), "name": "demo"},
        headers=token_headers(client),
    ).json()["project"]
    plan = client.post(
        "/coding/tasks/plan",
        json={"project_id": added["id"], "request": "fail during setup"},
        headers=token_headers(client),
    ).json()["plan"]
    isolation = plan["isolation"]

    def fail_opening_message(*args, **kwargs):
        raise RuntimeError("injected setup failure")

    monkeypatch.setattr(service, "_opening_message", fail_opening_message)
    response = client.post(
        "/coding/tasks/start",
        json={"task_id": plan["task_id"]},
        headers=token_headers(client),
    )

    assert response.status_code == 409
    assert not Path(isolation["worktree_path"]).exists()
    branch_ref = f"refs/heads/{isolation['branch_name']}"
    assert fx.git(project, "show-ref", "--verify", branch_ref).returncode != 0


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
        body = auth_get(client, "/coding/status").json()
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

    live = auth_get(client, f"/coding/tasks/{plan['task_id']}/live").json()
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

    body = auth_get(client, f"/coding/projects/{added['id']}/diff").json()
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
        auth_get(client, "/coding/status").text,
        auth_get(client, "/coding/projects").text,
        auth_get(client, f"/coding/projects/{added['id']}/diff").text,
        auth_get(client, f"/coding/projects/{added['id']}/git").text,
        auth_get(client, "/coding/tasks").text,
        json.dumps(plan),
        auth_get(client, f"/coding/tasks/{plan['plan']['task_id']}/report").text,
    ]
    everything = "\n".join(responses)
    for secret in fx.SECRET_VALUES:
        assert secret not in everything, f"a secret was served over HTTP: {secret[:12]}…"


def test_a_screenshot_name_cannot_escape_the_screenshot_directory(client):
    for name in ("../../etc/passwd", "..%2F..%2Fsecret.png", "/etc/passwd", "....//x.png"):
        response = auth_get(client, f"/coding/screenshots/{name}")
        assert response.status_code in (404, 400), name


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def test_the_template_list_describes_each_one_without_promising_a_download(client):
    templates = auth_get(client, "/coding/templates").json()["templates"]
    assert templates
    for template in templates:
        assert template["key"] and template["title"] and template["description"]


def test_planning_a_project_writes_nothing_at_all(client, tmp_path):
    """Step one of two. The response describes; the disk is untouched."""
    before = sorted(p.name for p in tmp_path.iterdir())
    response = client.post("/coding/projects/plan",
                           json={"parent_path": str(tmp_path), "name": "my-new-site",
                                 "template": "static"},
                           headers=token_headers(client))
    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["destination"].endswith("my-new-site")
    assert plan["files"]
    assert plan["creatable"] is True
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not (tmp_path / "my-new-site").exists()


def test_cancelling_a_plan_leaves_nothing_behind(client, tmp_path):
    plan = client.post("/coding/projects/plan",
                       json={"parent_path": str(tmp_path), "name": "abandoned",
                             "template": "static"},
                       headers=token_headers(client)).json()["plan"]

    response = client.post(f"/coding/projects/plan/{plan['plan_id']}/cancel", json={},
                           headers=token_headers(client))
    assert response.status_code == 200
    assert response.json()["files_created"] == 0
    assert not (tmp_path / "abandoned").exists()

    # And the cancelled plan cannot then be confirmed.
    confirmed = client.post("/coding/projects/create",
                            json={"plan_id": plan["plan_id"]},
                            headers=token_headers(client))
    assert confirmed.status_code == 400
    assert not (tmp_path / "abandoned").exists()


def test_creating_a_project_needs_a_plan_and_takes_nothing_else(client, tmp_path):
    """Step two takes a plan id and no destination of its own — otherwise
    it would be a one-step create with an extra field, and the plan the
    user read would not be the thing that ran."""
    smuggled = client.post("/coding/projects/create",
                           json={"parent_path": str(tmp_path), "name": "sneaky",
                                 "template": "static"},
                           headers=token_headers(client))
    assert smuggled.status_code == 422
    assert not (tmp_path / "sneaky").exists()


def test_confirming_a_plan_writes_files_and_installs_nothing(client, tmp_path):
    plan = client.post("/coding/projects/plan",
                       json={"parent_path": str(tmp_path), "name": "my-new-site",
                             "template": "static"},
                       headers=token_headers(client)).json()["plan"]

    response = client.post("/coding/projects/create", json={"plan_id": plan["plan_id"]},
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
    response = client.post("/coding/projects/plan",
                           json={"parent_path": str(tmp_path), "name": name,
                                 "template": "static"},
                           headers=token_headers(client))
    assert response.status_code in (400, 422), f"{name!r} was accepted"
    assert not (tmp_path.parent / "escape").exists()


def test_creating_into_a_parent_that_does_not_exist_is_refused(client, tmp_path):
    response = client.post("/coding/projects/plan",
                           json={"parent_path": str(tmp_path / "nope"), "name": "x",
                                 "template": "static"},
                           headers=token_headers(client))
    assert response.status_code == 400


def test_an_unknown_template_is_refused(client, tmp_path):
    response = client.post("/coding/projects/plan",
                           json={"parent_path": str(tmp_path), "name": "x",
                                 "template": "definitely-not-a-template"},
                           headers=token_headers(client))
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# The native folder dialog, at the HTTP boundary
#
# The dialog itself is Windows'. What is checked here is the brokering:
# who may ask for one, who may answer, and what a page can learn.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def a_clean_folder_broker():
    from app.coding import folder_requests

    folder_requests.broker.clear()
    yield
    folder_requests.broker.clear()


def desktop_headers(monkeypatch) -> dict:
    from app.launcher.server_process import SESSION_SECRET_ENV

    monkeypatch.setenv(SESSION_SECRET_ENV, "a-test-desktop-secret")
    return {"X-JARVIS-Desktop-Secret": "a-test-desktop-secret"}


def test_a_page_can_mint_a_folder_request_but_not_answer_it(client, monkeypatch, tmp_path):
    minted = client.post("/coding/folder-dialog", json={"purpose": "add_project"},
                         headers=token_headers(client))
    assert minted.status_code == 200
    request_id = minted.json()["request"]["request_id"]

    # The session token is not the credential for reporting a result.
    forged = client.post(f"/coding/folder-dialog/{request_id}/result",
                         json={"path": str(tmp_path)},
                         headers=token_headers(client))
    assert forged.status_code == 403

    state = auth_get(client, f"/coding/folder-dialog/{request_id}",
                       headers=token_headers(client)).json()["request"]
    assert state["state"] == "pending"
    assert state["path"] == ""


def test_an_unauthenticated_result_is_refused(client, tmp_path):
    minted = client.post("/coding/folder-dialog", json={"purpose": "add_project"},
                         headers=token_headers(client))
    request_id = minted.json()["request"]["request_id"]

    response = client.post(f"/coding/folder-dialog/{request_id}/result",
                           json={"path": str(tmp_path)})
    assert response.status_code == 403


def test_a_result_with_the_desktop_secret_is_accepted_once(client, monkeypatch, tmp_path):
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    headers = desktop_headers(monkeypatch)
    request_id = client.post("/coding/folder-dialog", json={"purpose": "add_project"},
                             headers=token_headers(client)).json()["request"]["request_id"]

    first = client.post(f"/coding/folder-dialog/{request_id}/result",
                        json={"path": str(chosen)}, headers=headers)
    assert first.status_code == 200
    # The window is the thing that sent the path; it does not get it back.
    assert first.json()["request"]["path"] == ""

    second = client.post(f"/coding/folder-dialog/{request_id}/result",
                         json={"path": str(tmp_path)}, headers=headers)
    assert second.status_code == 409

    shown = auth_get(client, f"/coding/folder-dialog/{request_id}",
                       headers=token_headers(client)).json()["request"]
    assert shown["state"] == "selected"
    assert shown["path"] == str(chosen.resolve())


def test_a_picked_folder_becomes_a_project_and_is_spent(client, monkeypatch, tmp_path):
    chosen = fx.static_site(tmp_path / "picked", with_defect=False)
    headers = desktop_headers(monkeypatch)
    request_id = client.post("/coding/folder-dialog", json={"purpose": "add_project"},
                             headers=token_headers(client)).json()["request"]["request_id"]
    client.post(f"/coding/folder-dialog/{request_id}/result",
                json={"path": str(chosen)}, headers=headers)

    added = client.post("/coding/projects", json={"request_id": request_id},
                        headers=token_headers(client))
    assert added.status_code == 200
    assert added.json()["selected_via_picker"] is True
    assert added.json()["project"]["root"] == str(chosen.resolve())

    reused = client.post("/coding/projects", json={"request_id": request_id},
                         headers=token_headers(client))
    assert reused.status_code == 400


def test_a_typed_path_is_never_reported_as_picked(client, tmp_path):
    """§4: do not claim a folder was selected through the picker when it
    was typed. Only the server can tell, so only the server may say."""
    typed = fx.static_site(tmp_path / "typed", with_defect=False)
    added = client.post("/coding/projects",
                        json={"path": str(typed), "name": "typed"},
                        headers=token_headers(client))
    assert added.status_code == 200
    assert added.json()["selected_via_picker"] is False


def test_a_cancelled_dialog_registers_nothing(client, monkeypatch, tmp_path):
    headers = desktop_headers(monkeypatch)
    request_id = client.post("/coding/folder-dialog", json={"purpose": "add_project"},
                             headers=token_headers(client)).json()["request"]["request_id"]

    client.post(f"/coding/folder-dialog/{request_id}/result",
                json={"cancelled": True}, headers=headers)

    added = client.post("/coding/projects", json={"request_id": request_id},
                        headers=token_headers(client))
    assert added.status_code == 400
    assert auth_get(client, "/coding/projects").json()["projects"] == []


def test_a_second_dialog_is_refused_while_one_is_open(client):
    client.post("/coding/folder-dialog", json={"purpose": "add_project"},
                headers=token_headers(client))
    second = client.post("/coding/folder-dialog", json={"purpose": "add_project"},
                         headers=token_headers(client))
    assert second.status_code == 409


def test_an_unknown_purpose_is_refused(client):
    response = client.post("/coding/folder-dialog", json={"purpose": "read_my_disk"},
                           headers=token_headers(client))
    assert response.status_code == 400


def test_availability_is_reported_from_the_proved_window_state(client):
    """The server has no window of its own and cannot see one; it reports
    what the parent proved, or says a dialog is not available."""
    body = auth_get(client, "/coding/folder-dialog").json()
    assert body["available"] is False
    assert body["reason"], "an unavailable picker must say why"


# ---------------------------------------------------------------------------
# Readiness hardening
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint,payload", [
    ("/coding/tasks/undo", lambda task: {"task_id": task}),
    ("/coding/tasks/commit", lambda task: {"task_id": task, "message": "x", "approved": True}),
])
def test_missing_isolated_worktree_never_falls_back_to_main(
    client, project, tmp_path, endpoint, payload,
):
    from app.coding import tasks
    added = client.post(
        "/coding/projects", json={"path": str(project), "name": "demo"},
        headers=token_headers(client),
    ).json()["project"]
    original = (project / "index.html").read_bytes()
    record = tasks.create(added["id"], "isolated task")
    record.isolation = {
        "strategy": "worktree", "worktree_path": str(tmp_path / "missing-worktree"),
        "start_sha": "0" * 40,
    }
    record.files_changed = [{
        "path": "index.html", "kind": "update",
        "before_sha256": hashlib.sha256(original).hexdigest(),
        "after_sha256": hashlib.sha256(original).hexdigest(),
    }]
    tasks.save(record)
    response = client.post(endpoint, json=payload(record.id), headers=token_headers(client))
    assert response.status_code == 409
    assert (project / "index.html").read_bytes() == original


def test_preview_confirmation_is_invalidated_when_package_script_changes(
    client, tmp_path,
):
    root = fx.vite_react_ts(tmp_path / "vite")
    fx.init_repo(root)
    added = client.post(
        "/coding/projects", json={"path": str(root), "name": "vite"},
        headers=token_headers(client),
    ).json()["project"]
    planned = client.post(
        "/coding/preview/plan",
        json={"project_id": added["id"], "script": "dev"},
        headers=token_headers(client),
    )
    assert planned.status_code == 200
    plan = planned.json()["plan"]
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    package["scripts"]["dev"] = "node changed-after-review.js"
    (root / "package.json").write_text(json.dumps(package), encoding="utf-8")
    started = client.post(
        "/coding/preview/start", json={"plan_id": plan["plan_id"]},
        headers=token_headers(client),
    )
    assert started.status_code == 409
    assert "changed" in started.json()["detail"].lower()


def test_isolated_task_diff_and_approved_export_are_downloadable(
    client, project,
):
    from app.coding import gitsafe, tasks
    added = client.post(
        "/coding/projects", json={"path": str(project), "name": "demo"},
        headers=token_headers(client),
    ).json()["project"]
    record = tasks.create(added["id"], "create export")
    plan = gitsafe.plan_isolation(project, record.id)
    assert gitsafe.create_worktree(project, plan)[0] is True
    worktree = Path(plan.worktree_path)
    payload = b"created in isolated task\n"
    (worktree / "delivered.txt").write_bytes(payload)
    record.isolation = plan.as_dict()
    record.files_changed = [{
        "path": "delivered.txt", "destination": "", "kind": "create",
        "before_sha256": None, "after_sha256": hashlib.sha256(payload).hexdigest(),
    }]
    tasks.save(record)
    review = auth_get(client, f"/coding/tasks/{record.id}/diff")
    assert review.status_code == 200
    assert "delivered.txt" in review.json()["diff"]
    planned = client.post(
        "/coding/tasks/export/plan", json={"task_id": record.id},
        headers=token_headers(client),
    ).json()["plan"]
    exported = client.post(
        "/coding/tasks/export",
        json={"task_id": record.id, "plan_id": planned["plan_id"]},
        headers=token_headers(client),
    )
    assert exported.status_code == 200
    download = auth_get(client, exported.json()["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/zip")
    assert not (project / "delivered.txt").exists()


def test_screenshot_bytes_are_not_public(client):
    """The empty header is not decoration.

    `client` is primed, and httpx merges the client's default headers
    into `.request(...)` — so the original form of this test sent a
    perfectly valid token and got 404 ("no such screenshot") from *behind*
    the gate. It would have passed just as happily against a route with no
    authentication at all, which is the one thing it exists to rule out.
    Overriding the header is what makes the request genuinely anonymous.
    """
    response = client.get(
        "/coding/screenshots/anything.png",
        headers={"X-JARVIS-Session-Token": ""},
    )
    assert response.status_code in (401, 403)


@pytest.mark.parametrize("failure_point", ["register", "thread"])
def test_every_post_creation_start_failure_cleans_isolation(
    client, project, monkeypatch, failure_point,
):
    from app.coding import service, sessions
    _use_ready_coding_provider(monkeypatch)
    added = client.post(
        "/coding/projects", json={"path": str(project), "name": "demo"},
        headers=token_headers(client),
    ).json()["project"]
    plan = client.post(
        "/coding/tasks/plan",
        json={"project_id": added["id"], "request": "inject setup failure"},
        headers=token_headers(client),
    ).json()["plan"]
    if failure_point == "register":
        monkeypatch.setattr(
            sessions, "register",
            lambda runner: (_ for _ in ()).throw(RuntimeError("register failed")),
        )
    else:
        # `service.threading` *is* the stdlib threading module, so replacing
        # its Thread replaces it for the whole process — including code we
        # are not testing. On Windows `subprocess` reads a child's pipes on
        # reader threads, so the git commands inside the cleanup path this
        # test exists to check tried to start a BrokenThread of their own.
        # The RuntimeError escaped `start_task`'s except block, the route
        # fell through to its generic handler, and the assertion read
        # `400 == 409` on Windows CI while passing on Linux, where
        # subprocess uses selectors and starts no thread at all.
        #
        # Only the task's own thread is broken now, matched on the name
        # service.py gives it. Everything else gets a real Thread, so the
        # cleanup being asserted below actually runs.
        real_thread = service.threading.Thread
        task_thread_name = f"coding-{plan['task_id'][:8]}"

        def _thread(*args, **kwargs):
            if kwargs.get("name") == task_thread_name:
                class BrokenThread:
                    def start(self):
                        raise RuntimeError("thread start failed")
                return BrokenThread()
            return real_thread(*args, **kwargs)

        monkeypatch.setattr(service.threading, "Thread", _thread)
    response = client.post(
        "/coding/tasks/start", json={"task_id": plan["task_id"]},
        headers=token_headers(client),
    )
    assert response.status_code == 409
    assert not Path(plan["isolation"]["worktree_path"]).exists()
    ref = f"refs/heads/{plan['isolation']['branch_name']}"
    assert fx.git(project, "show-ref", "--verify", ref).returncode != 0
    assert sessions.get(plan["task_id"]) is None


def test_running_task_history_cannot_be_deleted_or_cleared(
    client, project, monkeypatch,
):
    from app.coding import sessions, tasks
    added = client.post(
        "/coding/projects", json={"path": str(project), "name": "demo"},
        headers=token_headers(client),
    ).json()["project"]
    record = tasks.create(added["id"], "still running")
    monkeypatch.setattr(sessions, "get", lambda task_id: object() if task_id == record.id else None)
    monkeypatch.setattr(sessions, "live_ids", lambda: [record.id])
    deleted = client.delete(
        f"/coding/tasks/{record.id}", headers=token_headers(client)
    )
    cleared = client.post(
        "/coding/tasks/clear", headers=token_headers(client)
    )
    assert deleted.status_code == 409
    assert cleared.status_code == 409
    assert tasks.get(record.id) is not None
