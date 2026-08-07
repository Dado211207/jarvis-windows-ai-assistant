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
    "app.core.brain",
    "app.core.tool_registry",
    "app.core.policy",
    "app.core.runtime_state",
    "app.desktop.apps",
    "app.desktop.windows",
    "app.desktop.folders",
    "app.desktop.clipboard",
    "app.desktop.notes",
]


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

    healthy = False
    try:
        for _ in range(50):
            try:
                r = httpx.get("http://127.0.0.1:5599/health", timeout=1)
                if r.status_code == 200:
                    healthy = True
                    break
            except Exception:
                pass
            time.sleep(0.2)
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    if not healthy:
        raise RuntimeError("FastAPI did not report healthy within the timeout")
    print("FastAPI startup smoke OK (127.0.0.1 only, real bind, real shutdown)")


if __name__ == "__main__":
    try:
        check_imports()
        check_fastapi_startup()
    except Exception as exc:
        print(f"WINDOWS SMOKE FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    print("Windows smoke checks passed.")
