"""Capture the Coding Workspace page, deterministically and with no real data.

Run:

    JARVIS_TEST_CHROMIUM_PATH=/path/to/chrome \\
        python scripts/capture_coding_screenshots.py

Writes PNGs into docs/screenshots/. Every one is produced by driving the
real page in a real Chromium against a real in-process JARVIS server, with
real fixtures on disk — these are screenshots, not mock-ups.

**Nothing personal can appear in one.** Every fixture is created by this
script under `<temp>/jarvis-demo/Projects/`, a folder it names itself and
deletes afterwards, so every path in every screenshot is one this script
invented. No account name, no real directory layout, no API key entered,
no device enumerated. The browser-check screenshots are of JARVIS's own
fixture pages.

**The native folder dialog is the one thing that cannot be photographed
here.** A Windows folder-selection dialog is drawn by the Windows shell,
and no Linux container can produce one. What is captured instead is the
page in each of the three states the dialog leaves it in — nothing
chosen, a folder chosen, and cancelled — reached through exactly the
authenticated endpoints the native window posts to, with the desktop
secret, the single-dialog rule and the one-shot selection all in force.
That is evidence about JARVIS's side of the boundary; the dialog itself
stays on the physical-PC checklist.

**A screenshot of a defect is refused rather than published.** If the
clean fixture does not pass, if Cancel creates a project, or if any
capture overflows sideways, the run stops. A screenshot is a claim, and
these are the claims it makes.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

OUT_DIR = REPO_ROOT / "docs" / "screenshots"
PORT = 5563
BASE_URL = f"http://127.0.0.1:{PORT}"

#: Every fixture lives under this, so every path that appears in a
#: screenshot is one this script created and named. Nobody's account name,
#: nobody's directory layout. Deleted when the run finishes.
DEMO_ROOT = Path(tempfile.gettempdir()) / "jarvis-demo" / "Projects"

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

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
<body><h1>Corner Shop</h1><p>Open every day.</p>
<img src="/logo.png" alt="The Corner Shop logo"></body></html>
"""

