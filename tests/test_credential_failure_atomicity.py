"""Round 8 — a delayed rejection must not slip between the revision check
and the write it authorises.

Round 7 gave every request the non-secret `revision` of the credential pair
it was built with, and made `providers.note_runtime_failure()` discard a
rejection whose revision is no longer current. That fixed *attribution*. It
did not make the decision and the write one indivisible act:

    if credential_revision != credential_view.current_revision():   # read
        return
    _remember_runtime_downgrade(...)                               # write
    store_preferences({VERIFICATION_PREFERENCE: downgraded})       # write

Nothing holds the coordinator between the read and the writes. A Save can
complete in that window, and the comparison — already made, against a value
captured before the Save — still passes. The rejection of the *previous*
key then writes `verification_failed` over the credential that replaced it,
which is the exact damage round 7 set out to prevent, one layer down:

    new_save_outcome              applied
    revision_after_new_save       1
    state_before_old_failure      verified
    state_after_old_failure       verification_failed
    runtime_note_for_new_revision None

The process-local note stays correctly revision-scoped — that part of round
7 holds — but the *persisted* preference is corrupted, and the next
`credential_view.current()` reads it back and hands it to the new revision.

**Why round 7's test did not catch it.** It performs the whole Save and
*then* calls `note_runtime_failure()`, so the very first comparison sees the
new revision and rejects the stale failure immediately. The window is never
entered. Testing the ordering requires pausing inside the check itself.

Every wait below is an `Event` or a bounded join. There is no sleep.
"""

import threading

import pytest

from tests.test_credential_backend_targets import _WindowsLikeKeyring, _install
from tests.test_credential_pair_transaction import (
    _InAnotherThread,
    _Preferences,
    _metadata_keys,
    _wired,
)
from tests.test_credential_replacement_safety import JOIN_TIMEOUT, settle

OLD_KEY = "sk-ant-api03-OLD-key-in-flight"
NEW_KEY = "sk-ant-api03-NEW-key-just-saved"
OLD_WORKSPACE = "wrkspc_01OLDworkspaceidvalue"
NEW_WORKSPACE = "wrkspc_01NEWworkspaceidvalue"


@pytest.fixture(autouse=True)
def _clean_keyring_import():
    import sys

    sys.modules.pop("keyring", None)
    sys.modules.pop("keyring.errors", None)
    yield
    settle()
    sys.modules.pop("keyring", None)
    sys.modules.pop("keyring.errors", None)


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    """These tests are about the credential *store*; the environment
    override would otherwise answer every read."""
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)


def _seed(fake, preferences, credentials, key=OLD_KEY, workspace=OLD_WORKSPACE):
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, key)
    workspace_key, state_key = _metadata_keys()
    preferences.store_many({workspace_key: workspace, state_key: "verified"})
    preferences.writes.clear()


def _state():
    from app.core.providers import anthropic_credential_state

    return anthropic_credential_state()


def _stored_state():
    """The *persisted* verification preference, ignoring any session note."""
    from app.core.preferences import get as get_preference
    from app.core.providers import VERIFICATION_PREFERENCE

    return (get_preference(VERIFICATION_PREFERENCE) or "").strip()


class _PausedInsideTheRevisionCheck:
    """Holds `note_runtime_failure` after it has read the revision.

    The captured value is returned *after* the test releases it, which is
    the whole point: the decision is made on a number that was true when it
    was read and is not true when it is used.
    """

    def __init__(self, monkeypatch, credential_view):
        self.reached = threading.Event()
        self.release = threading.Event()
        self.captured = None
        self._done = False
        self._lock = threading.Lock()
        real = credential_view.current_revision

        def hooked():
            value = real()
            with self._lock:
                first = not self._done
                self._done = True
            if not first:
                return value
            self.captured = value
            self.reached.set()
            assert self.release.wait(JOIN_TIMEOUT), "the test never released the paused check"
            return value

        monkeypatch.setattr(credential_view, "current_revision", hooked)

    def wait_until_reached(self):
        assert self.reached.wait(JOIN_TIMEOUT), "the failure path never read the revision"

    def let_it_finish(self):
        self.release.set()


