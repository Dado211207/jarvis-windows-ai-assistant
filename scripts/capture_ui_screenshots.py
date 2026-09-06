"""Screenshot the real interface, and label anything simulated as simulated.

Drives the actual FastAPI app in a real Chromium through Playwright. No
mockups, no hand-drawn approximations: every image is the product
rendering itself.

**Anything simulated says so in its filename.** Three kinds of image here
carry `-simulated-`:

  * runtime states — reaching `speaking` or `awaiting_approval` for real
    would need a live provider, a microphone and a pending action, so the
    same `runtime_state` event the server broadcasts is dispatched through
    the same `handleStreamEvent` path production uses;
  * a conversation — invented words, drawn by the page's own
    `addMessage()`;
  * an approval card — a representative pending action drawn by the real
    `addApprovalCard()`. Nothing is actually pending; Confirm would ask
    the server about an id it never issued.

In every case the *rendering* is the product's own. Only the trigger is
the harness's, which is exactly what the name is there to disclose.

Nothing here is a test and nothing asserts. It writes PNGs.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

HOST = "127.0.0.1"

def _port() -> int:
    """The port the app's own origin allowlist expects.

    `app/api/origin.py` builds the allowed WebSocket origins from
    `settings.jarvis_port`. Serving the harness anywhere else means every
    handshake is correctly rejected as cross-origin, the stream drops,
    and the page falls back to "disconnected" — which then overwrites
    whatever state was being captured.

    That is the product behaving correctly and the harness lying about
    what it photographed: a file named `...-listening-...` showed
    "live updates disconnected". Serve on the configured port instead.
    """
    from app.config import settings
    return int(settings.jarvis_port)

#: Windows-ish desktop window, plus a narrow one to show the layout hold.
VIEWPORTS = {
    "desktop": {"width": 1280, "height": 860},
    "narrow": {"width": 720, "height": 900},
}

#: The states worth showing. Every one is a real `RuntimeState` value
#: except `disconnected`, which is this page's own "the stream dropped"
#: rendering and is included precisely because it is the one that must
#: never look like `listening`.
SIMULATED_STATES = (
    "standby",
    "listening",
    "thinking",
    "awaiting_approval",
    "speaking",
    "error",
    "offline",
)


def _serve(port: int) -> threading.Thread:
    import uvicorn
    from app.api.server import app

    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return thread


def _wait_for_server(port: int, timeout: float = 30.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://{HOST}:{port}/health", timeout=1):
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


#: A short exchange, drawn by the page's own `addMessage` / `addApprovalCard`
#: — the same functions a real reply goes through. The words are invented,
#: which is why every file built from this is named `-simulated-`.
SIMULATED_CONVERSATION = """
() => {
  addMessage("user", "system status");
  addMessage("assistant",
    "CPU 14%, memory 41% of 16 GB, disk C: 232 GB free of 476 GB. " +
    "Nothing is waiting for approval.", "system_status");
  addMessage("user", "make a note that the installer run finished");
  addMessage("assistant",
    "Saved to Documents\\\\JARVIS_Notes as installer-run.md.", "create_note");
}
"""

#: An approval card in the transcript, rendered by the real code path with
#: a representative pending action. Nothing is actually pending: pressing
#: Confirm here would ask the server about an id it has never issued.
SIMULATED_APPROVAL = """
() => {
  addMessage("user", "empty the downloads folder");
  addApprovalCard("simulated-action-id", {
    message: "This action needs your approval before it runs.",
    data: {
      tool_name: "delete_files",
      risk_level: "high",
      description: "Delete 38 files from C:\\\\Users\\\\...\\\\Downloads. " +
        "This cannot be undone from JARVIS.",
    },
  });
}
"""


def _apply_state(page, state: str) -> None:
    """Push a runtime_state event through the page's own handler.

    Deliberately not `element.className = ...`: setting the class
    directly would screenshot a rendering the real event path might never
    produce. This goes through `handleStreamEvent`, so what is captured is
    what a real transition draws.
    """
    # The sequence has to climb. `applyRuntimeObservation` discards an
    # observation that is not newer than the one on screen, so a harness
    # that sent `seq: 0` every time would photograph the first state
    # seven times and name the files after seven different ones.
    page.evaluate(
        """(state) => {
            window.__shotSeq = (window.__shotSeq || 1000000) + 1;
            window.handleStreamEvent({
                seq: window.__shotSeq, type: "runtime_state",
                timestamp: new Date().toISOString(), payload: { to: state }
            });
        }""",
        state,
    )
    page.wait_for_timeout(220)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(REPO_ROOT / "docs" / "screenshots"))
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("JARVIS_LOG_LEVEL", "WARNING")

    port = _port()
    print(f"serving on {HOST}:{port} (the origin allowlist's port)")
    _serve(port)
    if not _wait_for_server(port):
        print("NOT RUN: the server did not become healthy.")
        return 1

    from playwright.sync_api import sync_playwright

    written = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for vp_name, vp in VIEWPORTS.items():
                context = browser.new_context(viewport=vp, reduced_motion="no-preference")
                page = context.new_page()

                for path, label in (
                    ("/ui/chat", "chat"),
                    ("/ui/settings", "settings"),
                    ("/ui/voice", "voice"),
                    ("/ui/actions", "approvals"),
                ):
                    page.goto(f"http://{HOST}:{port}{path}", wait_until="networkidle")
                    page.wait_for_timeout(350)
                    name = out / f"{label}-{vp_name}.png"
                    page.screenshot(path=str(name), full_page=False)
                    written.append(name)

                # The stage compacts once there is a conversation to read,
                # so an empty Chat and a used one are two different
                # compositions and both are worth showing.
                page.goto(f"http://{HOST}:{port}/ui/chat", wait_until="networkidle")
                page.wait_for_timeout(250)
                page.evaluate(SIMULATED_CONVERSATION)
                page.wait_for_timeout(400)
                name = out / f"chat-simulated-conversation-{vp_name}.png"
                page.screenshot(path=str(name), full_page=False)
                written.append(name)

                # An approval in the transcript, where it actually appears
                # — there is no modal anywhere in this product.
                page.goto(f"http://{HOST}:{port}/ui/chat", wait_until="networkidle")
                page.wait_for_timeout(250)
                page.evaluate(SIMULATED_APPROVAL)
                page.wait_for_timeout(400)
                name = out / f"chat-simulated-approval-{vp_name}.png"
                page.screenshot(path=str(name), full_page=False)
                written.append(name)

                # Runtime states, on Chat, at the desktop size only —
                # the point is the core, not the layout.
                if vp_name == "desktop":
                    page.goto(f"http://{HOST}:{port}/ui/chat", wait_until="networkidle")
                    page.wait_for_timeout(300)
                    for state in SIMULATED_STATES:
                        _apply_state(page, state)
                        name = out / f"chat-simulated-state-{state}.png"
                        page.screenshot(path=str(name), full_page=False)
                        written.append(name)

                    # Reduced motion, so the states can be checked for
                    # remaining distinguishable with every animation off.
                    context_rm = browser.new_context(
                        viewport=vp, reduced_motion="reduce"
                    )
                    page_rm = context_rm.new_page()
                    page_rm.goto(f"http://{HOST}:{port}/ui/chat", wait_until="networkidle")
                    page_rm.wait_for_timeout(300)
                    _apply_state(page_rm, "listening")
                    name = out / "chat-simulated-state-listening-reduced-motion.png"
                    page_rm.screenshot(path=str(name), full_page=False)
                    written.append(name)
                    context_rm.close()

                context.close()
        finally:
            browser.close()

    print(f"Wrote {len(written)} screenshots to {out}:")
    for name in written:
        print(f"  {name.name}  ({name.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
