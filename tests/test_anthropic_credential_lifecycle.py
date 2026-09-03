"""The Anthropic credential as one thing: the key, the workspace it acts
in, and what the last check actually observed.

**Why this file exists.** Independent review of the identity-linked-key
work found four defects that a test asserting "the happy path stores both
values" would never see:

  1. The key goes to Windows Credential Manager and the workspace ID goes
     to a JSON file. Two stores, two writes, and the second write's result
     was discarded — so replacing a working key could leave the new key in
     Credential Manager beside the *previous* key's workspace ID and the
     *previous* key's verdict, and answer "API key saved and verified."
  2. Removal had the same shape: clear the credential, ignore whether the
     metadata went with it, report complete success.
  3. The failure that started all of this happened while saving a key, and
     `verify_anthropic_key()` wrote no Logs-page row at all — so the exact
     path that exposed the defect could still fail silently.
  4. `to_safe_error()` logged the provider's exception object. Anthropic
     documents the inaccessible-workspace response as ``Workspace `<id>`
     not found.``, so the file log could contain the very value the rest of
     this feature is careful never to write down.

  5. And a fifth: an offline, timed-out or rate-limited check was stored as
     `verification_failed`, whose UI sentence reads "The saved API key was
     rejected by Anthropic". Nothing rejected anything; the check never
     completed.

Every test here is written against the failure, not the fix — each one
fails against the implementation that shipped in the first draft of this
branch.

No test in this file touches a real credential store, a real preferences
file outside tmp_path, or the network.
"""

import re
from unittest.mock import patch

import pytest

from app.core.errors import ErrorCategory

FAKE_KEY = "sk-ant-api03-not-a-real-key-only-a-test-fixture"
OLD_KEY = "sk-ant-api03-the-previously-working-key"
FAKE_WORKSPACE = "wrkspc_01TESTonlyNeverReal"
OLD_WORKSPACE = "wrkspc_01TESTtheOneBefore"


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield prime_session(client)


def _applied():
    """A credential-store write that the backend confirmed."""
    from app.core import credentials
    return credentials.MutationResult(credentials.MUTATION_APPLIED)


def _refused():
    """A write the backend refused outright: provably nothing changed, which
    is what every "nothing was changed" assertion in this file depends on.

    A write that merely *timed out* is a third outcome — it may still land —
    and the messages that separate the two are proven in
    tests/test_credential_replacement_safety.py.
    """
    from app.core import credentials
    return credentials.MutationResult(credentials.MUTATION_UNCHANGED, "backend_refused")


class _Store:
    """A stand-in for Windows Credential Manager that can be made to fail
    on a chosen call, so a failure *ordering* can be tested rather than
    only a failure."""

    def __init__(self, initial="", fail_on=()):
        self.value = initial
        self.reachable = True
        self.fail_on = set(fail_on)
        self.writes = []

    def snapshot(self):
        # Mirrors credentials._read: an unreachable store yields "", which
        # is exactly why the pair carries `reached` separately.
        return (True, self.value) if self.reachable else (False, "")

    def set(self, value):
        return self.set_detailed(value).ok

    def clear(self):
        return self.clear_detailed().ok

    def set_detailed(self, value):
        self.writes.append(value)
        if len(self.writes) in self.fail_on:
            return _refused()
        self.value = value
        return _applied()

    def clear_detailed(self):
        self.writes.append(None)
        if len(self.writes) in self.fail_on:
            return _refused()
        self.value = ""
        return _applied()


class _Preferences:
    """Ditto for the preferences file."""

    def __init__(self, initial=None, fail_on=()):
        self.data = dict(initial or {})
        self.fail_on = set(fail_on)
        self.writes = []

    def store_many(self, values):
        self.writes.append(dict(values))
        if len(self.writes) in self.fail_on:
            return False
        for key, value in values.items():
            if value is None or not str(value).strip():
                self.data.pop(key, None)
            else:
                self.data[key] = str(value).strip()
        return True

    def get(self, key):
        value = self.data.get(key)
        return value if isinstance(value, str) and value.strip() else None


