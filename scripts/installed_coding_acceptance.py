"""Coding Workspace, exercised through the installed Windows application.

This exists because the previous pass shipped a browser check that worked
in a source checkout and reported `available: false` on every machine a
user actually had. Every test that covered it ran against the repository,
where Playwright was installed — so the one fact that mattered, *does
this work in the thing people download*, was the one nothing checked.

So this phase drives the installed `JARVIS.exe` over its own HTTP API,
using only synthetic fixtures written into a temporary folder, and
asserts on results that cannot be produced without the real thing:

* Browser QA must reach `passed` or `failed` — a state only assigned
  after a real browser loaded the page and the probes returned — and must
  name the engine that ran it.
* The defective fixture must produce **specific non-zero counts**. A
  check that found the two `<h1>`s and nothing else would satisfy
  "some problems were found" while missing the console error, the broken
  image and the overflow entirely.
* The clean fixture must produce **zero**, and only after `opened` is
  true. Zero from a check that did not run is the defect this whole
  subsystem exists to prevent.

**The source checkout's Playwright must not be able to help.** The
installed application is a frozen executable with its own interpreter and
its own site-packages; it cannot import from the repository. Step 3
records which engine and which packaged files answered, and the phase
fails if Playwright is what the packaged product used.

**No cloud API is called.** Tasks are never started; the plan, preview,
browser-check, patch and diff surfaces are driven directly. That keeps
the phase deterministic and free of an API key.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Derived, never duplicated. A hardcoded `http://127.0.0.1:8000` here sent
# every request in this phase to a port nothing was listening on, while
# the app itself was healthy on 5555 — the failure read as "the installed
# application died immediately after reporting ready", which is a much
# more alarming thing than it was.
from app.config import settings  # noqa: E402

BASE_URL = f"http://{settings.jarvis_host}:{settings.jarvis_port}"

#: A real 1x1 PNG, so an image that is *supposed* to load does.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

#: Exactly what §7 asks the fixture to contain: an intentional console
#: error, one broken image, horizontal overflow at 320px, two <h1>
#: elements, and a harmless accessibility violation.
DEFECTIVE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Corner Shop</title></head>
<body>
  <h1>Corner Shop</h1>
  <h1>Opening hours</h1>
  <img src="/shopfront.png">
  <div style="width:1900px;background:#eee">Wider than a phone.</div>
  <script>console.error("checkout total is NaN");</script>
</body></html>
"""

CLEAN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Corner Shop</title></head>
<body>
  <h1>Corner Shop</h1>
  <p>Open every day.</p>
  <img src="/logo.png" alt="The Corner Shop logo">
</body></html>
"""

#: A page that tries to leave loopback, so the boundary is exercised in
#: the installed product rather than only in the test suite.
HOSTILE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Away</title></head>
<body><h1>Away</h1>
<script>location.href = "https://example.com/landing";</script>
</body></html>
"""

#: A protected file. Its contents must never appear in a diff, a task
#: record, a screenshot name or any response.
SECRET_MARKER = "sk-ant-api03-" + "Z" * 95


def _step(text: str) -> None:
    print(f"\n--- {text}", flush=True)


def _fail(text: str) -> None:
    print(f"\nFAILED: {text}", flush=True)
    raise SystemExit(1)


def _client(attempts: int = 20):
    """A session-authenticated client, waiting out a startup refusal.

    Bounded rather than immediate: the readiness signal above is the real
    gate, and this is the belt to its braces. A single attempt turned a
    two-second window into a failed acceptance run.
    """
    import httpx

    client = httpx.Client(base_url=BASE_URL, timeout=180.0)
    last = ""
    for _ in range(attempts):
        try:
            client.get("/health")              # mints the session cookie
            token = client.cookies.get("jarvis_session")
            if token:
                client.headers["X-JARVIS-Session-Token"] = token
                return client
            last = "no session token was issued"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}"
        time.sleep(0.5)
    _fail(f"The installed app never issued a session token ({last}), "
          "so nothing could be driven.")
    return client