def _fail_with(revision, category=None):
    from app.core.errors import ErrorCategory
    from app.core.providers import note_runtime_failure

    note_runtime_failure(
        "anthropic", category or ErrorCategory.PROVIDER_AUTH, credential_revision=revision,
    )


# ---------------------------------------------------------------------------
# The blocker — check-then-write across a completed Save
# ---------------------------------------------------------------------------

def test_an_old_failure_paused_at_the_revision_check_cannot_downgrade_a_new_save(monkeypatch):
    """The reported TOCTOU, in the order it was reported."""
    from app.core.ai import credential_pair, credential_view

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    _seed(fake, preferences, __import__("app.core.credentials", fromlist=["x"]))
    credential_view.invalidate()

    in_flight = credential_view.current().revision

    paused = _PausedInsideTheRevisionCheck(monkeypatch, credential_view)
    failing = _InAnotherThread(lambda: _fail_with(in_flight), "old-request").start()
    paused.wait_until_reached()

    # The user replaces the credential while the old rejection is parked
    # between reading the revision and acting on it.
    assert credential_pair.save(NEW_KEY, NEW_WORKSPACE, "verified").ok
    settle()
    assert _state() == "verified"

    paused.let_it_finish()
    failing.join()
    settle()

    assert _stored_state() == "verified", (
        "a rejection of the previous key wrote verification_failed over the "
        "credential that replaced it: the revision was checked before the save "
        "and acted on afterwards"
    )
    assert _state() == "verified", (
        "the reported state of the newly saved credential was downgraded by an "
        "older request's rejection"
    )


class _PausedInsideTheDowngrade:
    """Holds the failure path *after* it has validated the revision and
    committed to writing, which is the window the blocker is about.

    Hooking `_remember_runtime_downgrade` is the honest place: the fix
    reaches it only once validation has passed under the coordinator, so
    reaching it proves the decision has been made, and pausing there holds
    the window open for as long as the test needs.
    """

    def __init__(self, monkeypatch, providers):
        self.reached = threading.Event()
        self.release = threading.Event()
        self._done = False
        self._lock = threading.Lock()
        real = providers._remember_runtime_downgrade

        def hooked(state, revision):
            with self._lock:
                first = not self._done
                self._done = True
            if first:
                self.reached.set()
                assert self.release.wait(JOIN_TIMEOUT), "the test never released the downgrade"
            return real(state, revision)

        monkeypatch.setattr(providers, "_remember_runtime_downgrade", hooked)

    def wait_until_reached(self):
        assert self.reached.wait(JOIN_TIMEOUT), "the failure path never reached its write"

    def let_it_finish(self):
        self.release.set()


def _cannot_start_now(operation, credential_transaction, monkeypatch):
    """Run *operation* with no willingness to queue, and report whether the
    coordinator let it start at all."""
    monkeypatch.setattr(credential_transaction, "WAIT_SECONDS", 0.0)
    return operation()


def test_a_save_cannot_complete_between_revision_validation_and_persistence(monkeypatch):
    """Once the revision has been validated, the downgrade owns the pair
    until it is written. A Save may not slip in and commit in that window —
    if it could, the rejection it was validated against would no longer be
    the stored credential by the time it landed."""
    from app.core.ai import credential_pair, credential_transaction, credential_view
    from app.core import providers

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    _seed(fake, preferences, __import__("app.core.credentials", fromlist=["x"]))
    credential_view.invalidate()

    in_flight = credential_view.current().revision
    paused = _PausedInsideTheDowngrade(monkeypatch, providers)
    failing = _InAnotherThread(lambda: _fail_with(in_flight), "old-request").start()
    paused.wait_until_reached()

    outcome = _cannot_start_now(
        lambda: credential_pair.save(NEW_KEY, NEW_WORKSPACE, "verified"),
        credential_transaction, monkeypatch,
    )
    assert not outcome.ok, (
        "a Save committed while a validated downgrade was part-way through "
        "writing; the revision it was checked against is no longer the stored one"
    )

    paused.let_it_finish()
    failing.join()
    settle()
    assert _stored_state() == "verification_failed"