def _wired(store, preferences):
    """Patch both stores at their definition sites, which is where the
    credential-pair module imports them from."""
    return (
        patch("app.core.credentials.stored_api_key_snapshot", store.snapshot),
        patch("app.core.credentials.set_stored_api_key", store.set),
        patch("app.core.credentials.clear_stored_api_key", store.clear),
        patch("app.core.credentials.set_stored_api_key_detailed", store.set_detailed),
        patch("app.core.credentials.clear_stored_api_key_detailed", store.clear_detailed),
        patch("app.core.preferences.store_many", preferences.store_many),
        patch("app.core.preferences.get", preferences.get),
    )


def _save(store, preferences, key=FAKE_KEY, workspace=FAKE_WORKSPACE, state="verified"):
    from app.core.ai import credential_pair

    patches = _wired(store, preferences)
    for entered in patches:
        entered.start()
    try:
        return credential_pair.save(key, workspace, state)
    finally:
        for entered in patches:
            entered.stop()


def _clear(store, preferences):
    from app.core.ai import credential_pair

    patches = _wired(store, preferences)
    for entered in patches:
        entered.start()
    try:
        return credential_pair.clear()
    finally:
        for entered in patches:
            entered.stop()


def _workspace_key():
    from app.core.ai.workspace import PREFERENCE_KEY
    return PREFERENCE_KEY


def _state_key():
    from app.core.providers import VERIFICATION_PREFERENCE
    return VERIFICATION_PREFERENCE


# ---------------------------------------------------------------------------
# Blocker 2 — the two stores are one operation, or they are not an operation
# ---------------------------------------------------------------------------

def test_the_happy_path_puts_both_stores_in_the_intended_state():
    store, preferences = _Store(), _Preferences()
    outcome = _save(store, preferences)

    assert outcome.ok is True
    assert store.value == FAKE_KEY
    assert preferences.data[_workspace_key()] == FAKE_WORKSPACE
    assert preferences.data[_state_key()] == "verified"


def test_a_credential_write_that_fails_changes_nothing_at_all():
    store = _Store(initial=OLD_KEY, fail_on={1})
    preferences = _Preferences({_workspace_key(): OLD_WORKSPACE, _state_key(): "verified"})

    outcome = _save(store, preferences)

    assert outcome.ok is False
    assert outcome.consistent is True, "nothing was written, so nothing can disagree"
    assert store.value == OLD_KEY
    assert preferences.data[_workspace_key()] == OLD_WORKSPACE
    assert preferences.writes == [], "metadata must not be touched after a failed credential write"


def test_a_failed_metadata_write_is_never_reported_as_success():
    """The defect: `store_preferences(...)`'s result was discarded and the
    endpoint answered that the key was saved and verified."""
    store, preferences = _Store(), _Preferences(fail_on={1})

    outcome = _save(store, preferences)

    assert outcome.ok is False
    assert "verified" not in outcome.message.lower()


def test_replacing_a_working_pair_rolls_the_credential_back_when_the_metadata_write_fails():
    """The state review named: new key in Credential Manager, previous
    key's workspace in preferences, previous key's verdict, and a success
    response. Rolling the credential back restores the pair that worked."""
    store = _Store(initial=OLD_KEY)
    preferences = _Preferences(
        {_workspace_key(): OLD_WORKSPACE, _state_key(): "verified"}, fail_on={1},
    )

    outcome = _save(store, preferences)

    assert outcome.ok is False
    assert outcome.rolled_back is True
    assert outcome.consistent is True
    assert store.value == OLD_KEY, "the previously working key must still be there"
    assert preferences.data[_workspace_key()] == OLD_WORKSPACE
    assert preferences.data[_state_key()] == "verified"


def test_a_first_time_save_whose_metadata_write_fails_leaves_no_orphan_credential():
    """With no previous key, rolling back means clearing the one just
    written — otherwise the installation holds a credential no metadata
    describes."""
    store = _Store(initial="")
    preferences = _Preferences(fail_on={1})

    outcome = _save(store, preferences)

    assert outcome.ok is False
    assert outcome.rolled_back is True
    assert store.value == ""