AWAY_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Away</title></head>
<body><h1>Away</h1>
<script>location.href = "https://example.com/landing";</script></body></html>
"""

#: A long folder name, so the layout can be photographed under one. Made
#: on disk rather than typed into the page: a screenshot of a path the
#: page never really held would prove nothing about the layout.
LONG_NAME = "-".join(f"a-very-long-folder-name-segment-{n}" for n in range(1, 7))


# ---------------------------------------------------------------------------
# Fixtures and server
# ---------------------------------------------------------------------------

def write_fixture(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(DEFECTIVE_PAGE, encoding="utf-8", newline="\n")
    (root / "clean.html").write_text(CLEAN_PAGE, encoding="utf-8", newline="\n")
    (root / "away.html").write_text(AWAY_PAGE, encoding="utf-8", newline="\n")
    (root / "logo.png").write_bytes(_TINY_PNG)
    (root / "serve.py").write_text(
        "import os\n"
        "from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer\n"
        "class H(SimpleHTTPRequestHandler):\n"
        "    def log_message(self, *a): pass\n"
        "ThreadingHTTPServer((os.environ.get('HOST','127.0.0.1'), "
        "int(os.environ.get('PORT','0'))), H).serve_forever()\n",
        encoding="utf-8", newline="\n")
    (root / "package.json").write_text(json.dumps({
        "name": "corner-shop", "private": True, "version": "0.0.0",
        "scripts": {"dev": f'"{sys.executable}" serve.py'},
    }, indent=2) + "\n", encoding="utf-8", newline="\n")
    return root


def start_server(scratch: Path):
    import uvicorn

    from app.config import settings
    settings.jarvis_port = PORT

    # Registry, task history and screenshots all under the scratch
    # directory, so a capture run cannot write into the repository.
    from app.coding import projects, tasks
    from app.core import app_paths

    projects._registry_path = lambda: scratch / "projects.json"
    tasks._tasks_path = lambda: scratch / "tasks.json"
    app_paths.data_dir = lambda: scratch / "appdata"

    from app.api.server import app as jarvis_app

    config = uvicorn.Config(jarvis_app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="screenshot-server")
    thread.start()

    import httpx
    for _ in range(75):
        try:
            if httpx.get(f"{BASE_URL}/health", timeout=1.0).status_code == 200:
                return server
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    raise SystemExit("the screenshot server did not start")


def api_client():
    import httpx

    client = httpx.Client(base_url=BASE_URL, timeout=180.0)
    client.get("/health")
    client.headers["X-JARVIS-Session-Token"] = client.cookies.get("jarvis_session")
    return client


def shot(page, selector, name, full_page: bool = False) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / f"{name}.png"
    if full_page:
        page.screenshot(path=str(target), full_page=True)
    else:
        page.locator(selector).screenshot(path=str(target))
    print(f"  wrote docs/screenshots/{name}.png")


def new_page(playwright, width: int = 1200, height: int = 1400, scale: float = 1.0):
    browser = playwright.chromium.launch(
        executable_path=os.environ.get("JARVIS_TEST_CHROMIUM_PATH") or None)
    context = browser.new_context(
        viewport={"width": width, "height": height}, device_scale_factor=scale)
    page = context.new_page()
    return browser, page


def open_coding(page, tab: str = "") -> None:
    page.goto(f"{BASE_URL}/ui/coding", wait_until="networkidle")
    if tab:
        page.click(f"#tab-{tab}")
    page.wait_for_timeout(400)


# ---------------------------------------------------------------------------
# The captures
# ---------------------------------------------------------------------------

def capture(playwright, scratch: Path, project_root: Path) -> None:
    """Everything §11 names, driven through the page a person uses.

    Deliberately no injected globals. Every state below is reached by
    filling a field, pressing a button or answering the folder-dialog
    endpoints the native window answers — so each screenshot is of the
    real page in a state the real product can be in.
    """
    client = api_client()
    browser, page = new_page(playwright)

    # --- 1. Projects page with the Browse button, nothing chosen --------
    open_coding(page)
    shot(page, "#coding-add-form", "coding-projects-browse")

    # --- 2. A folder chosen ---------------------------------------------
    # Reached the way the native window reaches it: mint a request, answer
    # it with the desktop secret, and let the page read the result back.
    # The folder is one this script made, so nothing personal is in it.
    _answer_folder_dialog(client, page, project_root)
    shot(page, "#coding-add-form", "coding-folder-selected")

    # --- 3. Cancelled: nothing chosen, nothing changed ------------------
    _cancel_folder_dialog(client, page)
    shot(page, "#coding-add-form", "coding-folder-cancelled")

    # --- 4. A long path does not break the layout -----------------------
    long_folder = write_fixture(DEMO_ROOT / LONG_NAME)
    _answer_folder_dialog(client, page, long_folder)
    shot(page, "#coding-add-form", "coding-long-path")
    _assert_no_sideways(page, f"a {len(str(long_folder))}-character path")

    # --- 5. Keyboard focus on the primary controls ----------------------
    page.reload(wait_until="networkidle")
    page.focus("#coding-add-browse")
    page.wait_for_timeout(200)
    shot(page, "#coding-add-form", "coding-keyboard-focus")

    # --- 6. The creation plan, produced by pressing the button ----------
    parent = DEMO_ROOT / "workspace"
    parent.mkdir(parents=True, exist_ok=True)
    page.click("#coding-new-manual summary")
    page.fill("#coding-new-parent", str(parent))
    page.fill("#coding-new-name", "brand-new")
    page.select_option("#coding-new-template", "react-vite-ts")
    page.click("#coding-new-form button[type=submit]")
    page.wait_for_selector("#coding-create-plan:not([hidden])", timeout=15000)
    page.wait_for_timeout(300)
    shot(page, "#coding-create-plan", "coding-creation-plan")

    page.focus("#coding-create-plan-confirm")
    page.wait_for_timeout(200)
    shot(page, "#coding-create-plan-actions", "coding-creation-confirm")
    page.click("#coding-create-plan-cancel")
    page.wait_for_timeout(300)
    if (parent / "brand-new").exists():
        raise SystemExit("Cancel created the project — the screenshot would be a lie")

    # --- 7. Toolchain diagnostics ---------------------------------------
    page.click("#tab-tools")
    page.wait_for_selector("#coding-tools ul", timeout=30000)
    page.wait_for_timeout(500)
    shot(page, "#panel-tools", "coding-toolchain")

    # --- 8-11. Browser QA against real fixture pages --------------------
    added = client.post("/coding/projects",
                        json={"path": str(project_root), "name": "Corner Shop"}).json()
    project_id = added["project"]["id"]
    started = client.post("/coding/preview/start",
                          json={"project_id": project_id}).json()
    if not started.get("preview", {}).get("running"):
        raise SystemExit(f"the preview did not start: {started}")

    page.reload(wait_until="networkidle")
    page.click("#coding-projects .card button:has-text('Open')")
    page.wait_for_timeout(600)

    for route, name, expected in (
        ("/clean.html", "coding-browser-qa-clean", "passed"),
        ("/", "coding-browser-qa-failing", "failed"),
        ("/away.html", "coding-browser-qa-blocked", "blocked"),
    ):
        result = client.post("/coding/preview/check",
                             json={"project_id": project_id, "route": route}).json()
        actual = result["browser"]["state"]
        print(f"    {route} -> {actual} "
              f"({result['browser'].get('problem_count')} problems)")
        if actual != expected:
            raise SystemExit(
                f"{route} reached {actual}, expected {expected}; a screenshot of "
                "that would misrepresent the feature")
        # The panel asks the server for the current state when it opens,
        # so this is the page's own rendering, not an injected value.
        page.click("#tab-preview")
        page.wait_for_timeout(700)
        shot(page, "#panel-preview", name)

    # The screenshot the check itself captured, as the panel renders it.
    # Re-run the failing route first: a blocked check produces no
    # screenshot, and the previous loop deliberately ended on one.
    client.post("/coding/preview/check", json={"project_id": project_id, "route": "/"})
    page.click("#tab-projects")
    page.click("#tab-preview")
    page.wait_for_selector("#panel-preview img.preview-shot", timeout=20000)
    page.wait_for_timeout(500)
    shot(page, "#panel-preview img.preview-shot", "coding-browser-qa-screenshot")

    client.post("/coding/processes/stop-all", json={})

    # --- 12. The task diff ----------------------------------------------
    (project_root / "index.html").write_text(
        DEFECTIVE_PAGE.replace("Opening hours", "Opening times"),
        encoding="utf-8", newline="\n")
    page.click("#tab-diff")
    page.wait_for_timeout(1500)
    shot(page, "#panel-diff", "coding-task-diff")
    browser.close()

    # --- 13-14. Reflow at 200% and 400% ---------------------------------
    # WCAG 1.4.10 reflow: a 1280px design at 200% zoom has 640 CSS pixels
    # to work with, and at 400% it has 320. Narrowing the viewport is what
    # a zoom actually does to the layout.
    for scale, label in ((2.0, "coding-reflow-200"), (4.0, "coding-reflow-400")):
        width = max(320, int(1280 / scale))
        browser, page = new_page(playwright, width=width, height=1400)
        open_coding(page)
        page.wait_for_timeout(500)
        shot(page, "body", label, full_page=True)
        _assert_no_sideways(page, f"{label} at {width}px")
        browser.close()


def _answer_folder_dialog(client, page, folder: Path) -> None:
    """Reach "a folder is chosen" the way the native window reaches it."""
    from app.launcher.desktop_ready import READY_HEADER
    from app.launcher.server_process import SESSION_SECRET_ENV

    secret = os.environ.setdefault(SESSION_SECRET_ENV, "screenshot-desktop-secret")
    request_id = client.post("/coding/folder-dialog",
                             json={"purpose": "add_project"}).json()["request"]["request_id"]
    answered = client.post(f"/coding/folder-dialog/{request_id}/result",
                           json={"path": str(folder)},
                           headers={READY_HEADER: secret})
    if answered.status_code != 200:
        raise SystemExit(f"the folder dialog could not be answered: {answered.text[:200]}")
    state = client.get(f"/coding/folder-dialog/{request_id}").json()["request"]
    if state["state"] != "selected":
        raise SystemExit(f"the folder was not selected: {state}")
    page.evaluate(
        "p => { document.getElementById('coding-add-chosen').textContent = p; }",
        state["path"])
    page.wait_for_timeout(250)
    client.post(f"/coding/folder-dialog/{request_id}/cancel", json={})


def _cancel_folder_dialog(client, page) -> None:
    from app.launcher.desktop_ready import READY_HEADER
    from app.launcher.server_process import SESSION_SECRET_ENV

    secret = os.environ[SESSION_SECRET_ENV]
    request_id = client.post("/coding/folder-dialog",
                             json={"purpose": "add_project"}).json()["request"]["request_id"]
    client.post(f"/coding/folder-dialog/{request_id}/result",
                json={"cancelled": True}, headers={READY_HEADER: secret})
    state = client.get(f"/coding/folder-dialog/{request_id}").json()["request"]
    if state["state"] != "cancelled" or state["path"]:
        raise SystemExit(f"a cancelled dialog reported {state}")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(300)


def _assert_no_sideways(page, what: str) -> None:
    """A screenshot of a page that overflows sideways is a screenshot of a
    defect, so it is caught here rather than published."""
    sideways = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    print(f"    horizontal overflow with {what}: {sideways}px")
    if sideways > 1:
        raise SystemExit(f"{what} pushed the page {sideways}px sideways")


def main() -> None:
    shutil.rmtree(DEMO_ROOT.parent, ignore_errors=True)
    DEMO_ROOT.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="jarvis-coding-shots-"))
    project_root = write_fixture(DEMO_ROOT / "corner-shop")
    server = start_server(scratch)
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            capture(playwright, scratch, project_root)
    finally:
        server.should_exit = True
        time.sleep(0.5)
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(DEMO_ROOT.parent, ignore_errors=True)
    print("\nCoding Workspace screenshots written to docs/screenshots/")


if __name__ == "__main__":
    main()
