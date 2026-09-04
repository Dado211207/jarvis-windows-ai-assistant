"""A key and the Workspace ID that describes it must always belong to the
same request — including when two requests overlap and **both succeed**.

**The defect these tests were written against.** `credential_pair.save()`
is four steps against two stores: read a snapshot, write the credential,
write the metadata, and roll back if the metadata write failed. Nothing
held those four together. Two overlapping saves could therefore interleave
*between* them, and the round-5 correction made that reachable through a
path where both requests report success:

    older_result    applied
    newer_result    applied
    final_key       NEWER-KEY
    final_workspace OLDER-WORKSPACE
    targets         ['JARVIS']

The sequence, on the exact source it was found in:

  1. the older save writes OLDER-KEY and reaches the credential layer's
     success path, where it picks the survivor its cleanup should aim at;
  2. a newer save completes entirely — NEWER-KEY *and* NEWER-WORKSPACE;
  3. the older save resumes. `_cleanup_survivor()` correctly follows the
     newer generation, so the discard succeeds and the older credential
     mutation correctly returns `MUTATION_APPLIED`;
  4. the older `save()` takes that as permission to commit **its own**
     metadata, and writes OLDER-WORKSPACE over the newer request's.

Every individual step behaved as designed. The credential layer's
latest-intent-wins rule is not violated — the *key* is the newer one. What
was missing is that "this credential write landed" is not the same
permission as "this request may still commit its own description of the
credential", and there was nothing above the two stores to tell them apart.

The correction is a server-side coordinator: one credential-pair change at
a time, from the snapshot through to the returned `PairOutcome`. Disabling
a button cannot be the fix — two concurrent `POST /settings/api-key`
requests need no button at all — and the UI change that accompanies this is
labelled as defence in depth for that reason.

**What these tests assert.** Not `MutationResult`. The actual pair: the
value in the credential store, the Workspace ID, the verification state,
the plain/compound target invariant that must survive on real Windows, and
whether either response claimed a success that is not in the store.

Every wait is a `threading.Event` or a bounded join. There are no sleeps: a
timing-dependent test of a timing defect proves nothing.
"""

import threading

import pytest

from tests.test_credential_backend_targets import _WindowsLikeKeyring, _install
from tests.test_credential_replacement_safety import JOIN_TIMEOUT, settle

OLDER_KEY = "sk-ant-api03-OLDER-request-key"
NEWER_KEY = "sk-ant-api03-NEWER-request-key"
OLDER_WORKSPACE = "wrkspc_01OLDERrequestworkspaceid"
NEWER_WORKSPACE = "wrkspc_01NEWERrequestworkspaceid"
OLDER_STATE = "verification_failed"
NEWER_STATE = "verified"


@pytest.fixture(autouse=True)
def _clean_keyring_import():
    import sys

    sys.modules.pop("keyring", None)
    sys.modules.pop("keyring.errors", None)
    yield
    settle()
    sys.modules.pop("keyring", None)
    sys.modules.pop("keyring.errors", None)


# ---------------------------------------------------------------------------
# The second store, in memory
# ---------------------------------------------------------------------------