def test_a_failed_rollback_is_reported_precisely_and_never_as_success():
    """Both writes failed in sequence. There is no honest way to call that
    a save, and no honest way to call the result consistent."""
    store = _Store(initial=OLD_KEY, fail_on={2})       # the rollback write fails
    preferences = _Preferences(
        {_workspace_key(): OLD_WORKSPACE, _state_key(): "verified"}, fail_on={1, 2},
    )

    outcome = _save(store, preferences)

    assert outcome.ok is False
    assert outcome.rolled_back is False
    assert outcome.consistent is False
    assert outcome.outcome == "inconsistent"
    assert outcome.category, "a partial failure must carry a category the UI can act on"
    assert "verified" not in outcome.message.lower()


def test_a_failed_rollback_still_tries_to_stop_the_stale_metadata_describing_the_new_key():
    """If the credential cannot be put back, the metadata that described
    the old one is now attached to a different key. Clearing it is the only
    remaining way to stop the status page reporting a verification that
    belongs to a credential that is gone."""
    store = _Store(initial=OLD_KEY, fail_on={2})
    preferences = _Preferences(
        {_workspace_key(): OLD_WORKSPACE, _state_key(): "verified"}, fail_on={1},
    )

    outcome = _save(store, preferences)

    assert outcome.ok is False
    assert preferences.data.get(_workspace_key()) in (None, "")
    assert preferences.data.get(_state_key()) in (None, "")


def test_the_previous_key_is_never_read_into_a_log_line():
    """The snapshot exists so a failure can be undone. It must not exist
    anywhere else — not a log, not a message, not the response."""
    import logging

    store = _Store(initial=OLD_KEY)
    preferences = _Preferences(fail_on={1})

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    logging.getLogger().addHandler(handler)
    try:
        outcome = _save(store, preferences)
    finally:
        logging.getLogger().removeHandler(handler)

    rendered = "\n".join(logging.Formatter().format(record) for record in records)
    assert OLD_KEY not in rendered
    assert FAKE_KEY not in rendered
    assert OLD_WORKSPACE not in rendered
    assert FAKE_WORKSPACE not in rendered
    assert OLD_KEY not in outcome.message


def test_removal_that_cannot_clear_the_metadata_does_not_claim_complete_success():
    """Otherwise the next key entered silently inherits the workspace of
    the one before it."""
    store = _Store(initial=OLD_KEY)
    preferences = _Preferences(
        {_workspace_key(): OLD_WORKSPACE, _state_key(): "verified"}, fail_on={1},
    )

    outcome = _clear(store, preferences)

    assert store.value == ""
    assert outcome.ok is False
    assert outcome.consistent is False
    assert "removed" in outcome.message.lower()


def test_removal_that_cannot_reach_the_credential_store_changes_nothing():
    store = _Store(initial=OLD_KEY, fail_on={1})
    preferences = _Preferences({_workspace_key(): OLD_WORKSPACE})

    outcome = _clear(store, preferences)

    assert outcome.ok is False
    assert store.value == OLD_KEY
    assert preferences.writes == []


def test_removal_clears_both_when_both_stores_answer():
    store = _Store(initial=OLD_KEY)
    preferences = _Preferences({_workspace_key(): OLD_WORKSPACE, _state_key(): "verified"})

    outcome = _clear(store, preferences)

    assert outcome.ok is True
    assert store.value == ""
    assert preferences.data.get(_workspace_key()) in (None, "")
    assert preferences.data.get(_state_key()) in (None, "")


def test_an_unreadable_previous_credential_is_never_treated_as_no_credential():
    """"" comes back both when there is no key and when the store could not
    be reached. Rolling back the second case by *clearing* would delete a
    key that may still be the only working one."""
    store = _Store(initial=OLD_KEY)
    store.reachable = False
    preferences = _Preferences(fail_on={1})

    outcome = _save(store, preferences)

    assert outcome.ok is False
    assert store.value != "", "an unverifiable snapshot must not authorise a destructive rollback"


