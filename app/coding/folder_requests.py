"""The short-lived record of "the user asked for a folder dialog".

The UI used to ask people to type a full folder path into a text field.
That is wrong in three separate ways: it is hostile to anyone who does
not know where their project lives, it is unusable with a screen reader
that cannot browse the filesystem, and it makes a typo indistinguishable
from a deliberate choice.

Replacing it needs a native dialog, and a native dialog can only be
opened by the process that owns a window — which is not the process
serving this page. So the flow is brokered:

1. The page, holding the session token, asks the server to **mint** a
   request. One at a time, and it expires quickly.
2. The page hands the request id to the native window process, through
   the window's own JavaScript bridge. That call is the user gesture: a
   model cannot make it, and neither can anything a project contains —
   the JARVIS window never renders project content, and the preview runs
   in a separate, headless browser.
3. The window opens the dialog, and **posts the outcome back**
   authenticated with the per-session desktop secret. Nothing else on the
   machine has that secret, so nothing else can claim a folder was
   chosen.
4. The page reads the outcome and shows it. Registering the project
   consumes the request, once.

**Why the result does not simply return through the bridge.** It could,
and the page would show the same thing. But then the server would have no
way to tell a path a person picked from a path a page put in a field, and
§4's "do not claim it was selected through the picker" would be
unenforceable. Routing the answer through an authenticated endpoint makes
`selected_via_picker` a fact the server witnessed.

**No path is ever logged here.** A chosen folder is
`C:\\Users\\<name>\\...`; the account name is in it, and these records are
the kind that end up in a diagnostic. Log lines carry the request id and
the outcome, never the path.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from app.logging_config import get_logger

logger = get_logger("coding.folder_requests")

#: Long enough to find a folder, short enough that a request left open
#: overnight cannot be used the next morning.
REQUEST_TTL_SECONDS = 300.0

#: What a dialog is being opened *for*. A closed set: the purpose decides
#: the dialog's title and where validation sends the answer, and an
#: unknown purpose is a bug, not something to attempt.
PURPOSE_ADD_PROJECT = "add_project"
PURPOSE_NEW_PROJECT_PARENT = "new_project_parent"
VALID_PURPOSES = frozenset({PURPOSE_ADD_PROJECT, PURPOSE_NEW_PROJECT_PARENT})

PROMPTS = {
    PURPOSE_ADD_PROJECT: "Choose the folder that contains your project",
    PURPOSE_NEW_PROJECT_PARENT: "Choose the folder to create the new project in",
}

PENDING = "pending"
SELECTED = "selected"
CANCELLED = "cancelled"
EXPIRED = "expired"
FAILED = "failed"


class FolderRequestError(Exception):
    """Carries a message that is already safe to show.

    `reason` exists so a route never has to call `str()` on an exception:
    `tests/test_security_invariants.py::test_no_endpoint_returns_raw_exception_text`
    AST-walks every handler for exactly that, and the check is worth
    keeping strict. An explicit attribute says "this text was written to
    be read by a person" in a way `str(exc)` cannot.
    """

    #: True when the request is fine but the moment is wrong — a dialog is
    #: already open, or this one has already been answered. The routes map
    #: it to 409 and everything else to 400, so a page can tell "try again
    #: in a second" from "that will never work".
    conflict = False

    def __init__(self, message: str, *, conflict: bool = False) -> None:
        super().__init__(message)
        self.conflict = conflict

    @property
    def reason(self) -> str:
        return str(self)


@dataclass
class FolderRequest:
    id: str
    purpose: str
    created_at: float
    state: str = PENDING
    path: str = ""
    error: str = ""
    consumed: bool = False

    def expired(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) - self.created_at > REQUEST_TTL_SECONDS

    def as_dict(self, *, include_path: bool) -> dict:
        """`include_path` is False for anything but the page that asked.

        There is exactly one audience for the chosen folder — the person
        who chose it — and one surface it may appear on. Everything else
        gets the state.
        """
        return {
            "request_id": self.id,
            "purpose": self.purpose,
            "prompt": PROMPTS.get(self.purpose, "Choose a folder"),
            "state": self.state,
            "path": self.path if include_path else "",
            "error": self.error,
            "consumed": self.consumed,
        }


class FolderRequestBroker:
    """Every outstanding folder request. At most one is ever pending."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: Dict[str, FolderRequest] = {}

    # -- minting ----------------------------------------------------------

    def create(self, purpose: str) -> FolderRequest:
        if purpose not in VALID_PURPOSES:
            raise FolderRequestError("That is not a folder JARVIS knows how to ask for.")
        with self._lock:
            self._sweep_locked()
            for existing in self._requests.values():
                if existing.state == PENDING:
                    # Two native dialogs at once is a window that cannot be
                    # dismissed in the order the user expects, and on
                    # Windows a second modal owned by the same window is a
                    # good way to produce one nobody can close.
                    raise FolderRequestError(
                        "A folder dialog is already open. Finish or cancel that one first.",
                        conflict=True,
                    )
            request = FolderRequest(id=secrets.token_urlsafe(16), purpose=purpose,
                                    created_at=time.time())
            self._requests[request.id] = request
        logger.info("Folder dialog requested (%s, id=%s).", purpose, request.id[:8])
        return request

    # -- resolving --------------------------------------------------------

    def resolve(self, request_id: str, *, path: str = "", cancelled: bool = False,
                error: str = "") -> FolderRequest:
        """Record the outcome. Only the native window may call this, and
        only once."""
        with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                raise FolderRequestError("That folder request is not open.")
            if request.state != PENDING:
                # Deliberately not idempotent-with-overwrite: a second
                # result for a settled request is either a bug or an
                # attempt to replace a chosen folder with another one.
                raise FolderRequestError("That folder request has already been answered.",
                                         conflict=True)
            if request.expired():
                request.state = EXPIRED
                request.error = "The folder dialog timed out."
                raise FolderRequestError(request.error)

            if cancelled:
                request.state = CANCELLED
            elif error:
                request.state = FAILED
                request.error = error[:200]
            elif path:
                validated, problem = _validate(path, request.purpose)
                if problem:
                    request.state = FAILED
                    request.error = problem
                else:
                    request.state = SELECTED
                    request.path = validated
            else:
                request.state = CANCELLED
        logger.info("Folder dialog %s: %s.", request_id[:8], request.state)
        return request

    # -- reading ----------------------------------------------------------

    def get(self, request_id: str) -> Optional[FolderRequest]:
        with self._lock:
            request = self._requests.get(request_id)
            if request is not None and request.state == PENDING and request.expired():
                request.state = EXPIRED
                request.error = "The folder dialog timed out."
            return request

    def cancel(self, request_id: str) -> Optional[FolderRequest]:
        """The page abandoning a request — navigating away, or pressing
        Cancel in JARVIS's own UI before the dialog answered."""
        with self._lock:
            request = self._requests.get(request_id)
            if request is not None and request.state == PENDING:
                request.state = CANCELLED
        return request

    def consume(self, request_id: str) -> str:
        """The chosen folder, once.

        Registering a project spends the request. A second registration
        cannot reuse it, so a stale id in an open tab is not a second
        chance to add a folder the user chose ten minutes ago for
        something else.
        """
        with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                raise FolderRequestError("That folder selection is no longer available.")
            if request.state != SELECTED or not request.path:
                raise FolderRequestError("No folder was selected.")
            if request.consumed:
                raise FolderRequestError("That folder selection has already been used.")
            if request.expired():
                request.state = EXPIRED
                raise FolderRequestError("That folder selection has expired. Choose again.")
            request.consumed = True
            return request.path

    def pending_count(self) -> int:
        with self._lock:
            self._sweep_locked()
            return sum(1 for r in self._requests.values() if r.state == PENDING)

    def clear(self) -> None:
        """Every request, forgotten. Called on Restart and on Quit so a
        selection never outlives the session that asked for it."""
        with self._lock:
            self._requests.clear()

    def _sweep_locked(self) -> None:
        now = time.time()
        stale = [
            key for key, request in self._requests.items()
            if now - request.created_at > REQUEST_TTL_SECONDS * 2
        ]
        for key in stale:
            self._requests.pop(key, None)
        for request in self._requests.values():
            if request.state == PENDING and request.expired(now):
                request.state = EXPIRED
                request.error = "The folder dialog timed out."


def _validate(raw: str, purpose: str) -> tuple:
    """Canonicalise and screen the folder the dialog returned.

    A native dialog cannot return a `javascript:` URL, and Windows will
    not let somebody pick a device path through it. The check runs anyway:
    the value arrives over a socket, and a boundary that trusts its input
    because of where it is *supposed* to have come from is not a boundary.
    `workspace.canonical_root()` is the same routine every other path in
    this feature goes through — there is deliberately no second one.
    """
    from app.coding import workspace

    try:
        root = workspace.canonical_root(raw)
    except workspace.WorkspaceViolation as exc:
        return "", str(exc)
    except Exception:  # noqa: BLE001
        return "", "That folder could not be used."

    if not root.is_dir():
        return "", "That is not a folder."
    return str(root), ""


#: One broker per server process. Requests are in memory and deliberately
#: do not survive a restart — a dialog nobody is looking at any more must
#: not be answerable.
broker = FolderRequestBroker()