class _Preferences:
    """`preferences.store_many` / `get`, recording what is actually kept.

    The real one serialises the whole file and replaces it, so a failed
    write leaves the previous contents intact; this models the same
    all-or-nothing shape.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.values = {}
        self.writes = []
        #: Set to make every write fail, the way a settings file that
        #: cannot be written behaves. All-or-nothing, like the real one.
        self.refuse_writes = False

    def store_many(self, pairs):
        if self.refuse_writes:
            return False
        with self._lock:
            self.values.update(pairs)
            self.writes.append(dict(pairs))
        return True

    def get(self, key, default=""):
        with self._lock:
            return self.values.get(key, default)


def _metadata_keys():
    from app.core.ai.workspace import PREFERENCE_KEY as WORKSPACE_KEY
    from app.core.providers import VERIFICATION_PREFERENCE

    return WORKSPACE_KEY, VERIFICATION_PREFERENCE


# ---------------------------------------------------------------------------
# Pausing the older request exactly where the interleaving became possible
# ---------------------------------------------------------------------------

class _PausedAtCleanupSurvivor:
    """Holds the first `credentials._cleanup_survivor()` call until released.

    That call is on the credential layer's *success* path: the write has
    landed and the request is choosing what its tidy-up should aim at. It is
    the last moment before the older request is handed `MUTATION_APPLIED`
    and goes on to commit its own metadata, so pausing there puts the newer
    request precisely inside the window the defect occupied.
    """

    def __init__(self, monkeypatch, credentials):
        self.reached = threading.Event()
        self.release = threading.Event()
        self._held = False
        self._lock = threading.Lock()
        real = credentials._cleanup_survivor

        def hooked(username, generation, survivor):
            with self._lock:
                first = not self._held
                self._held = True
            if first:
                self.reached.set()
                assert self.release.wait(JOIN_TIMEOUT), (
                    "the test never released the paused credential operation"
                )
            return real(username, generation, survivor)

        monkeypatch.setattr(credentials, "_cleanup_survivor", hooked)

    def wait_until_reached(self):
        assert self.reached.wait(JOIN_TIMEOUT), (
            "the older request never reached the credential success path"
        )

    def let_it_finish(self):
        self.release.set()


class _InAnotherThread:
    def __init__(self, call, name):
        self.result = None
        self.error = None
        self.started = threading.Event()
        self.finished = threading.Event()

        def run():
            self.started.set()
            try:
                self.result = call()
            except BaseException as exc:  # noqa: BLE001 — re-raised in join()
                self.error = exc
            finally:
                self.finished.set()

        self._thread = threading.Thread(target=run, name=name)

    def start(self):
        self._thread.start()
        assert self.started.wait(JOIN_TIMEOUT), f"{self._thread.name} never started"
        return self

    def join(self):
        self._thread.join(timeout=JOIN_TIMEOUT)
        assert not self._thread.is_alive(), f"{self._thread.name} never returned"
        if self.error is not None:
            raise self.error
        return self.result


def _coordinator():
    """The correction, or None on the source this file was written against."""
    try:
        from app.core.ai import credential_transaction
    except ImportError:
        return None
    return credential_transaction


def _wait_until_the_newer_request_can_do_no_more(newer):
    """Deterministically reach the moment the older request may be released.

    The two worlds differ, and saying so plainly is the point of this
    helper. **Before the correction** nothing holds the two stores together,
    so the newer request simply runs to completion inside the older one's
    window — waiting for it to finish is both possible and exactly the
    defect. **After the correction** it cannot make any progress at all: it
    is parked in the coordinator behind the request in front of it, so what
    is waited for is that it has been parked.

    Neither branch sleeps, and neither guesses.
    """
    coordinator = _coordinator()
    if coordinator is None:
        assert newer.finished.wait(JOIN_TIMEOUT), (
            "the newer request did not finish; this test's premise is that, "
            "without a coordinator, it runs right through the older one's window"
        )
        return
    assert coordinator.wait_for_waiters(1, JOIN_TIMEOUT), (
        "the newer request was never parked behind the older one"
    )


# ---------------------------------------------------------------------------
# What the store actually ends up holding
# ---------------------------------------------------------------------------

def _final_pair(credentials, fake, preferences):
    workspace_key, state_key = _metadata_keys()
    settle()
    return {
        "key": fake.value_at(credentials.SERVICE_NAME),
        "workspace": preferences.get(workspace_key, ""),
        "state": preferences.get(state_key, ""),
        "targets": fake.target_names(),
    }


def _wired(monkeypatch, preferences):
    from app.core import preferences as preferences_module

    monkeypatch.setattr(preferences_module, "store_many", preferences.store_many)
    monkeypatch.setattr(preferences_module, "get", preferences.get)


def _assert_one_target(credentials, pair):
    assert pair["targets"] == [credentials.SERVICE_NAME], (
        f"one logical secret must occupy one target; found {pair['targets']}"
    )


def _assert_no_target(pair):
    assert pair["targets"] == [], (
        f"a removed credential must leave no target; found {pair['targets']}"
    )


# ---------------------------------------------------------------------------
# Ordering 1 — two successful saves
# ---------------------------------------------------------------------------

def test_two_successful_saves_leave_a_key_and_a_workspace_from_the_same_request(monkeypatch):
    """The reported reproduction. Both requests succeed; the pair that
    survives must be one request's pair, not one of each."""
    from app.core import credentials
    from app.core.ai import credential_pair

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    paused = _PausedAtCleanupSurvivor(monkeypatch, credentials)

    older = _InAnotherThread(
        lambda: credential_pair.save(OLDER_KEY, OLDER_WORKSPACE, OLDER_STATE),
        "older-save",
    ).start()
    paused.wait_until_reached()

    newer = _InAnotherThread(
        lambda: credential_pair.save(NEWER_KEY, NEWER_WORKSPACE, NEWER_STATE),
        "newer-save",
    ).start()
    _wait_until_the_newer_request_can_do_no_more(newer)

    paused.let_it_finish()
    older_outcome = older.join()
    newer_outcome = newer.join()

    pair = _final_pair(credentials, fake, preferences)

    assert pair["key"] == NEWER_KEY, "the newer key did not survive"
    assert pair["workspace"] == NEWER_WORKSPACE, (
        "the stored key and the stored Workspace ID belong to different requests"
    )
    assert pair["state"] == NEWER_STATE, (
        "the stored key and its verification state belong to different requests"
    )
    _assert_one_target(credentials, pair)
    assert not fake.holds_value_anywhere(OLDER_KEY)

    # Neither response may claim a success the store does not support.
    for name, outcome in (("older", older_outcome), ("newer", newer_outcome)):
        if outcome.ok:
            assert outcome.consistent, f"the {name} request reported success and inconsistency"
    assert newer_outcome.ok, "the newer request must succeed"