def test_the_endpoint_reports_the_partial_state_rather_than_success(api_client):
    """End to end: the route must not turn a partial failure into
    `success: true`, and must not report the key as stored-and-verified.

    The worst ordering, deliberately: the credential was replaced, its
    metadata could not be written, and the credential could not be put
    back either.
    """
    from app.core.ai.key_check import KeyVerification

    verification = KeyVerification(ok=True, message="API key verified.", worth_storing=True)
    with patch("app.core.ai.key_check.verify_anthropic_key", return_value=verification), \
         patch("app.core.credentials.stored_api_key_snapshot", return_value=(True, OLD_KEY)), \
         patch("app.core.credentials.set_stored_api_key_detailed", side_effect=[_applied(), _refused()]), \
         patch("app.core.credentials.clear_stored_api_key_detailed", return_value=_refused()), \
         patch("app.core.preferences.store_many", return_value=False):
        response = api_client.post(
            "/settings/api-key",
            json={"api_key": FAKE_KEY, "workspace_id": FAKE_WORKSPACE},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is False
    # The page needs to know the key still has to be re-saved even though
    # it did reach the credential store.
    assert body["stored"] is True
    assert body["consistent"] is False
    assert FAKE_KEY not in response.text
    assert FAKE_WORKSPACE not in response.text


def test_an_ordinary_save_reports_a_consistent_pair(api_client):
    from app.core.ai.key_check import KeyVerification

    verification = KeyVerification(ok=True, message="API key verified.", worth_storing=True)
    with patch("app.core.ai.key_check.verify_anthropic_key", return_value=verification), \
         patch("app.core.credentials.stored_api_key_snapshot", return_value=(True, "")), \
         patch("app.core.credentials.set_stored_api_key_detailed", return_value=_applied()):
        response = api_client.post(
            "/settings/api-key", json={"api_key": FAKE_KEY, "workspace_id": FAKE_WORKSPACE},
        )

    body = response.json()
    assert body["success"] is True
    assert body["consistent"] is True


def test_a_partial_removal_is_reported_through_the_endpoint(api_client):
    with patch("app.core.credentials.clear_stored_api_key_detailed", return_value=_applied()), \
         patch("app.core.preferences.store_many", return_value=False):
        response = api_client.post("/settings/api-key/remove")

    body = response.json()
    assert body["success"] is False
    assert body["consistent"] is False
    assert "removed" in body["message"].lower()


# ---------------------------------------------------------------------------
# Blocker 3 — the path that exposed the defect must leave a trace
# ---------------------------------------------------------------------------

class _WorkspaceRequired(Exception):
    """Shaped like the SDK's BadRequestError for the observed 400."""

    def __str__(self):
        return (
            "Error code: 400 - {'type': 'error', 'error': {'type': "
            "'invalid_request_error', 'message': 'anthropic-workspace-id is required "
            "when authenticating with an identity-linked API key; send the id of the "
            "workspace this request acts in.'}}"
        )


class _FailingProvider:
    name = "anthropic"

    def __init__(self, config):
        self._config = config

    def resolved_model(self):
        return "claude-haiku-4-5-20251001"

    def generate(self, messages, system, cancel=None):
        from app.core.ai.base import ProviderError
        from app.core.errors import classify_anthropic_exception

        exc = _WorkspaceRequired()
        raise ProviderError(classify_anthropic_exception(exc), cause=exc) from exc


def test_a_failed_key_verification_writes_exactly_one_safe_logs_row():
    """The real-PC failure happened here, in Settings, and the Logs page
    stayed empty — so there was no second place to look."""
    from app.core.ai.key_check import verify_anthropic_key

    with patch("db.database.get_db") as get_db:
        result = verify_anthropic_key(FAKE_KEY, FAKE_WORKSPACE, provider_factory=_FailingProvider)

    assert result.ok is False
    assert result.category == ErrorCategory.PROVIDER_WORKSPACE_REQUIRED
    assert get_db.return_value.log_action.call_count == 1


def test_the_verification_logs_row_carries_a_reference_and_no_secret():
    from app.core.ai.events import EVENT_CATEGORY
    from app.core.ai.key_check import verify_anthropic_key

    with patch("db.database.get_db") as get_db:
        verify_anthropic_key(FAKE_KEY, FAKE_WORKSPACE, provider_factory=_FailingProvider)

    kwargs = get_db.return_value.log_action.call_args.kwargs
    assert kwargs["command"] == EVENT_CATEGORY
    assert kwargs["tool_name"] == "anthropic"
    assert kwargs["status"] == ErrorCategory.PROVIDER_WORKSPACE_REQUIRED.value
    assert re.search(r"reference [0-9a-f-]{8,}", kwargs["message"])

    written = " ".join(str(value) for value in kwargs.values())
    for secret in (FAKE_KEY, FAKE_WORKSPACE, "x-api-key", "Error code: 400", "identity-linked"):
        assert secret not in written, f"{secret!r} reached the Logs page"


def test_a_verification_that_succeeds_writes_no_failure_row():
    from app.core.ai.key_check import verify_anthropic_key

    class _Working(_FailingProvider):
        def generate(self, messages, system, cancel=None):
            from app.core.ai.base import ProviderReply
            return ProviderReply(content="ok", provider="anthropic", model="m", used_api=True)

    with patch("db.database.get_db") as get_db:
        result = verify_anthropic_key(FAKE_KEY, FAKE_WORKSPACE, provider_factory=_Working)

    assert result.ok is True
    get_db.return_value.log_action.assert_not_called()


def test_a_malformed_workspace_id_is_refused_without_spending_a_request_and_still_leaves_a_row():
    """No request is made — and it still gets a Logs row, because "every
    failed key save leaves a trace" has to include the ones the user is
    least likely to understand. The row carries JARVIS's own guidance
    sentence, never the value that was rejected."""
    from app.core.ai.key_check import verify_anthropic_key

    def _never(*args, **kwargs):
        raise AssertionError("a shape failure must not reach the provider")

    with patch("db.database.get_db") as get_db:
        result = verify_anthropic_key(FAKE_KEY, "not-a: workspace id", provider_factory=_never)

    assert result.ok is False
    assert result.worth_storing is False
    assert get_db.return_value.log_action.call_count == 1
    written = " ".join(str(v) for v in get_db.return_value.log_action.call_args.kwargs.values())
    assert "not-a: workspace id" not in written
    assert FAKE_KEY not in written


def test_the_endpoint_produces_the_logs_row_for_the_observed_failure(api_client):
    """The endpoint-level proof: one submission of the workspace-required
    failure, exactly one safe row, nothing sensitive in it."""
    with patch("db.database.get_db") as get_db, \
         patch("app.core.ai.anthropic_provider.AnthropicProvider", _FailingProvider):
        response = api_client.post(
            "/settings/api-key",
            json={"api_key": FAKE_KEY, "workspace_id": FAKE_WORKSPACE},
        )

    assert response.status_code == 200
    assert response.json()["success"] is False

    rows = [
        call.kwargs for call in get_db.return_value.log_action.call_args_list
        if call.kwargs.get("command") == "ai_provider"
    ]
    assert len(rows) == 1, f"expected exactly one provider row, got {len(rows)}"
    assert rows[0]["status"] == ErrorCategory.PROVIDER_WORKSPACE_REQUIRED.value
    assert FAKE_WORKSPACE not in str(rows[0])


# ---------------------------------------------------------------------------
# Blocker 4 — the file log is a place a workspace ID must never reach
# ---------------------------------------------------------------------------

class _RecordingLogger:
    """Captures every call, and renders it the way the logging module
    eventually would — including anything reachable through `exc_info`."""

    def __init__(self):
        self.calls = []

    def _record(self, level):
        def call(message, *args, **kwargs):
            self.calls.append((level, message, args, kwargs))
        return call

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error", "critical", "exception"):
            return self._record(name)
        raise AttributeError(name)

    def rendered(self) -> str:
        import traceback

        parts = []
        for level, message, args, kwargs in self.calls:
            try:
                parts.append(message % args if args else str(message))
            except Exception:  # noqa: BLE001
                parts.append(f"{message!r} {args!r}")
            for key, value in kwargs.items():
                if key == "exc_info" and isinstance(value, BaseException):
                    parts.append("".join(traceback.format_exception(
                        type(value), value, value.__traceback__,
                    )))
                # repr, because that is what a formatter or a debugger would
                # reach, and an exception's repr contains its arguments.
                parts.append(f"{key}={value!r}")
        return "\n".join(parts)


class _WorkspaceNotFound(Exception):
    """Anthropic's documented 404 body for an inaccessible workspace.

    "If the workspace doesn't exist, or the key's user or service account
    doesn't have access to it, the API returns a 404 not_found_error with
    the message ``Workspace `<id>` not found.``"
    """

    def __str__(self):
        return (
            f"Error code: 404 - {{'type': 'error', 'error': {{'type': 'not_found_error', "
            f"'message': 'Workspace `{FAKE_WORKSPACE}` not found.'}}}} "
            f"headers={{'x-api-key': '{FAKE_KEY}'}}"
        )


def _raised(exc_type):
    try:
        raise exc_type()
    except Exception as exc:  # noqa: BLE001
        return exc


def test_the_safe_error_boundary_never_renders_the_provider_message():
    """`to_safe_error(exc, ...)` logged `exc_info=exc`. Anthropic's own 404
    quotes the workspace ID back, so that one keyword put it in
    jarvis.log."""
    from app.core import errors

    recorder = _RecordingLogger()
    with patch.object(errors, "logger", recorder):
        errors.to_safe_error(
            _raised(_WorkspaceNotFound),
            category=ErrorCategory.PROVIDER_ERROR,
            context="anthropic generation",
        )

    rendered = recorder.rendered()
    assert FAKE_WORKSPACE not in rendered, "the workspace ID reached the file log"
    assert FAKE_KEY not in rendered, "an authorization header reached the file log"
    assert "not_found_error" not in rendered, "the raw provider response body reached the file log"


def test_the_safe_error_boundary_still_records_what_a_developer_needs():
    from app.core import errors

    recorder = _RecordingLogger()
    with patch.object(errors, "logger", recorder):
        safe = errors.to_safe_error(
            _raised(_WorkspaceNotFound),
            category=ErrorCategory.PROVIDER_ERROR,
            context="anthropic generation",
        )

    rendered = recorder.rendered()
    assert safe.correlation_id in rendered
    assert "provider_error" in rendered
    assert "_WorkspaceNotFound" in rendered, "the exception type is safe and is the whole point"
    assert "anthropic generation" in rendered


def test_the_safe_error_boundary_keeps_traceback_structure_without_the_value():
    """Frames locate the failure; the exception's rendered value is what
    carries the provider's text."""
    from app.core import errors

    recorder = _RecordingLogger()
    with patch.object(errors, "logger", recorder):
        errors.to_safe_error(_raised(_WorkspaceNotFound), category=ErrorCategory.PROVIDER_ERROR)

    rendered = recorder.rendered()
    assert "test_anthropic_credential_lifecycle.py" in rendered, "no frame survived"
    assert "_raised" in rendered


def test_the_key_verification_path_does_not_log_a_raw_provider_exception():
    from app.core.ai import key_check

    class _Unclassified(_FailingProvider):
        def generate(self, messages, system, cancel=None):
            raise _WorkspaceNotFound()

    recorder = _RecordingLogger()
    with patch.object(key_check, "logger", recorder), patch("db.database.get_db"):
        key_check.verify_anthropic_key(FAKE_KEY, FAKE_WORKSPACE, provider_factory=_Unclassified)

    rendered = recorder.rendered()
    assert FAKE_WORKSPACE not in rendered
    assert FAKE_KEY not in rendered


#: Every module that can hold a live provider exception. In these, a
#: logging call may not carry `exc_info` at all: the logging module renders
#: str(exc) either way, whether it is handed the exception or told to fetch
#: the ambient one, and str(exc) of an Anthropic SDK error is the response
#: body. Elsewhere in the app the exception is ours and exc_info is fine.
PROVIDER_FACING_MODULES = (
    "app/core/errors.py",
    "app/core/brain.py",
    "app/core/ai/key_check.py",
    "app/core/ai/anthropic_provider.py",
    "app/api/chat.py",
)


@pytest.mark.parametrize("module", PROVIDER_FACING_MODULES)
def test_no_provider_facing_module_hands_an_exception_to_the_logger(module):
    """Structural, because the leak was one keyword in one call and the
    next one would be just as easy to add."""
    import ast
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / module
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if not isinstance(target, ast.Attribute):
            continue
        if not (isinstance(target.value, ast.Name) and target.value.id == "logger"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "exc_info":
                offenders.append(f"{module}:{node.lineno}")
    assert offenders == [], (
        f"a provider exception can be rendered into jarvis.log at {offenders}; "
        f"use app/core/safe_traceback.py::describe() instead"
    )


def test_the_credential_store_never_logs_a_traceback_that_could_quote_the_key():
    """app/core/credentials.py already refuses to log `str(exc)` because a
    backend exception may quote the value it was asked to store — and then
    logged `exc_info=True`, which renders exactly that."""
    from app.core import credentials

    source = (
        __import__("pathlib").Path(credentials.__file__).read_text(encoding="utf-8")
    )
    mutate = source[source.index("def _mutate("):source.index("def _set(")]
    # Comments are allowed to say the word; code is not.
    code = "\n".join(
        line for line in mutate.splitlines() if not line.strip().startswith("#")
    )
    assert "exc_info" not in code, (
        "a keyring failure must not be logged with a traceback: the exception may quote the key"
    )


# ---------------------------------------------------------------------------
# Blocker 5 — a check that did not happen is not a rejection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("category,expected", [
    (None, "verified"),                                     # the provider answered
    (ErrorCategory.PROVIDER_TIMEOUT, "configured_unverified"),
    (ErrorCategory.PROVIDER_UNAVAILABLE, "configured_unverified"),
    (ErrorCategory.PROVIDER_RATE_LIMIT, "configured_unverified"),
    (ErrorCategory.PROVIDER_BILLING, "account_unfunded"),
])
def test_each_verification_outcome_maps_to_a_state_that_describes_it(category, expected):
    from app.core.providers import state_for_verification

    assert state_for_verification(ok=category is None, category=category) == expected