def _write_fixture(root: Path) -> None:
    """A synthetic static site, plus a protected file and a Git history."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(DEFECTIVE_PAGE, encoding="utf-8", newline="\n")
    (root / "clean.html").write_text(CLEAN_PAGE, encoding="utf-8", newline="\n")
    (root / "away.html").write_text(HOSTILE_PAGE, encoding="utf-8", newline="\n")
    (root / "logo.png").write_bytes(_TINY_PNG)
    # Deliberately protected. Never read, never diffed, never recorded.
    (root / ".env").write_text(f"ANTHROPIC_API_KEY={SECRET_MARKER}\n",
                               encoding="utf-8", newline="\n")
    # The port comes from the environment, not from argv. That is not a
    # shortcut: `preview.start()` sets PORT, HOST and BROWSER *and* pins
    # the same values on the command line, because different dev servers
    # honour different ones — and a script launched through `npm run` sees
    # the environment but not the flags npm swallowed.
    (root / "serve.py").write_text(
        "import os\n"
        "from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer\n"
        "class H(SimpleHTTPRequestHandler):\n"
        "    def log_message(self, *a): pass\n"
        "port = int(os.environ.get('PORT', '0'))\n"
        "ThreadingHTTPServer((os.environ.get('HOST', '127.0.0.1'), port), H)"
        ".serve_forever()\n",
        encoding="utf-8", newline="\n")
    # A declared dev script, because JARVIS starts only what a project
    # declares and will not guess a command. The interpreter is named by
    # full path so the fixture does not depend on `python` being on PATH.
    (root / "package.json").write_text(json.dumps({
        "name": "corner-shop", "private": True, "version": "0.0.0",
        "scripts": {"dev": f'"{sys.executable}" serve.py'},
    }, indent=2) + "\n", encoding="utf-8", newline="\n")

    env = dict(os.environ, GIT_AUTHOR_NAME="JARVIS Test",
               GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="JARVIS Test",
               GIT_COMMITTER_EMAIL="test@example.invalid")
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=JARVIS Test", "-c", "user.email=test@example.invalid",
                  "commit", "-q", "-m", "fixture"]):
        subprocess.run(["git", *args], cwd=str(root), env=env,  # noqa: S603
                       capture_output=True, shell=False, timeout=60)


# ---------------------------------------------------------------------------
# The phase
# ---------------------------------------------------------------------------

def run(exe_path: Path, log_dir: Path, wait_for_health, wait_for_health_to_stop,
        wait_for_pid_exit, wait_for_desktop_ready) -> None:
    """Steps 1-25 of §7, against the installed executable."""
    workspace = Path(tempfile.mkdtemp(prefix="jarvis-coding-acceptance-"))
    project_root = workspace / "corner-shop"
    parent_for_new = workspace / "new-projects"
    parent_for_new.mkdir(parents=True, exist_ok=True)
    _write_fixture(project_root)

    _step("Step 1: Launch the installed application")
    proc = subprocess.Popen([str(exe_path)], cwd=str(exe_path.parent))
    screenshot_name = ""
    preview_pid = None
    try:
        wait_for_health(proc)
        # And then the parent's own readiness signal. `/health` answers as
        # soon as the *server child* is up, several seconds before the
        # parent has finished starting — and a launch that arrives during
        # that gap can still be handed off to a previous instance that is
        # on its way out, leaving nothing listening. This phase learned
        # that the same way the lifecycle phases did: health answered,
        # and two seconds later the connection was refused.
        ready = wait_for_desktop_ready()
        print(f"  desktop ready (session {ready.get('session_id', '?')})")

        _step("Step 2: Authenticate through the normal session mechanism")
        client = _client()

        _step("Step 3: Open Coding Workspace and confirm it is off until a project exists")
        status = client.get("/coding/status").json()
        if status.get("enabled") is not False:
            _fail("Coding Workspace reported itself enabled with no project added.")
        for excluded in ("git push", "merge", "deployment"):
            if excluded not in " ".join(status.get("disabled_in_this_version", [])):
                _fail(f"GET /coding/status does not publish that {excluded} is excluded.")

        _step("Step 4: Exercise the folder-selection boundary through its own IPC path")
        _folder_dialog_boundary(client, project_root)

        _step("Step 5: Add the synthetic project")
        added = client.post("/coding/projects",
                            json={"path": str(project_root), "name": "Corner Shop"})
        if added.status_code != 200:
            _fail(f"Adding the fixture project failed: {added.status_code} {added.text[:300]}")
        project = added.json()["project"]
        if added.json().get("selected_via_picker") is not False:
            _fail("A typed path was reported as chosen through the picker.")
        project_id = project["id"]

        _step("Step 6: Confirm the ordinary assistant gained nothing")
        _assert_isolated_from_chat(client)

        _step("Step 7: Generate a creation plan, and confirm planning created nothing")
        before = sorted(p.name for p in parent_for_new.iterdir())
        planned = client.post("/coding/projects/plan",
                              json={"parent_path": str(parent_for_new),
                                    "name": "brand-new", "template": "static"})
        if planned.status_code != 200:
            _fail(f"Planning a project failed: {planned.status_code} {planned.text[:300]}")
        plan = planned.json()["plan"]
        if not plan["files"] or not plan["destination"].endswith("brand-new"):
            _fail("The creation plan did not describe the destination and files.")
        if (parent_for_new / "brand-new").exists():
            _fail("Planning a project created the destination folder.")
        if sorted(p.name for p in parent_for_new.iterdir()) != before:
            _fail("Planning a project changed the parent folder.")
        print(f"OK: plan describes {plan['file_count']} file(s) at {plan['destination']}")

        _step("Step 8: Cancel the plan and prove nothing was created")
        client.post(f"/coding/projects/plan/{plan['plan_id']}/cancel", json={})
        if (parent_for_new / "brand-new").exists():
            _fail("Cancelling a plan created the project anyway.")
        if sorted(p.name for p in parent_for_new.iterdir()) != before:
            _fail("Cancelling a plan changed the parent folder.")

        _step("Step 9: Re-plan and confirm creation")
        plan = client.post("/coding/projects/plan",
                           json={"parent_path": str(parent_for_new),
                                 "name": "brand-new", "template": "static"}).json()["plan"]
        created = client.post("/coding/projects/create", json={"plan_id": plan["plan_id"]})
        if created.status_code != 200:
            _fail(f"Confirming a plan failed: {created.status_code} {created.text[:300]}")
        made = Path(created.json()["project"]["root"])
        if not (made / "index.html").is_file():
            _fail("Confirming the plan did not write the files the plan described.")
        if (made / "node_modules").exists():
            _fail("Creating a project installed dependencies.")
        print(f"OK: created {len(list(made.rglob('*')))} entries, installed nothing")

        _step("Step 10: Report the toolchain without changing this machine")
        tools = client.get(f"/coding/toolchain?project_id={project_id}").json()
        if not tools.get("tools"):
            _fail("The toolchain report was empty.")
        if not tools.get("nothing_was_installed"):
            _fail("The toolchain report does not state that nothing was installed.")
        for tool in tools["tools"]:
            if tool["state"] == "available" and not tool["version"]:
                _fail(f"{tool['display']} is reported available with no version.")
        print("OK: " + ", ".join(
            f"{t['display']}={t['state']}" for t in tools["tools"]))

        _step("Step 11-16: Start the owned preview and run browser QA from the installed product")
        preview = _start_preview(client, project_id, project_root)
        preview_pid = preview.get("pid")

        findings = _browser_check(client, project_id, "/")
        _assert_real_defects(findings)
        screenshot_name = findings.get("screenshot", "")

        _step("Step 17: Confirm the screenshot was written and is served")
        if not screenshot_name:
            _fail("The browser check produced no screenshot.")
        shot = client.get(f"/coding/screenshots/{screenshot_name}")
        if shot.status_code != 200 or not shot.content.startswith(b"\x89PNG"):
            _fail("The screenshot was not served as a PNG by the installed product.")
        print(f"OK: screenshot {screenshot_name} is {len(shot.content)} bytes")

        _step("Step 18: A page that tries to leave loopback is blocked, in the installed product")
        blocked = _browser_check(client, project_id, "/away.html")
        if blocked.get("state") != "blocked":
            _fail(f"A page navigating to example.com was not blocked: {blocked.get('state')} "
                  f"— {blocked.get('reason')}")
        if not blocked.get("blocked_origins"):
            _fail("The blocked navigation was not recorded.")
        if blocked.get("console_errors") is not None:
            _fail("A blocked check reported counts, which means something inspected the page.")
        print(f"OK: {blocked['reason'][:120]}")

        _step("Step 19: A clean page reports zero, and only after a real check")
        clean = _browser_check(client, project_id, "/clean.html")
        if not clean.get("opened"):
            _fail("The clean page's result was produced without a browser opening it.")
        if clean.get("state") != "passed":
            _fail(f"The clean fixture did not pass: {clean.get('reason')} "
                  f"{clean.get('console_messages')}")
        for field in ("console_errors", "page_errors", "failed_requests",
                      "broken_images", "accessibility_findings"):
            if clean.get(field) != 0:
                _fail(f"The clean fixture reported {field}={clean.get(field)}, expected 0.")
        print(f"OK: clean page passed with {clean['engine']}")

        _step("Step 20: Stop the preview and confirm every process tree is gone")
        _stop_everything(client)
        _assert_no_leftovers(preview_pid)

        _step("Step 21: A safe patch, its diff, and the protected file")
        _assert_protected_file_never_read(client, project_id, project_root)

        _step("Step 22: Task history carries no secret")
        _assert_history_is_redacted(client)
    finally:
        _step("Step 23: Quit JARVIS")
        subprocess.run(["taskkill", "/PID", str(proc.pid)], capture_output=True, text=True)
        wait_for_pid_exit(proc.pid)
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(workspace, ignore_errors=True)

    wait_for_health_to_stop()

    _step("Step 24: Confirm no browser, driver or preview process survived the quit")
    _assert_no_leftovers(preview_pid)

    _step("Step 25: Record which packaged runtime answered")
    _record_provenance(exe_path, log_dir)

    print("\nOK: Coding Workspace works in the installed application")


# ---------------------------------------------------------------------------
# Steps that are worth their own function
# ---------------------------------------------------------------------------

def _folder_dialog_boundary(client, real_folder: Path) -> None:
    """The same authenticated IPC path the native window uses.

    The dialog itself is Windows'; nothing here can draw one. What is
    driven is everything around it — who may ask, who may answer, and
    whether a page can forge a result — through the identical endpoints
    the window child posts to.
    """
    availability = client.get("/coding/folder-dialog").json()
    print(f"  native dialog available: {availability.get('available')}"
          + (f" ({availability.get('reason')})" if availability.get("reason") else ""))

    minted = client.post("/coding/folder-dialog", json={"purpose": "add_project"})
    if minted.status_code != 200:
        _fail(f"Minting a folder request failed: {minted.status_code} {minted.text[:200]}")
    request_id = minted.json()["request"]["request_id"]

    second = client.post("/coding/folder-dialog", json={"purpose": "add_project"})
    if second.status_code != 409:
        _fail("A second folder dialog was allowed while one was pending.")

    # The page's own session token must not be able to answer.
    forged = client.post(f"/coding/folder-dialog/{request_id}/result",
                         json={"path": str(real_folder)})
    if forged.status_code != 403:
        _fail(f"A forged folder-dialog result was accepted ({forged.status_code}).")

    state = client.get(f"/coding/folder-dialog/{request_id}").json()["request"]
    if state["state"] != "pending" or state["path"]:
        _fail("A forged result changed the folder request.")

    client.post(f"/coding/folder-dialog/{request_id}/cancel", json={})
    print("OK: unauthenticated and session-token results are both refused")


def _assert_isolated_from_chat(client) -> None:
    """Coding capabilities must not appear in the ordinary assistant."""
    payload = client.get("/tools").json()
    # The endpoint has returned both a bare list and a wrapper over the
    # product's life; accept either rather than asserting on the shape of
    # something this phase does not own.
    entries = payload.get("tools", []) if isinstance(payload, dict) else payload
    names = json.dumps(entries).lower()
    for leaked in ("edit_file", "run_command", "start_preview", "browser_check",
                   "apply_patch", "write_file"):
        if leaked in names:
            _fail(f"Coding capability '{leaked}' is registered in the ordinary tool registry.")
    print(f"OK: {len(entries)} ordinary tools, none of them coding tools")


def _start_preview(client, project_id: str, root: Path) -> dict:
    """Start the project's own declared dev server, through the product."""
    import httpx

    # Starting a preview is a two-step review-then-confirm flow: /preview/plan
    # describes the exact script body without running it, and /preview/start
    # consumes that reviewed plan by id. Confirming by project_id instead
    # would ask the product to run whatever the script says *now* rather than
    # what was reviewed, which is the check this flow exists to make.
    planned = client.post(
        "/coding/preview/plan", json={"project_id": project_id, "script": "dev"}
    )
    if planned.status_code == 404:
        # The preview is driven by a task in this build. Start the fixture
        # server the way the product would and register it, so the browser
        # check still runs against an owned loopback preview.
        _fail("This build has no direct preview endpoint; the acceptance phase needs one.")
    if planned.status_code != 200:
        _fail(f"Planning the preview failed: {planned.status_code} {planned.text[:300]}")
    plan = planned.json().get("plan", {})
    plan_id = plan.get("plan_id")
    if not plan_id:
        _fail(f"The preview plan carried no plan_id: {planned.text[:300]}")
    print(f"OK: preview plan {plan_id[:8]}… describes {plan.get('argv')}")

    started = client.post("/coding/preview/start", json={"plan_id": plan_id})
    if started.status_code != 200:
        _fail(f"Starting the preview failed: {started.status_code} {started.text[:300]}")
    state = started.json().get("preview", started.json())
    if not state.get("running"):
        _fail(f"The preview did not come up: {state.get('last_error')}")
    if state.get("bound_to") != "127.0.0.1":
        _fail(f"The preview is bound to {state.get('bound_to')}, not loopback.")
    print(f"OK: preview running on {state.get('url')} (pid {state.get('pid')})")
    return state


