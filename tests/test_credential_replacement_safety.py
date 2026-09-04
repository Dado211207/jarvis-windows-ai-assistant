"""What a *failed* credential write leaves behind, and what JARVIS is
allowed to say about it.

Four independently reported defects, each with the failure written first:

**1. A failed replacement could destroy the key it was replacing.**
`credentials._mutate()` reconciled every failed non-None write to
*absence* — it recorded `None` as the newest desired value and queued a
delete behind the late operation. That is right for a first-time save,
where a late `set_password` would otherwise leave a credential nobody
asked for. It is destructive for a **replacement**, because the old key
and the new key are the same Credential Manager entry: the reconciler
deletes the key that was already working, and `credential_pair.save()`
answers "Nothing was changed."

**2. Removal reported states it had not established, and offered a
recovery the UI cannot perform.** A timed-out delete may still land, so
"Nothing was changed" is not a fact; and telling someone to "clear the
Workspace ID field and save" is impossible while `SetApiKeyRequest`
refuses a blank API key.

**3. `exc_info=True` survived on failure paths this feature added to.**
A SQLite or filesystem exception quotes paths — `C:\\Users\\<account>\\…`
— and `exc_info` renders the whole traceback, which is the thing
`safe_traceback.py` exists to trim.

**4. `note_runtime_failure()` ignored `store_many()`'s result** and logged
a downgrade that may never have been written.

Every wait here is on a `threading.Event` or a thread join. There are no
sleeps: a timing-dependent test of a timing defect proves nothing.
"""

import sys
import threading
import time
import types

import pytest

def _current_revision():
    """The revision a request made right now would carry.

    `note_runtime_failure()` requires it: a rejection must name the pair it
    was about, so a delayed failure cannot downgrade a credential that
    replaced the one it actually used. These tests are about what a
    rejection of the *current* credential does, so they pass the current
    revision — the stale case has its own file,
    tests/test_credential_read_coherence.py.
    """
    from app.core.ai import credential_view

    # The coordinator's own number rather than a snapshot's, because a
    # snapshot taken from a store these tests have patched into failing
    # reads as unreadable and carries -1 — which correctly matches nothing.
    return credential_view.current_revision()


# Obviously fake, and shaped like the real things so a leak is unmistakable.
OLD_KEY = "sk-ant-api03-PREVIOUS-key-that-must-survive"
NEW_KEY = "sk-ant-api03-REPLACEMENT-key-being-saved"
FAKE_WORKSPACE = "wrkspc_01TESTONLYnotarealworkspace"
FAKE_USERNAME = "TestAccountName"
FAKE_PRIVATE_PATH = r"C:\Users\TestAccountName\AppData\Local\JARVIS\data\jarvis.db"

#: Long enough that a correct implementation never hits it, short enough
#: that a deadlocked one fails the run rather than hanging it.
JOIN_TIMEOUT = 10.0

MUTATION_THREAD_PREFIX = "jarvis-keyring-mutation"


# ---------------------------------------------------------------------------
# A keyring the test drives, rather than races
# ---------------------------------------------------------------------------