def test_the_older_save_never_commits_its_workspace_after_the_newer_one(monkeypatch):
    """The invariant behind ordering 1, asserted on the writes themselves.

    A metadata write carrying the older request's Workspace ID must not
    appear after one carrying the newer request's. Checking the final value
    alone would pass a build that wrote them in the wrong order and then
    happened to be corrected by something else.
    """
    from app.core import credentials
    from app.core.ai import credential_pair

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    paused = _PausedAtCleanupSurvivor(monkeypatch, credentials)
    workspace_key, _state_key = _metadata_keys()

    older = _InAnotherThread(
        lambda: credential_pair.save(OLDER_KEY, OLDER_WORKSPACE, OLDER_STATE),
        "older-save",
    ).start()
    paused.wait_until_reached()
    newer = _InAnotherThread(
        lambda: credential_pair.save(NEWER_KEY, NEWER_WORKSPACE, NEWER_STATE),
        "newer-save",
    ).start()
    _wait_until_the_newer_request_can_do_no_more(newer)
    paused.let_it_finish()
    older.join()
    newer.join()
    settle()

    written = [write.get(workspace_key) for write in preferences.writes]
    assert NEWER_WORKSPACE in written, "the newer request never wrote its Workspace ID"
    assert written.index(NEWER_WORKSPACE) == len(written) - 1 or (
        OLDER_WORKSPACE not in written[written.index(NEWER_WORKSPACE):]
    ), (
        f"an older request wrote its Workspace ID after a newer one committed: {written}"
    )


# ---------------------------------------------------------------------------
# Ordering 2 — a successful save overtaken by a successful remove
# ---------------------------------------------------------------------------