def _browser_check(client, project_id: str, route: str) -> dict:
    response = client.post("/coding/preview/check",
                           json={"project_id": project_id, "route": route})
    if response.status_code != 200:
        _fail(f"The browser check failed to run: {response.status_code} {response.text[:300]}")
    return response.json().get("browser", response.json())


def _assert_real_defects(findings: dict) -> None:
    """The heart of the phase. Every assertion here is a number a real
    browser had to produce."""
    if findings.get("state") == "engine_unavailable":
        _fail("Browser QA reported no engine in the installed product. "
              f"{findings.get('reason')} — {findings.get('fix')}")
    if not findings.get("opened"):
        _fail(f"Browser QA did not open the page: {findings.get('state')} "
              f"— {findings.get('reason')}")
    if not findings.get("engine"):
        _fail("Browser QA produced a result without naming the engine that ran it.")
    if "playwright" in str(findings.get("engine", "")).lower():
        _fail("The packaged product used Playwright, which it must not carry.")

    expected = {
        "http_status": 200,
        "h1_count": 2,
        "console_errors": 1,
        "broken_images": 1,
    }
    for field, want in expected.items():
        if findings.get(field) != want:
            _fail(f"Browser QA reported {field}={findings.get(field)}, expected {want}. "
                  f"Full result: {json.dumps(findings)[:600]}")
    if findings.get("title") != "Corner Shop":
        _fail(f"The page title was read as {findings.get('title')!r}.")
    overflow = (findings.get("horizontal_overflow") or {}).get("320") or {}
    if not overflow.get("overflows"):
        _fail("Horizontal overflow at 320px was not detected.")
    if not findings.get("accessibility_findings"):
        _fail("The accessibility check found nothing on a page with two <h1>s and "
              "an image with no alt text.")
    if findings.get("state") != "failed":
        _fail(f"A page with five defects was reported as {findings.get('state')}.")
    print(f"OK: {findings['engine']} found {findings['problem_count']} real problems "
          f"({findings['console_errors']} console, {findings['broken_images']} broken image, "
          f"{findings['accessibility_findings']} accessibility, overflow at 320px)")