def test_a_remove_cannot_complete_between_revision_validation_and_persistence(monkeypatch):
    """Remove changes the same pair, so it must be excluded from the same
    window for the same reason."""
    from app.core.ai import credential_pair, credential_transaction, credential_view
    from app.core import providers

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    _seed(fake, preferences, __import__("app.core.credentials", fromlist=["x"]))
    credential_view.invalidate()

    in_flight = credential_view.current().revision
    paused = _PausedInsideTheDowngrade(monkeypatch, providers)
    failing = _InAnotherThread(lambda: _fail_with(in_flight), "old-request").start()
    paused.wait_until_reached()

    outcome = _cannot_start_now(credential_pair.clear, credential_transaction, monkeypatch)
    assert not outcome.ok, (
        "a Remove committed while a validated downgrade was part-way through writing"
    )

    paused.let_it_finish()
    failing.join()
    settle()


# ---------------------------------------------------------------------------
# The current revision must still be downgradable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "category_name,expected",
    [
        ("PROVIDER_AUTH", "verification_failed"),
        ("PROVIDER_WORKSPACE_REQUIRED", "verification_failed"),
        ("PROVIDER_BILLING", "account_unfunded"),
    ],
)
def test_a_rejection_of_the_current_revision_still_downgrades(monkeypatch, category_name, expected):
    """The correction must not make every live rejection look stale.

    This is the failure mode of getting item 6 wrong: if taking the
    coordinator advanced the revision, the value the request carries would
    never match by the time it was compared.
    """
    from app.core.errors import ErrorCategory
    from app.core.ai import credential_view

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    _seed(fake, preferences, __import__("app.core.credentials", fromlist=["x"]))
    credential_view.invalidate()

    pair = credential_view.current()
    _fail_with(pair.revision, getattr(ErrorCategory, category_name))
    settle()

    assert _state() == expected
    assert _stored_state() == expected


def test_two_rejections_of_the_same_credential_are_both_honoured(monkeypatch):
    """A downgrade describes the same pair, so it must not advance the
    revision — otherwise the second rejection of one key looks stale."""
    from app.core.errors import ErrorCategory
    from app.core.ai import credential_view

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    _seed(fake, preferences, __import__("app.core.credentials", fromlist=["x"]))
    credential_view.invalidate()

    pair = credential_view.current()
    _fail_with(pair.revision, ErrorCategory.PROVIDER_AUTH)
    settle()
    assert credential_view.current().revision == pair.revision, (
        "recording a verification-state change moved the credential-identity "
        "revision; the next rejection of this same key would look stale"
    )

    _fail_with(pair.revision, ErrorCategory.PROVIDER_BILLING)
    settle()
    assert _state() == "account_unfunded", (
        "the second rejection of the same credential was discarded as stale"
    )


def test_a_failed_preference_write_leaves_a_session_note_for_the_right_revision(monkeypatch):
    """When the file cannot be written the observation still happened — but
    it belongs to the revision that was rejected and to no other."""
    from app.core.errors import ErrorCategory
    from app.core.ai import credential_pair, credential_view
    from app.core import providers

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    _seed(fake, preferences, __import__("app.core.credentials", fromlist=["x"]))
    credential_view.invalidate()

    pair = credential_view.current()
    monkeypatch.setattr(preferences, "fail_writes", True, raising=False)
    _fail_with(pair.revision, ErrorCategory.PROVIDER_AUTH)
    settle()

    assert providers.runtime_downgrade(pair.revision) == "verification_failed"
    monkeypatch.setattr(preferences, "fail_writes", False, raising=False)

    assert credential_pair.save(NEW_KEY, NEW_WORKSPACE, "verified").ok
    settle()
    after = credential_view.current()
    assert after.revision != pair.revision
    assert providers.runtime_downgrade(after.revision) is None, (
        "a session-only downgrade recorded against the previous credential "
        "described the one that replaced it"
    )
    assert providers.credential_state_for(after) == "verified"


# ---------------------------------------------------------------------------
# Readers, and the values that must never travel
# ---------------------------------------------------------------------------

