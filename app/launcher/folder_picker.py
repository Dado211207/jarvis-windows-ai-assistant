"""The native folder dialog, opened by the process that owns the window.

Only one process on this machine can put a modal dialog on JARVIS's
window, and it is not the one serving the page. This module runs in the
**window child**, is reached through pywebview's JavaScript bridge, and
posts its answer back to the server authenticated with the per-session
desktop secret.

**Why the bridge is the user gesture.** `window.pywebview.api` exists
only inside the native window, and the native window only ever renders
JARVIS's own interface. A model cannot reach it: models produce text that
`app/coding/schema.py` validates against a closed union with no dialog
action in it. A project cannot reach it either: project content is
rendered by a separate, headless browser in `app/coding/browser_qa.py`,
never here.

**One dialog at a time, enforced twice.** The server refuses to mint a
second pending request, and this module holds a lock so a bridge call
that arrived anyway cannot open a second modal. Two modals owned by the
same window is a reliable way to produce one nobody can dismiss.

**The dialog does not outlive the window.** pywebview's dialog is owned
by the window handle, so closing the window, Restart and Quit all take it
with them. `abandon()` additionally releases the lock, so a window that
went away while a dialog was open does not leave this module believing
one is still up.

**No path is logged.** A chosen folder contains the account name.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from app.logging_config import get_logger

logger = get_logger("launcher.folder_picker")

#: How long the bridge call may block. The dialog itself is modal and has
#: no timeout of its own — a person may take as long as they like — but
#: the *request* on the server expires, and this bound stops a wedged
#: dialog from holding the bridge thread for the life of the process.
POST_TIMEOUT_SECONDS = 10.0

_lock = threading.Lock()
_open = False


def is_open() -> bool:
    with _lock:
        return _open


def abandon() -> None:
    """Forget that a dialog is open. Called when the window goes away."""
    global _open
    with _lock:
        _open = False


def _server_url() -> str:
    from app.launcher import ipc

    return os.environ.get(ipc.IPC_URL_ENV, "").rstrip("/")


def _desktop_secret() -> str:
    from app.launcher.server_process import SESSION_SECRET_ENV

    return os.environ.get(SESSION_SECRET_ENV, "")


def choose_folder(request_id: str, prompt: str = "", *,
                  webview_module=None, poster=None) -> dict:
    """Open the dialog for one minted request and report the outcome.

    Returns a small dict for the page — `{"state": ...}` — and separately
    posts the authoritative answer to the server. The page's copy is a
    convenience; the server's is the record.

    `webview_module` and `poster` are the injection seams the tests use,
    the same pattern `webview_window.create_and_run` already follows.
    Production passes neither.
    """
    global _open

    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        return {"state": "failed", "error": "That request is not one JARVIS made."}

    with _lock:
        if _open:
            return {"state": "failed",
                    "error": "A folder dialog is already open."}
        _open = True

    outcome: dict
    try:
        selected = _open_dialog(prompt, webview_module)
        if selected is None:
            outcome = {"cancelled": True}
        else:
            outcome = {"path": selected}
    except Exception as exc:  # noqa: BLE001 — a dialog failure must not kill the window
        logger.warning("The folder dialog could not be opened.", exc_info=True)
        outcome = {"error": f"The folder dialog could not be opened ({type(exc).__name__})."}
    finally:
        with _lock:
            _open = False

    state = _report(request_id, outcome, poster)
    return {"state": state}


def _open_dialog(prompt: str, webview_module=None) -> Optional[str]:
    """The dialog itself. Returns the folder, or None for Cancel.

    Deliberately no `directory=` argument. pywebview would happily start
    the dialog wherever we said, and the obvious thing to say is "the last
    folder they picked" — which would mean JARVIS remembering, and
    displaying, a list of the user's directories. The dialog's own
    Windows-managed history is the user's, and stays that way.
    """
    webview = webview_module
    if webview is None:
        import webview as _real_webview
        webview = _real_webview

    window = None
    from app.launcher import webview_window

    window = webview_window.current_window()
    if window is None:
        raise RuntimeError("There is no JARVIS window to own the dialog.")

    result = window.create_file_dialog(
        webview.FOLDER_DIALOG,
        allow_multiple=False,
        # The dialog is owned by the JARVIS window, so it is modal to
        # JARVIS and cannot be lost behind it.
    )
    if not result:
        return None
    first = result[0] if isinstance(result, (list, tuple)) else result
    return str(first) if first else None


def _report(request_id: str, outcome: dict, poster=None) -> str:
    """Post the outcome to the server, authenticated.

    A failure to post is itself reported to the page, because the
    alternative — the page waiting on a request the server will expire in
    five minutes — looks exactly like a hung dialog.
    """
    url = f"{_server_url()}/coding/folder-dialog/{request_id}/result"
    secret = _desktop_secret()
    if not secret:
        # Nothing can be proved without it, and claiming a folder was
        # picked without proof is the thing this design exists to stop.
        logger.error("No desktop secret: the folder dialog result cannot be reported.")
        return "failed"

    from app.launcher.desktop_ready import READY_HEADER

    send = poster or _post
    try:
        send(url, outcome, {READY_HEADER: secret})
    except Exception:  # noqa: BLE001
        logger.warning("Could not report the folder dialog result.", exc_info=True)
        return "failed"

    if outcome.get("path"):
        return "selected"
    if outcome.get("cancelled"):
        return "cancelled"
    return "failed"


def _post(url: str, payload: dict, headers: dict) -> None:
    import json
    import urllib.request

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        request.add_header(key, value)
    # An explicit empty proxy handler: a machine-wide HTTPS_PROXY must not
    # be consulted to reach JARVIS's own server on loopback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=POST_TIMEOUT_SECONDS) as response:  # noqa: S310
        response.read(4096)


class WindowApi:
    """What the page can call. Exactly one method, and it takes an id.

    Kept deliberately tiny: this object is the entire attack surface the
    JavaScript bridge exposes, and every method added to it is another
    thing a page can do to the native process.
    """

    def __init__(self, webview_module=None, poster=None) -> None:
        self._webview_module = webview_module
        self._poster = poster

    def choose_folder(self, request_id: str = "", prompt: str = "") -> dict:
        return choose_folder(request_id, prompt,
                             webview_module=self._webview_module,
                             poster=self._poster)