def test_a_successful_remove_after_a_successful_save_leaves_nothing_behind(monkeypatch):
    """Remove is the request that finished last, so the credential must be
    gone *and* the metadata that described it must be cleared. A Workspace
    ID left behind is inherited by the next key entered."""
    from app.core import credentials
    from app.core.ai import credential_pair

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    paused = _PausedAtCleanupSurvivor(monkeypatch, credentials)

    older = _InAnotherThread(
        lambda: credential_pair.save(OLDER_KEY, OLDER_WORKSPACE, OLDER_STATE),
        "older-save",
    ).start()
    paused.wait_until_reached()
    newer = _InAnotherThread(credential_pair.clear, "newer-remove").start()
    _wait_until_the_newer_request_can_do_no_more(newer)
    paused.let_it_finish()
    older.join()
    newer_outcome = newer.join()

    pair = _final_pair(credentials, fake, preferences)

    assert pair["key"] is None, "a removed credential is still in the store"
    assert pair["workspace"] == "", (
        "the removed key's Workspace ID was left behind for the next key to inherit"
    )
    assert pair["state"] == "", "the removed key's verification state was left behind"
    _assert_no_target(pair)
    assert not fake.holds_value_anywhere(OLDER_KEY)
    assert newer_outcome.ok, "the newer removal must succeed"


# ---------------------------------------------------------------------------
# Ordering 3 — a successful save overtaking a successful remove
# ---------------------------------------------------------------------------

def test_a_successful_save_after_a_successful_remove_keeps_its_own_metadata(monkeypatch):
    """The mirror image, and the one where a stale metadata clear would
    leave a stored key with no Workspace ID at all."""
    from app.core import credentials
    from app.core.ai import credential_pair

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLDER_KEY)
    preferences = _Preferences()
    preferences.store_many(dict(zip(_metadata_keys(), (OLDER_WORKSPACE, OLDER_STATE))))
    _wired(monkeypatch, preferences)
    paused = _PausedAtCleanupSurvivor(monkeypatch, credentials)

    older = _InAnotherThread(credential_pair.clear, "older-remove").start()
    paused.wait_until_reached()
    newer = _InAnotherThread(
        lambda: credential_pair.save(NEWER_KEY, NEWER_WORKSPACE, NEWER_STATE),
        "newer-save",
    ).start()
    _wait_until_the_newer_request_can_do_no_more(newer)
    paused.let_it_finish()
    older.join()
    newer_outcome = newer.join()

    pair = _final_pair(credentials, fake, preferences)

    assert pair["key"] == NEWER_KEY, "the newer key did not survive the older removal"
    assert pair["workspace"] == NEWER_WORKSPACE, (
        "a stored key was left describing nothing, or describing the removed key"
    )
    assert pair["state"] == NEWER_STATE
    _assert_one_target(credentials, pair)
    assert not fake.holds_value_anywhere(OLDER_KEY)
    assert newer_outcome.ok, "the newer save must succeed"


# ---------------------------------------------------------------------------
# The coordinator's own contract
# ---------------------------------------------------------------------------

