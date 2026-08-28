"""Windows CI smoke check: real imports, real FastAPI startup, no mocks.

Run via `python scripts/ci_windows_smoke.py` from the repo root (see the
windows-smoke job in .github/workflows/ci.yml). This is separate from
pytest so a failure here — the app can't even import or boot on Windows —
gives an unambiguous signal instead of being buried in pytest collection
output. The rest of the Windows job runs the real pytest suite (which
already runs entirely through mocks for anything platform-sensitive —
see tests/test_safe_actions.py, tests/test_clipboard.py); this script
covers the two things that happen before pytest is even in the picture.

Binds 127.0.0.1 only (matches app/api/server.py), needs no
ANTHROPIC_API_KEY (deliberately unset in this CI job — proves the
text-only local-fallback path works without credentials), and shuts its
own server down before exiting — nothing is left running afterward.
"""
import importlib
import sys
import threading
import time
from pathlib import Path

# Running this file directly puts only scripts/ on sys.path, not the repo
# root, so `import app...` would otherwise fail regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CORE_IMPORTS = [
    "app.api.server",
    "app.api.chat",
    "app.core.brain",
    "app.core.tool_registry",
    "app.core.policy",
    "app.core.runtime_state",
    "app.core.ai",
    "app.core.ai.anthropic_provider",
    "app.core.ai.ollama_provider",
    "app.core.conversation",
    "app.core.generation",
    "app.core.preferences",
    "app.desktop.apps",
    "app.desktop.folders",
    "app.desktop.clipboard",
    "app.desktop.notes",
    "app.desktop.session",
]

# Every page a user can reach from the sidebar. Walked over a real HTTP
# connection to a real uvicorn bind, not a TestClient: a template that
# renders fine in-process can still fail here (a missing static mount, a
# response the ASGI layer cannot encode), and this job exists to catch
# what only shows up when the server is really running.
UI_PAGES = [
    "/ui/", "/ui/chat", "/ui/actions", "/ui/voice", "/ui/logs", "/ui/memory",
    "/ui/help", "/ui/settings", "/ui/diagnostics", "/ui/setup",
]

# Read-only endpoints. Mutating ones are deliberately absent: this smoke
# check must not change anything on the machine it runs on.
READ_ENDPOINTS = [
    "/", "/health", "/system", "/tools", "/logs", "/memory", "/conversation",
    "/voice/status", "/voice/stt-status", "/privacy/status", "/privacy/data",
    "/providers", "/settings/api-key-status", "/settings/startup",
    "/onboarding/readiness", "/onboarding/complete", "/diagnostics",
    "/about", "/about/notices", "/actions/pending", "/actions/history",
]

STATIC_ASSETS = ["/ui/static/style.css", "/ui/static/app.js"]


def check_imports() -> None:
    for name in CORE_IMPORTS:
        importlib.import_module(name)
    print(f"imports OK ({len(CORE_IMPORTS)} core modules)")


def check_fastapi_startup() -> None:
    import httpx
    import uvicorn

    from app.api.server import app

    config = uvicorn.Config(app, host="127.0.0.1", port=5599, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = "http://127.0.0.1:5599"
    healthy = False
    failures = []
    try:
        for _ in range(50):
            try:
                r = httpx.get(f"{base}/health", timeout=1)
                if r.status_code == 200:
                    healthy = True
                    break
            except Exception:
                pass
            time.sleep(0.2)

        if healthy:
            failures = _walk_everything(httpx, base)
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    if not healthy:
        raise RuntimeError("FastAPI did not report healthy within the timeout")
    if failures:
        raise RuntimeError("real-server checks failed:\n  " + "\n  ".join(failures))
    print(
        f"FastAPI smoke OK — {len(UI_PAGES)} pages, {len(READ_ENDPOINTS)} endpoints, "
        f"{len(STATIC_ASSETS)} assets over a real bind, then a real shutdown"
    )


def _walk_everything(httpx, base):
    """Every page, endpoint and asset over the live server. Collects all
    failures rather than stopping at the first, so one run reports the
    whole picture instead of one symptom at a time.

    Runs inside a session, exactly as the dashboard does. The reads below
    include the user's own logs, memory, conversation and diagnostics, and
    those require the double-submit session token now; a client that walks
    them anonymously gets nine 403s and reports them as failures — which
    is what happened the first time this job met the security pass. The
    token is not a workaround here: a real consumer of these endpoints has
    to hold one, so a smoke check that did not would be testing a path no
    supported client uses.
    """
    failures = []

    client = httpx.Client(timeout=10)
    try:
        # GET /health is the bootstrap that issues the cookie; echoing it
        # back as a header is the double-submit the server requires.
        client.get(f"{base}/health")
        token = client.cookies.get("jarvis_session")
        if token:
            client.headers["X-JARVIS-Session-Token"] = token
        else:
            failures.append("/health: no session cookie was issued")
        return _walk_with(client, base, failures)
    finally:
        client.close()


def _walk_with(client, base, failures):
    for path in UI_PAGES:
        try:
            response = client.get(f"{base}{path}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path}: request failed ({type(exc).__name__})")
            continue
        if response.status_code != 200:
            failures.append(f"{path}: HTTP {response.status_code}")
        elif "<html" not in response.text.lower():
            failures.append(f"{path}: did not return an HTML document")

    for path in READ_ENDPOINTS:
        try:
            response = client.get(f"{base}{path}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path}: request failed ({type(exc).__name__})")
            continue
        if response.status_code != 200:
            failures.append(f"{path}: HTTP {response.status_code}")

    for path in STATIC_ASSETS:
        try:
            response = client.get(f"{base}{path}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path}: request failed ({type(exc).__name__})")
            continue
        if response.status_code != 200 or not response.text.strip():
            failures.append(f"{path}: not served (HTTP {response.status_code})")

    failures.extend(_check_no_credentials_are_served(client, base))
    return failures


def _check_no_credentials_are_served(client, base):
    """Plant a credential-shaped value where the app would read one, then
    walk every page and read-only endpoint asserting it never comes back.

    Deliberately end-to-end: the unit suite proves each surface
    individually, and this proves the assembled server — which is the
    thing a user actually points a browser at.
    """
    from app.config import settings

    planted = "sk-ant-smoke-check-must-never-be-served"
    original = getattr(type(settings), "effective_api_key", None)
    failures = []
    try:
        type(settings).effective_api_key = property(lambda self: planted)
        for path in UI_PAGES + READ_ENDPOINTS:
            try:
                body = client.get(f"{base}{path}").text
            except Exception:  # noqa: BLE001 — already reported above
                continue
            if planted in body or "sk-ant-" in body:
                failures.append(f"{path}: served a credential-shaped value")
    finally:
        if original is not None:
            type(settings).effective_api_key = original
        else:
            # Nothing to restore means the attribute was not there before;
            # leaving the planted one behind would be worse than removing it.
            delattr(type(settings), "effective_api_key")
    return failures


if __name__ == "__main__":
    try:
        check_imports()
        check_fastapi_startup()
    except Exception as exc:
        print(f"WINDOWS SMOKE FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    print("Windows smoke checks passed.")