@pytest.mark.parametrize("category", [
    ErrorCategory.PROVIDER_TIMEOUT,
    ErrorCategory.PROVIDER_UNAVAILABLE,
    ErrorCategory.PROVIDER_RATE_LIMIT,
])
def test_a_check_that_could_not_run_is_never_described_as_a_rejection(category):
    """"The saved API key was rejected by Anthropic" for a machine that was
    simply offline is a false diagnosis, and it sends someone to replace a
    key that was never the problem."""
    from app.core.providers import _CREDENTIAL_DETAIL, state_for_verification

    state = state_for_verification(ok=False, category=category)
    detail = _CREDENTIAL_DETAIL[state].lower()
    assert "reject" not in detail


def test_an_unfunded_account_is_not_described_as_unchecked_either():
    """It *was* checked. Anthropic answered, and the answer was about
    credit, not about the key."""
    from app.core.providers import _CREDENTIAL_DETAIL, state_for_verification

    state = state_for_verification(ok=False, category=ErrorCategory.PROVIDER_BILLING)
    detail = _CREDENTIAL_DETAIL[state].lower()
    assert "has not been checked" not in detail
    assert "credit" in detail or "billing" in detail or "funded" in detail


@pytest.mark.parametrize("category", [
    ErrorCategory.PROVIDER_AUTH,
    ErrorCategory.PROVIDER_WORKSPACE_REQUIRED,
])
def test_an_explicitly_rejected_pair_is_not_stored_at_all(api_client, category):
    from app.core.ai.key_check import KeyVerification

    verification = KeyVerification(
        ok=False, message="no", category=category, worth_storing=False,
    )
    with patch("app.core.ai.key_check.verify_anthropic_key", return_value=verification), \
         patch("app.core.credentials.set_stored_api_key") as write:
        response = api_client.post(
            "/settings/api-key", json={"api_key": FAKE_KEY, "workspace_id": FAKE_WORKSPACE},
        )

    assert response.json()["stored"] is False
    write.assert_not_called()