class _ControllableKeyring:
    """A fake Windows Credential Manager whose writes can be made to fail
    once, or to block inside the backend until the test releases them.

    `block_next_set` reproduces the real shape of the defect: keyring calls
    that time out keep running in Python, because a `Future` timeout does
    not cancel a call already inside WinCred. The blocked call completes
    *after* the request that started it has already answered the user.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._store = {}
        self._fail_sets = 0
        self._block_sets = 0
        self._fail_deletes = 0
        self._block_deletes = 0
        self.release_set = threading.Event()
        self.release_delete = threading.Event()
        self.set_entered = threading.Event()
        self.delete_entered = threading.Event()
        #: Every applied operation, in order. The evidence the assertions read.
        self.applied = []
        self.errors = None  # replaced by _install_keyring

    # -- what the module under test calls -----------------------------------

    def get_password(self, service, username):
        with self._lock:
            return self._store.get((service, username))

    def set_password(self, service, username, value):
        with self._lock:
            failing = self._fail_sets > 0
            blocking = self._block_sets > 0
            if failing:
                self._fail_sets -= 1
            if blocking:
                self._block_sets -= 1
        self.set_entered.set()
        if failing:
            raise RuntimeError("the credential backend refused this write")
        if blocking:
            assert self.release_set.wait(JOIN_TIMEOUT), "the test never released the blocked write"
        with self._lock:
            self._store[(service, username)] = value
            self.applied.append(("set", username, value))

    def delete_password(self, service, username):
        with self._lock:
            failing = self._fail_deletes > 0
            blocking = self._block_deletes > 0
            if failing:
                self._fail_deletes -= 1
            if blocking:
                self._block_deletes -= 1
        self.delete_entered.set()
        if failing:
            raise RuntimeError("the credential backend refused this delete")
        if blocking:
            assert self.release_delete.wait(JOIN_TIMEOUT), "the test never released the blocked delete"
        with self._lock:
            if (service, username) not in self._store:
                raise self.errors.PasswordDeleteError("not found")
            del self._store[(service, username)]
            self.applied.append(("delete", username, None))

    # -- what the test calls ------------------------------------------------

    def preload(self, service, username, value):
        with self._lock:
            self._store[(service, username)] = value

    def stored(self, service, username):
        with self._lock:
            return self._store.get((service, username))

    def fail_next_set(self, count=1):
        with self._lock:
            self._fail_sets = count

    def block_next_set(self, count=1):
        with self._lock:
            self._block_sets = count

    def fail_next_delete(self, count=1):
        with self._lock:
            self._fail_deletes = count

    def block_next_delete(self, count=1):
        with self._lock:
            self._block_deletes = count


class _UnreadableKeyring(_ControllableKeyring):
    """A store that cannot be read at all — the case that must never be
    mistaken for "there is no credential here"."""

    def get_password(self, service, username):
        raise RuntimeError("the credential backend could not be read")


def _install_keyring(monkeypatch, fake):
    errors_module = types.ModuleType("keyring.errors")

    class PasswordDeleteError(Exception):
        pass

    errors_module.PasswordDeleteError = PasswordDeleteError
    fake.errors = errors_module

    keyring_module = types.ModuleType("keyring")
    keyring_module.get_password = fake.get_password
    keyring_module.set_password = fake.set_password
    keyring_module.delete_password = fake.delete_password
    keyring_module.errors = errors_module

    monkeypatch.setitem(sys.modules, "keyring", keyring_module)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors_module)
    return fake


def settle(timeout=JOIN_TIMEOUT):
    """Block until no credential mutation worker is still running.

    Assertions about "what the store ended up holding" are meaningless
    while a late `set_password` or a queued reconciliation is still in
    flight — that lateness *is* the defect. Joining the mutation threads
    is deliberately used in preference to a sleep, and works identically
    before and after the correction, so a failure here is the defect and
    never the harness.
    """
    deadline = time.monotonic() + timeout
    while True:
        alive = [
            thread for thread in threading.enumerate()
            if thread.name.startswith(MUTATION_THREAD_PREFIX) and thread.is_alive()
        ]
        if not alive:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("credential mutation workers did not settle")
        for thread in alive:
            thread.join(timeout=max(0.01, deadline - time.monotonic()))


@pytest.fixture(autouse=True)
def _clean_keyring_import():
    sys.modules.pop("keyring", None)
    sys.modules.pop("keyring.errors", None)
    yield
    settle()
    sys.modules.pop("keyring", None)
    sys.modules.pop("keyring.errors", None)


def test_the_mutation_worker_threads_are_still_named_what_settle_joins():
    """`settle()` above is only sound while this prefix is what the module
    uses. A rename must fail here rather than silently make every timing
    assertion in this file vacuous."""
    import pathlib

    from app.core import credentials

    source = pathlib.Path(credentials.__file__).read_text(encoding="utf-8")
    assert f'thread_name_prefix="{MUTATION_THREAD_PREFIX}"' in source


# ---------------------------------------------------------------------------
# Blocker 1 — a failed replacement must never destroy the previous key
# ---------------------------------------------------------------------------

def test_an_existing_key_survives_a_replacement_the_backend_refuses(monkeypatch):
    """The old and new Anthropic keys are the same Credential Manager
    entry. A write that fails must leave the entry holding what it held."""
    from app.core import credentials

    fake = _install_keyring(monkeypatch, _ControllableKeyring())
    fake.preload(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake.fail_next_set(1)

    assert credentials.set_stored_api_key(NEW_KEY) is False
    settle()

    assert fake.stored(credentials.SERVICE_NAME, credentials.USERNAME) == OLD_KEY, (
        "a failed replacement destroyed the key it was replacing"
    )


def test_an_existing_key_survives_a_replacement_that_times_out_and_lands_late(monkeypatch):
    """The real shape: `set_password` does not return before the timeout,
    the request answers, and the backend call completes afterwards. The
    reconciliation behind it must target the previous key, not absence."""
    from app.core import credentials

    monkeypatch.setattr(credentials, "TIMEOUT_SECONDS", 0.2)
    fake = _install_keyring(monkeypatch, _ControllableKeyring())
    fake.preload(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake.block_next_set(1)

    assert credentials.set_stored_api_key(NEW_KEY) is False
    assert fake.set_entered.wait(JOIN_TIMEOUT), "the write never reached the backend"
    fake.release_set.set()          # the late call now completes, as WinCred would
    settle()

    assert fake.stored(credentials.SERVICE_NAME, credentials.USERNAME) == OLD_KEY, (
        "a replacement that timed out left the previous key destroyed or replaced"
    )


def test_a_first_time_save_that_times_out_leaves_no_late_orphan_key(monkeypatch):
    """The property the destructive reconciliation was written for, which
    the correction must keep: with no previous credential, a late write
    must not leave a key the user was told was not saved."""
    from app.core import credentials

    monkeypatch.setattr(credentials, "TIMEOUT_SECONDS", 0.2)
    fake = _install_keyring(monkeypatch, _ControllableKeyring())
    fake.block_next_set(1)

    assert credentials.set_stored_api_key(NEW_KEY) is False
    assert fake.set_entered.wait(JOIN_TIMEOUT), "the write never reached the backend"
    fake.release_set.set()
    settle()

    assert fake.stored(credentials.SERVICE_NAME, credentials.USERNAME) is None, (
        "a first-time save that failed left an orphan credential behind"
    )


def test_a_write_is_never_begun_when_the_store_cannot_be_read(monkeypatch):
    """A write that cannot be undone must not be started. Reading first is
    what makes the undo target a *proven* value rather than a guess."""
    from app.core import credentials

    fake = _install_keyring(monkeypatch, _UnreadableKeyring())

    assert credentials.set_stored_api_key(NEW_KEY) is False
    settle()

    assert fake.applied == [], (
        "JARVIS wrote to a credential store whose contents it could not read"
    )


def test_an_unreadable_store_never_deletes_an_unknown_credential(monkeypatch):
    """"Could not read" must never be treated as "there is nothing here".
    The entry may hold the only working key on the machine."""
    from app.core import credentials

    fake = _install_keyring(monkeypatch, _UnreadableKeyring())
    fake.preload(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake.fail_next_set(1)

    credentials.set_stored_api_key(NEW_KEY)
    settle()

    assert fake.stored(credentials.SERVICE_NAME, credentials.USERNAME) == OLD_KEY
    assert not any(operation == "delete" for operation, _u, _v in fake.applied), (
        "an unreadable snapshot authorised deleting a credential nobody had read"
    )


def test_a_refused_write_is_reported_as_provably_unchanged(monkeypatch):
    """The two failures are different facts and the caller has to be able
    to tell them apart: a backend that refused changed nothing, a call that
    never came back may still complete."""
    from app.core import credentials

    fake = _install_keyring(monkeypatch, _ControllableKeyring())
    fake.preload(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake.fail_next_set(1)

    result = credentials.set_stored_api_key_detailed(NEW_KEY)
    settle()

    assert result.ok is False
    assert result.provably_unchanged is True


def test_a_timed_out_write_is_never_reported_as_provably_unchanged(monkeypatch):
    from app.core import credentials

    monkeypatch.setattr(credentials, "TIMEOUT_SECONDS", 0.2)
    fake = _install_keyring(monkeypatch, _ControllableKeyring())
    fake.preload(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake.block_next_set(1)

    result = credentials.set_stored_api_key_detailed(NEW_KEY)
    fake.release_set.set()
    settle()

    assert result.ok is False
    assert result.provably_unchanged is False, (
        "a call that never returned was reported as having changed nothing"
    )


def test_neither_the_previous_nor_the_proposed_key_reaches_a_log(monkeypatch):
    """Both values are in memory on this path — the previous one because it
    is the restore target — so both have to be proven absent from the log."""
    from app.core import credentials

    recorder = _Recorder()
    monkeypatch.setattr(credentials, "logger", recorder)
    monkeypatch.setattr(credentials, "TIMEOUT_SECONDS", 0.2)
    fake = _install_keyring(monkeypatch, _ControllableKeyring())
    fake.preload(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake.block_next_set(1)

    credentials.set_stored_api_key(NEW_KEY)
    fake.release_set.set()
    settle()

    rendered = recorder.rendered()
    assert OLD_KEY not in rendered
    assert NEW_KEY not in rendered


# ---------------------------------------------------------------------------
# Blocker 1, at the route's own layer
# ---------------------------------------------------------------------------

def _pair():
    from app.core.ai import credential_pair
    return credential_pair


def test_save_refuses_outright_when_the_credential_store_cannot_be_read(monkeypatch):
    """`credential_pair.save()` claims a failed replacement cannot destroy a
    working pair. It can only claim that if it declines to start one it
    could not undo."""
    from unittest.mock import patch

    with patch("app.core.credentials.stored_api_key_snapshot", return_value=(False, "")), \
         patch("app.core.credentials.set_stored_api_key_detailed") as write, \
         patch("app.core.credentials.set_stored_api_key") as legacy_write:
        outcome = _pair().save(NEW_KEY, FAKE_WORKSPACE, "verified")

    assert outcome.ok is False
    assert outcome.stored is False
    write.assert_not_called()
    legacy_write.assert_not_called()


def test_save_does_not_claim_nothing_changed_after_an_unconfirmed_write():
    """"Nothing was changed" is a postcondition, not a consolation."""
    from unittest.mock import patch

    from app.core import credentials

    uncertain = credentials.MutationResult(credentials.MUTATION_UNCERTAIN, "timed_out")
    with patch("app.core.credentials.stored_api_key_snapshot", return_value=(True, OLD_KEY)), \
         patch("app.core.credentials.set_stored_api_key_detailed", return_value=uncertain):
        outcome = _pair().save(NEW_KEY, FAKE_WORKSPACE, "verified")

    assert outcome.ok is False
    assert "nothing was changed" not in outcome.message.lower()


def test_save_may_say_nothing_changed_when_the_store_refused_outright():
    from unittest.mock import patch

    from app.core import credentials

    refused = credentials.MutationResult(credentials.MUTATION_UNCHANGED, "backend_refused")
    with patch("app.core.credentials.stored_api_key_snapshot", return_value=(True, OLD_KEY)), \
         patch("app.core.credentials.set_stored_api_key_detailed", return_value=refused):
        outcome = _pair().save(NEW_KEY, FAKE_WORKSPACE, "verified")

    assert outcome.ok is False
    assert "nothing was changed" in outcome.message.lower()


# ---------------------------------------------------------------------------
# Blocker 2 — removal must be truthful, and its advice must be performable
# ---------------------------------------------------------------------------

def test_an_unconfirmed_removal_is_never_reported_as_unchanged():
    from unittest.mock import patch

    from app.core import credentials

    uncertain = credentials.MutationResult(credentials.MUTATION_UNCERTAIN, "timed_out")
    with patch("app.core.credentials.clear_stored_api_key_detailed", return_value=uncertain):
        outcome = _pair().clear()

    assert outcome.ok is False
    assert "nothing was changed" not in outcome.message.lower()
    assert outcome.consistent is False


def test_a_removal_the_store_refused_outright_may_say_nothing_changed():
    from unittest.mock import patch

    from app.core import credentials

    refused = credentials.MutationResult(credentials.MUTATION_UNCHANGED, "backend_refused")
    with patch("app.core.credentials.clear_stored_api_key_detailed", return_value=refused):
        outcome = _pair().clear()

    assert outcome.ok is False
    assert "nothing was changed" in outcome.message.lower()
    assert outcome.consistent is True


def test_a_removal_that_cannot_clear_metadata_tells_the_user_to_remove_again():
    """The previous instruction — clear the Workspace ID field and save —
    cannot be carried out: the same request requires a non-blank API key.
    Advice the UI cannot perform is worse than none."""
    from unittest.mock import patch

    from app.core import credentials

    applied = credentials.MutationResult(credentials.MUTATION_APPLIED)
    with patch("app.core.credentials.clear_stored_api_key_detailed", return_value=applied), \
         patch("app.core.preferences.store_many", return_value=False):
        outcome = _pair().clear()

    assert outcome.ok is False
    message = outcome.message.lower()
    assert "remove again" in message, "the message does not name the one recovery that works"
    assert "clear the workspace id field" not in message, (
        "the message still asks for a save the API refuses"
    )


def test_the_recovery_the_old_message_offered_is_impossible():
    """Proves the premise rather than asserting it: a blank API key is
    refused, so "clear the Workspace ID field and save" could never run."""
    import pydantic
    import pytest as _pytest

    from app.api.routes import SetApiKeyRequest

    with _pytest.raises(pydantic.ValidationError):
        SetApiKeyRequest(api_key="", workspace_id="")


def test_retrying_remove_finishes_the_metadata_cleanup(monkeypatch):
    """The recovery has to actually work: deleting an already-absent
    credential succeeds, so a second Remove reaches the metadata clear."""
    from unittest.mock import patch

    from app.core import credentials, preferences
    from app.core.ai.workspace import PREFERENCE_KEY as WORKSPACE_KEY
    from app.core.providers import VERIFICATION_PREFERENCE

    fake = _install_keyring(monkeypatch, _ControllableKeyring())
    fake.preload(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    preferences.store_many({WORKSPACE_KEY: FAKE_WORKSPACE, VERIFICATION_PREFERENCE: "verified"})

    with patch("app.core.preferences.store_many", return_value=False):
        first = _pair().clear()
    settle()

    assert first.ok is False
    assert first.consistent is False
    assert fake.stored(credentials.SERVICE_NAME, credentials.USERNAME) is None

    second = _pair().clear()          # the credential is already gone
    settle()

    assert second.ok is True
    assert second.consistent is True
    assert preferences.get(WORKSPACE_KEY) is None
    assert preferences.get(VERIFICATION_PREFERENCE) is None


def test_a_removal_that_lands_late_still_leaves_a_truthful_response(monkeypatch):
    """The delete completes after the response was written. The response
    must not have claimed the key was still there."""
    from app.core import credentials

    monkeypatch.setattr(credentials, "TIMEOUT_SECONDS", 0.2)
    fake = _install_keyring(monkeypatch, _ControllableKeyring())
    fake.preload(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake.block_next_delete(1)

    outcome = _pair().clear()
    assert fake.delete_entered.wait(JOIN_TIMEOUT), "the delete never reached the backend"
    fake.release_delete.set()
    settle()

    assert fake.stored(credentials.SERVICE_NAME, credentials.USERNAME) is None
    assert outcome.ok is False
    assert "nothing was changed" not in outcome.message.lower()


# ---------------------------------------------------------------------------
# Blocker 3 — no failure path may render an exception into the log
# ---------------------------------------------------------------------------

class _Recorder:
    """Captures logger calls and renders them the way `logging` would —
    including `exc_info=True`, which reaches for the *ambient* exception and
    is therefore invisible to a recorder that only inspects its arguments.
    """

    def __init__(self):
        self.calls = []

    def _record(self, level):
        def call(message, *args, **kwargs):
            self.calls.append((level, message, args, dict(kwargs), sys.exc_info()))
        return call

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error", "critical", "exception"):
            return self._record(name)
        raise AttributeError(name)

    def rendered(self) -> str:
        import traceback

        parts = []
        for level, message, args, kwargs, ambient in self.calls:
            try:
                parts.append(message % args if args else str(message))
            except Exception:  # noqa: BLE001
                parts.append(f"{message!r} {args!r}")
            for key, value in kwargs.items():
                if key == "exc_info":
                    target = value if isinstance(value, BaseException) else (
                        ambient[1] if value else None
                    )
                    if target is not None:
                        parts.append("".join(traceback.format_exception(
                            type(target), target, target.__traceback__,
                        )))
                        continue
                parts.append(f"{key}={value!r}")
            if level == "exception":
                target = ambient[1]
                if target is not None:
                    parts.append("".join(traceback.format_exception(
                        type(target), target, target.__traceback__,
                    )))
        return "\n".join(parts)


class _LeakyFailure(Exception):
    """Everything a real SQLite or filesystem exception can carry, and
    every one of them a thing that must not reach `jarvis.log`."""

    def __str__(self):
        return (
            f"unable to open database file: {FAKE_PRIVATE_PATH} "
            f"(user {FAKE_USERNAME}, key {OLD_KEY}, workspace {FAKE_WORKSPACE})"
        )


def _assert_safe(rendered: str):
    for secret in (OLD_KEY, FAKE_WORKSPACE, FAKE_USERNAME, FAKE_PRIVATE_PATH):
        assert secret not in rendered, f"{secret!r} reached the log"
    assert "_LeakyFailure" in rendered, "the exception type was lost as well as its value"


def test_a_failed_logs_write_does_not_render_the_exception(monkeypatch):
    from app.core.ai import events
    from app.core.errors import ErrorCategory

    recorder = _Recorder()
    monkeypatch.setattr(events, "logger", recorder)

    class _Broken:
        def log_action(self, **_kwargs):
            raise _LeakyFailure()

    monkeypatch.setattr("db.database.get_db", lambda: _Broken())
    events.record_provider_failure(
        provider="anthropic",
        category=ErrorCategory.PROVIDER_WORKSPACE_REQUIRED,
        correlation_id="00000000-0000-0000-0000-000000000000",
    )
    _assert_safe(recorder.rendered())


def test_a_failed_downgrade_write_does_not_render_the_exception(monkeypatch):
    from app.core import providers
    from app.core.errors import ErrorCategory

    recorder = _Recorder()
    monkeypatch.setattr(providers, "logger", recorder)

    def _explode(*_args, **_kwargs):
        raise _LeakyFailure()

    monkeypatch.setattr("app.core.preferences.get", _explode)
    providers.note_runtime_failure("anthropic", ErrorCategory.PROVIDER_AUTH, credential_revision=_current_revision())
    _assert_safe(recorder.rendered())


def test_an_unreadable_preferences_file_does_not_render_the_exception(monkeypatch):
    from app.core import preferences

    recorder = _Recorder()
    monkeypatch.setattr(preferences, "logger", recorder)

    class _Path:
        def exists(self):
            return True

        def read_text(self, **_kwargs):
            raise _LeakyFailure()

    monkeypatch.setattr(preferences, "_path_or_none", lambda: _Path())
    assert preferences.load() == {}
    _assert_safe(recorder.rendered())


def test_an_unresolvable_preferences_location_does_not_render_the_exception(monkeypatch):
    from app.core import preferences

    recorder = _Recorder()
    monkeypatch.setattr(preferences, "logger", recorder)

    def _explode():
        raise _LeakyFailure()

    monkeypatch.setattr(preferences, "preferences_path", _explode)
    assert preferences._path_or_none() is None
    _assert_safe(recorder.rendered())


def test_an_unwritable_preferences_file_does_not_render_the_exception(monkeypatch):
    from app.core import preferences

    recorder = _Recorder()
    monkeypatch.setattr(preferences, "logger", recorder)

    class _Parent:
        def mkdir(self, **_kwargs):
            raise _LeakyFailure()

    class _Path:
        parent = _Parent()

        def exists(self):
            return False

        def with_name(self, _name):
            return self

        @property
        def name(self):
            return "preferences.json"

        def unlink(self, **_kwargs):
            return None

    monkeypatch.setattr(preferences, "_path_or_none", lambda: _Path())
    assert preferences.store_many({"ai_provider": "ollama"}) is False
    _assert_safe(recorder.rendered())


#: Every module on the credential, provider and preferences failure paths.
#: `exc_info` renders the exception's value *and* its full traceback paths,
#: and each of these catches an exception raised by something outside this
#: codebase — a provider SDK, SQLite, the filesystem, a keyring backend.
LOG_SAFE_MODULES = (
    "app/core/errors.py",
    "app/core/brain.py",
    "app/core/ai/key_check.py",
    "app/core/ai/anthropic_provider.py",
    "app/api/chat.py",
    "app/core/ai/events.py",
    "app/core/ai/credential_pair.py",
    "app/core/providers.py",
    "app/core/preferences.py",
    "app/core/credentials.py",
)


@pytest.mark.parametrize("module", LOG_SAFE_MODULES)
def test_no_module_on_these_paths_hands_an_exception_to_the_logger(module):
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
        if target.attr == "exception":
            offenders.append(f"{module}:{node.lineno} logger.exception")
        for keyword in node.keywords:
            if keyword.arg == "exc_info":
                offenders.append(f"{module}:{node.lineno} exc_info")
    assert offenders == [], (
        f"an exception can be rendered into jarvis.log at {offenders}; "
        f"use app/core/safe_traceback.py::describe() instead"
    )


def test_describe_keeps_the_type_and_a_shortened_frame():
    from app.core.safe_traceback import describe

    try:
        raise _LeakyFailure()
    except _LeakyFailure as exc:
        rendered = describe(exc)

    assert "_LeakyFailure" in rendered
    assert "test_credential_replacement_safety.py:" in rendered
    for secret in (OLD_KEY, FAKE_WORKSPACE, FAKE_USERNAME, FAKE_PRIVATE_PATH):
        assert secret not in rendered


# ---------------------------------------------------------------------------
# Blocker 4 — a downgrade that was not written is not a downgrade
# ---------------------------------------------------------------------------

def test_a_downgrade_that_cannot_be_persisted_is_not_logged_as_persisted(monkeypatch):
    from unittest.mock import patch

    from app.core import providers
    from app.core.errors import ErrorCategory

    recorder = _Recorder()
    monkeypatch.setattr(providers, "logger", recorder)

    with patch("app.core.preferences.store_many", return_value=False):
        providers.note_runtime_failure("anthropic", ErrorCategory.PROVIDER_AUTH, credential_revision=_current_revision())

    rendered = recorder.rendered().lower()
    assert "downgraded to" not in rendered, (
        "a downgrade that was never written was logged as though it had been"
    )


def test_a_downgrade_that_was_persisted_is_logged(monkeypatch):
    from unittest.mock import patch

    from app.core import providers
    from app.core.errors import ErrorCategory

    recorder = _Recorder()
    monkeypatch.setattr(providers, "logger", recorder)

    with patch("app.core.preferences.store_many", return_value=True):
        providers.note_runtime_failure("anthropic", ErrorCategory.PROVIDER_AUTH, credential_revision=_current_revision())

    assert "downgraded to" in recorder.rendered().lower()


def test_a_downgrade_that_cannot_be_persisted_still_stops_claiming_availability():
    """The point of the downgrade is that the dashboard stops reporting a
    rejected credential as usable. A failed write must not leave it
    claiming exactly that for the rest of the session."""
    from unittest.mock import patch

    from app.core import providers
    from app.core.errors import ErrorCategory

    with patch("app.config.settings.anthropic_api_key", OLD_KEY), \
         patch("app.core.preferences.get", return_value=providers.CREDENTIAL_VERIFIED), \
         patch("app.core.preferences.store_many", return_value=False):
        assert providers.anthropic_status().available is True
        providers.note_runtime_failure("anthropic", ErrorCategory.PROVIDER_AUTH, credential_revision=_current_revision())
        assert providers.anthropic_credential_state() == providers.CREDENTIAL_FAILED
        assert providers.anthropic_status().available is False


def test_the_process_local_downgrade_can_never_report_a_credential_as_working():
    from app.core import providers

    for state in (providers.CREDENTIAL_VERIFIED, providers.CREDENTIAL_UNVERIFIED,
                  providers.CREDENTIAL_NOT_CONFIGURED, "anything-else"):
        providers.clear_runtime_downgrade()
        providers._remember_runtime_downgrade(state, _current_revision())
        assert providers.runtime_downgrade() is None, (
            f"{state!r} was accepted as a downgrade; only negative states may be"
        )


def test_the_process_local_downgrade_is_forgotten_when_the_credential_changes():
    from unittest.mock import patch

    from app.core import credentials, providers
    from app.core.errors import ErrorCategory

    applied = credentials.MutationResult(credentials.MUTATION_APPLIED)
    with patch("app.core.preferences.store_many", return_value=False):
        providers.note_runtime_failure("anthropic", ErrorCategory.PROVIDER_AUTH, credential_revision=_current_revision())
    assert providers.runtime_downgrade() == providers.CREDENTIAL_FAILED

    with patch("app.core.credentials.stored_api_key_snapshot", return_value=(True, OLD_KEY)), \
         patch("app.core.credentials.set_stored_api_key_detailed", return_value=applied), \
         patch("app.core.preferences.store_many", return_value=True):
        outcome = _pair().save(NEW_KEY, FAKE_WORKSPACE, providers.CREDENTIAL_VERIFIED)

    assert outcome.ok is True
    assert providers.runtime_downgrade() is None, (
        "a downgrade describing the previous credential outlived it"
    )


def test_the_process_local_downgrade_is_forgotten_when_the_credential_is_removed():
    from unittest.mock import patch

    from app.core import credentials, providers
    from app.core.errors import ErrorCategory

    applied = credentials.MutationResult(credentials.MUTATION_APPLIED)
    with patch("app.core.preferences.store_many", return_value=False):
        providers.note_runtime_failure("anthropic", ErrorCategory.PROVIDER_AUTH, credential_revision=_current_revision())
    assert providers.runtime_downgrade() == providers.CREDENTIAL_FAILED

    with patch("app.core.credentials.clear_stored_api_key_detailed", return_value=applied), \
         patch("app.core.preferences.store_many", return_value=True):
        assert _pair().clear().ok is True

    assert providers.runtime_downgrade() is None


def test_a_transient_runtime_failure_leaves_no_process_local_downgrade():
    from app.core import providers
    from app.core.errors import ErrorCategory

    for category in (ErrorCategory.PROVIDER_TIMEOUT, ErrorCategory.PROVIDER_RATE_LIMIT,
                     ErrorCategory.PROVIDER_UNAVAILABLE):
        providers.note_runtime_failure("anthropic", category, credential_revision=_current_revision())
        assert providers.runtime_downgrade() is None