def _stop_everything(client) -> None:
    stopped = client.post("/coding/processes/stop-all", json={})
    if stopped.status_code != 200:
        _fail(f"Stopping coding processes failed: {stopped.status_code}")
    report = stopped.json()
    survivors = []
    for entry in report.get("stopped", []) or []:
        survivors += entry.get("survivors", []) or []
    if survivors:
        _fail(f"Stop-all left processes running: {survivors}")
    print("OK: every owned process tree reported terminated")


def _assert_no_leftovers(preview_pid) -> None:
    """No browser, driver or preview process may survive.

    Named rather than counted: "three unexpected processes" is not
    something anyone can act on.
    """
    try:
        import psutil
    except ImportError:
        print("  psutil is not available; skipping the process sweep")
        return

    leftovers = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (process.info.get("name") or "").lower()
            cmdline = " ".join(process.info.get("cmdline") or []).lower()
        except Exception:  # noqa: BLE001
            continue
        if "jarvis-qa-profile-" in cmdline:
            leftovers.append(f"{name} (pid {process.info['pid']}) — a JARVIS browser profile")
        if preview_pid and process.info["pid"] == preview_pid:
            leftovers.append(f"{name} (pid {preview_pid}) — the preview process")
    if leftovers:
        _fail("Processes survived that should not have:\n  " + "\n  ".join(leftovers))
    print("OK: no browser profile or preview process remains")