def test_a_runtime_rejection_downgrades_a_previously_verified_credential():
    """"Verified" means "answered successfully the last time it was
    checked". A key that is revoked afterwards must not go on being
    reported as working."""
    from app.core import providers

    preferences = _Preferences({_state_key(): "verified"})
    with patch("app.core.preferences.store_many", preferences.store_many), \
         patch("app.core.preferences.get", preferences.get):
        providers.note_runtime_failure("anthropic", ErrorCategory.PROVIDER_AUTH)

    assert preferences.data[_state_key()] == "verification_failed"


@pytest.mark.parametrize("category", [
    ErrorCategory.PROVIDER_TIMEOUT,
    ErrorCategory.PROVIDER_RATE_LIMIT,
    ErrorCategory.PROVIDER_UNAVAILABLE,
])
def test_a_transient_runtime_failure_never_downgrades_anything(category):
    from app.core import providers

    preferences = _Preferences({_state_key(): "verified"})
    with patch("app.core.preferences.store_many", preferences.store_many), \
         patch("app.core.preferences.get", preferences.get):
        providers.note_runtime_failure("anthropic", category)

    assert preferences.data[_state_key()] == "verified"
    assert preferences.writes == []


def test_a_runtime_failure_never_upgrades_a_state():
    from app.core import providers

    preferences = _Preferences({_state_key(): "verification_failed"})
    with patch("app.core.preferences.store_many", preferences.store_many), \
         patch("app.core.preferences.get", preferences.get):
        providers.note_runtime_failure("anthropic", ErrorCategory.PROVIDER_AUTH)

    assert preferences.data[_state_key()] == "verification_failed"