def test_the_whole_pair_operation_runs_inside_one_transaction():
    """Structural, because the behavioural failure needs a race and the
    rule is simple: neither entry point may touch a store outside the
    coordinator. A future edit that moves the snapshot, a store write or
    the runtime-downgrade update outside `begin()` fails here immediately,
    on every platform, without a thread."""
    import ast
    import pathlib

    from app.core.ai import credential_pair

    tree = ast.parse(pathlib.Path(credential_pair.__file__).read_text(encoding="utf-8"))
    entry_points = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in ("save", "clear")
    }
    assert set(entry_points) == {"save", "clear"}

    for name, node in entry_points.items():
        calls = {
            call.func.attr for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        }
        names = {
            call.func.id for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        assert "begin" in calls or "begin" in names, (
            f"{name}() does not open a credential transaction"
        )
        # The stores are reached only from the locked body, never from the
        # entry point that owns the transaction.
        for forbidden in ("stored_api_key_snapshot", "set_stored_api_key_detailed",
                          "clear_stored_api_key_detailed", "_write_metadata",
                          "_forget_runtime_downgrade"):
            assert forbidden not in calls and forbidden not in names, (
                f"{name}() touches {forbidden} outside the transaction body"
            )


def test_a_second_change_that_cannot_be_started_is_refused_rather_than_queued_forever():
    """The wait is bounded, and running out of it is reported as what it is:
    nothing was attempted, so nothing was changed."""
    from app.core.ai import credential_transaction

    assert credential_transaction.WAIT_SECONDS > 0
    holder_ready = threading.Event()
    release = threading.Event()

    def hold():
        with credential_transaction.begin("test-holder"):
            holder_ready.set()
            assert release.wait(JOIN_TIMEOUT)

    holder = threading.Thread(target=hold, name="transaction-holder")
    holder.start()
    try:
        assert holder_ready.wait(JOIN_TIMEOUT)
        with pytest.raises(credential_transaction.TransactionBusy):
            with credential_transaction.begin("test-second", wait=0.0):
                pass
    finally:
        release.set()
        holder.join(timeout=JOIN_TIMEOUT)
        assert not holder.is_alive()


def test_a_busy_save_changes_nothing_and_says_so(monkeypatch):
    """The user-visible half of the bound above."""
    from app.core.ai import credential_pair, credential_transaction

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    monkeypatch.setattr(credential_transaction, "WAIT_SECONDS", 0.0)

    holder_ready = threading.Event()
    release = threading.Event()

    def hold():
        with credential_transaction.begin("test-holder"):
            holder_ready.set()
            assert release.wait(JOIN_TIMEOUT)

    holder = threading.Thread(target=hold, name="transaction-holder")
    holder.start()
    try:
        assert holder_ready.wait(JOIN_TIMEOUT)
        outcome = credential_pair.save(NEWER_KEY, NEWER_WORKSPACE, NEWER_STATE)
        removal = credential_pair.clear()
    finally:
        release.set()
        holder.join(timeout=JOIN_TIMEOUT)
        assert not holder.is_alive()

    for outcome_under_test in (outcome, removal):
        assert not outcome_under_test.ok
        # Round 7 corrected what this may claim. `stored` and `consistent`
        # describe the *installation*, and a request that never started
        # observed neither store — least of all in the case this outcome
        # actually occurs in, where the transaction in front may be
        # part-way between the credential and its metadata. The real
        # interleaving is covered by
        # tests/test_credential_request_ordering.py; here the transaction
        # in front is empty, which is exactly why the old hard-coded
        # answers looked true.
        assert outcome_under_test.stored is None
        assert outcome_under_test.consistent is None
        lowered = outcome_under_test.message.lower()
        assert "did not" in lowered
        assert "already running" in lowered or "in progress" in lowered

    assert fake.target_names() == [], "a refused request wrote to the credential store"
    assert preferences.writes == [], "a refused request wrote metadata"


def test_the_coordinator_never_lets_an_older_transaction_outlive_a_newer_one():
    """The tripwire behind requirement 5, stated as a fact about ids.

    Under the coordinator an older transaction cannot still be running once
    a newer one has committed, because the newer one could not have started.
    This asserts that directly, so a future change that widens the lock's
    scope — or removes it — is caught by something other than a race.
    """
    from app.core.ai import credential_transaction

    seen = []

    def record():
        with credential_transaction.begin("test") as transaction:
            seen.append((transaction.id, credential_transaction.committed_generation()))
            transaction.commit()

    threads = [threading.Thread(target=record, name=f"txn-{index}") for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
        assert not thread.is_alive()

    ids = [entry[0] for entry in seen]
    assert len(set(ids)) == len(ids), "two transactions shared an id"
    for transaction_id, committed_before in seen:
        assert committed_before < transaction_id, (
            "a transaction started while a newer one had already committed"
        )