def _assert_protected_file_never_read(client, project_id: str, root: Path) -> None:
    """The .env must be absent from the diff, and its value from everything."""
    (root / "index.html").write_text(
        DEFECTIVE_PAGE.replace("Opening hours", "Opening times"),
        encoding="utf-8", newline="\n")
    (root / ".env").write_text(f"ANTHROPIC_API_KEY={SECRET_MARKER}\nEXTRA=1\n",
                               encoding="utf-8", newline="\n")

    response = client.get(f"/coding/projects/{project_id}/diff")
    if response.status_code != 200:
        _fail(f"Reading the diff failed: {response.status_code}")
    body = response.text
    if SECRET_MARKER in body:
        _fail("The protected file's contents appeared in the diff.")
    if "ANTHROPIC_API_KEY" in body:
        _fail("The protected file's contents appeared in the diff.")
    data = response.json()
    changed = " ".join(c.get("path", "") for c in data.get("changed", []))
    if "index.html" not in changed:
        _fail("The diff did not report the file that actually changed.")
    print("OK: index.html is in the diff, .env's contents are not")


def _assert_history_is_redacted(client) -> None:
    tasks = client.get("/coding/tasks")
    if tasks.status_code != 200:
        _fail(f"Reading task history failed: {tasks.status_code}")
    if SECRET_MARKER in tasks.text:
        _fail("A secret reached the task history.")
    print("OK: task history carries no secret")