def test_another_providers_failure_never_touches_the_anthropic_state():
    from app.core import providers

    preferences = _Preferences({_state_key(): "verified"})
    with patch("app.core.preferences.store_many", preferences.store_many), \
         patch("app.core.preferences.get", preferences.get):
        providers.note_runtime_failure("ollama", ErrorCategory.PROVIDER_AUTH)

    assert preferences.data[_state_key()] == "verified"


def test_note_runtime_failure_never_raises():
    from app.core import providers

    with patch("app.core.preferences.get", side_effect=RuntimeError("preferences are gone")):
        providers.note_runtime_failure("anthropic", ErrorCategory.PROVIDER_AUTH)


def test_verified_does_not_promise_the_key_will_keep_working():
    from app.core.providers import CREDENTIAL_VERIFIED, _CREDENTIAL_DETAIL

    detail = _CREDENTIAL_DETAIL[CREDENTIAL_VERIFIED].lower()
    assert "last" in detail or "when it was checked" in detail, (
        "the verified sentence reads as a permanent guarantee"
    )


def test_only_the_verified_state_reports_the_provider_as_available():
    from app.core import providers

    for state in (
        providers.CREDENTIAL_NOT_CONFIGURED,
        providers.CREDENTIAL_UNVERIFIED,
        providers.CREDENTIAL_FAILED,
        providers.CREDENTIAL_UNFUNDED,
    ):
        with patch("app.core.providers.anthropic_credential_state", return_value=state):
            assert providers.anthropic_status().available is False
    with patch("app.core.providers.anthropic_credential_state",
               return_value=providers.CREDENTIAL_VERIFIED):
        assert providers.anthropic_status().available is True


def test_every_state_has_its_own_sentence():
    from app.core.providers import _CREDENTIAL_DETAIL

    assert len(set(_CREDENTIAL_DETAIL.values())) == len(_CREDENTIAL_DETAIL)