def test_a_status_reader_sees_the_previous_state_or_the_completed_downgrade(monkeypatch):
    """Never a half-applied one: the runtime note and the preference are
    written together, under the same gate."""
    from app.core.errors import ErrorCategory
    from app.core.ai import credential_view

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    _seed(fake, preferences, __import__("app.core.credentials", fromlist=["x"]))
    credential_view.invalidate()

    pair = credential_view.current()
    seen = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            seen.append(_state())

    watcher = threading.Thread(target=watch, name="status-reader", daemon=True)
    watcher.start()
    _fail_with(pair.revision, ErrorCategory.PROVIDER_AUTH)
    settle()
    stop.set()
    watcher.join(timeout=JOIN_TIMEOUT)

    assert set(seen) <= {"verified", "verification_failed"}, (
        f"a status reader saw a state that was never complete: {sorted(set(seen))}"
    )


def test_neither_production_failure_path_loses_its_revision():
    """Both call sites must pass the revision the request was built with."""
    import ast
    import pathlib

    for module in ("app/core/brain.py", "app/api/chat.py"):
        tree = ast.parse(pathlib.Path(module).read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", "")) == "note_runtime_failure"
        ]
        assert calls, f"{module} no longer reports provider failures at all"
        for call in calls:
            assert any(kw.arg == "credential_revision" for kw in call.keywords), (
                f"{module} calls note_runtime_failure without the credential revision "
                "that made the request — a delayed failure would then downgrade "
                "whatever is stored when it lands"
            )


def test_no_credential_value_reaches_a_log_a_response_or_the_revision(monkeypatch, caplog):
    """The revision is a counter. Nothing derived from a secret may identify
    a credential, and no value may be logged on this path."""
    import logging

    from app.core.errors import ErrorCategory
    from app.core.ai import credential_view

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    _seed(fake, preferences, __import__("app.core.credentials", fromlist=["x"]))
    credential_view.invalidate()

    pair = credential_view.current()
    assert isinstance(pair.revision, int)

    with caplog.at_level(logging.DEBUG):
        _fail_with(pair.revision, ErrorCategory.PROVIDER_AUTH)
        _fail_with(pair.revision + 999, ErrorCategory.PROVIDER_AUTH)
        settle()

    written = caplog.text
    for secret in (OLD_KEY, NEW_KEY, OLD_WORKSPACE, NEW_WORKSPACE):
        assert secret not in written, "a credential value reached the log"
    assert repr(pair).find(OLD_KEY) == -1, "the snapshot's repr rendered the key"


# ---------------------------------------------------------------------------
# The audit item — `readable` must be established, not assumed
# ---------------------------------------------------------------------------

def test_a_readable_store_with_no_key_is_not_reported_as_unreadable(monkeypatch):
    from app.core.ai import credential_view

    _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    credential_view.invalidate()

    pair = credential_view.current()
    assert pair.readable is True, "an empty but working credential store read as unreadable"
    assert pair.configured is False


def test_an_unreadable_store_is_not_reported_as_no_credential(monkeypatch):
    """The distinction the field has always promised: one is a fact about
    the credential, the other a fact about this machine."""
    from app.core.ai import credential_view
    from app.core import credentials

    _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    credential_view.invalidate()

    monkeypatch.setattr(credentials, "stored_api_key_snapshot", lambda: (False, ""))
    pair = credential_view.current()

    assert pair.readable is False, (
        "a credential store that could not be read was reported as readable, which "
        "makes 'no key is configured' indistinguishable from 'this machine could "
        "not answer'"
    )
    assert pair.configured is False


def test_the_environment_variable_still_wins_and_needs_no_store(monkeypatch):
    """`ANTHROPIC_API_KEY` is what development and CI use; it must never be
    shadowed by a credential store, nor be marked unreadable because one
    was not consulted."""
    from app.config import settings
    from app.core.ai import credential_view
    from app.core import credentials

    _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    credential_view.invalidate()

    def _must_not_be_called():
        raise AssertionError("the credential store was consulted despite an env key")

    monkeypatch.setattr(settings, "anthropic_api_key", NEW_KEY, raising=False)
    monkeypatch.setattr(
        credentials, "stored_api_key_snapshot", lambda: _must_not_be_called(),
    )
    pair = credential_view.current()

    assert pair.api_key == NEW_KEY
    assert pair.configured is True
    assert pair.readable is True