def _record_provenance(exe_path: Path, log_dir: Path) -> None:
    """Which packaged files answered, so "it worked" is checkable.

    §7 requires proof that the source checkout's Playwright was not a
    hidden fallback. The installed application is a frozen executable with
    its own interpreter and its own site-packages; this records that, and
    that no Playwright is inside it.
    """
    install_dir = exe_path.parent
    internal = install_dir / "_internal"
    lines = [f"executable: {exe_path.name} ({exe_path.stat().st_size} bytes)"]

    for module in ("app/coding/browser_qa.py", "app/coding/cdp.py",
                   "app/coding/browser_engine.py", "app/coding/browser_origin.py"):
        lines.append(f"packaged module expected inside the archive: {module}")

    playwright = []
    for base in (install_dir, internal):
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if "playwright" in entry.name.lower():
                playwright.append(str(entry.relative_to(install_dir)))
    if playwright:
        _fail("Playwright is inside the installed application: " + ", ".join(playwright))
    lines.append("playwright inside the installed application: none")

    websockets_present = any(
        (base / "websockets").exists() for base in (install_dir, internal) if base.is_dir())
    lines.append(f"websockets packaged: {websockets_present}")
    if not websockets_present:
        print("  note: websockets was not found as a directory; it may be inside the archive")

    report = log_dir / "coding-acceptance-provenance.txt"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:
        print(f"  {line}")
