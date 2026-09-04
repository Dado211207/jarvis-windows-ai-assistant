"""When two requests touch the same credential, the newest one wins — and
an older one that *failed* must never be the thing that decides.

**The defect these tests were written against.** `_mutate_detailed()`'s
failure path asks `_record_desired_if_latest()` whether its rollback value
is still the newest intent. That function exists for exactly this reason and
returns `None` to mean "no, something newer has already been asked for". The
answer was then thrown away, and the very next statement called
`_queue_reconciliation()`, which called `_record_desired()` — creating a
*brand-new newest generation* holding the older request's rollback value and
submitting a worker to write it.

So the protection ran, correctly said no, and was then bypassed one line
later. A save that had already answered the user "saved" was overwritten by
the value a different, older, failed save had decided to roll back to:

    newer_result       applied
    older_result       uncertain
    final_plain_value  OLD
    targets            ['JARVIS']

Three orderings of the same shape are covered below, because the same
statement produces three different kinds of damage:

  * an older failed **save** overwrites a newer successful **save**;
  * an older failed **save** resurrects a credential a newer **remove**
    deleted — the user pressed Remove, was told it was gone, and it came
    back;
  * an older failed **remove** deletes a credential a newer **save** stored.

**How the race is made deterministic.** Not by sleeping and hoping. The
older request is stopped exactly where the defect lives — on entry to
`_record_desired_if_latest()` — by a wrapper that sets one `Event` and waits
on another. The newer request then runs to completion on the main thread,
observed through its own return value rather than through timing. Only then
is the older request released. Every wait in this file is an `Event` or a
thread join with a bounded timeout, so a regression fails the run instead of
hanging it, and the interleaving is identical on every machine.

The fake backend is `_WindowsLikeKeyring` from
tests/test_credential_backend_targets.py — keyed by *target name*, so every
assertion here can also check the invariant that matters on the real
Windows Credential Manager: one logical secret, one target, and no copy of a
superseded secret left behind.
"""

import threading

import pytest

from tests.test_credential_backend_targets import (
    OTHER_KEY,
    _WindowsLikeKeyring,
    _install,
)
from tests.test_credential_replacement_safety import (
    JOIN_TIMEOUT,
    NEW_KEY,
    OLD_KEY,
    settle,
)

#: The value the *older*, doomed request tries to store. Deliberately not
#: OLD_KEY or NEW_KEY: if it turns up in the store, the assertion message
#: should say which request put it there.
OLDER_REQUEST = "sk-ant-api03-OLDER-request-that-failed-midway"
#: What the *newer* request asks for, and therefore what must survive.
NEWER_REQUEST = "sk-ant-api03-NEWER-request-that-must-win"


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
# Stopping the older request exactly where the defect is
# ---------------------------------------------------------------------------

class _PausedAtLatestCheck:
    """Holds the first `_record_desired_if_latest()` call until released.

    That call is the decision point: before it, the older request has
    already failed inside the backend; after it, the older request knows a
    newer intent exists. Pausing *on entry* means the newer request runs
    entirely within the window the defect occupied, with no timing
    assumptions at all.

    Only the first call is held. The newer request never reaches this
    function (a successful write does not roll anything back), and the
    older request's own later calls, if any, must not deadlock the test.
    """

    def __init__(self, monkeypatch, credentials):
        self.reached = threading.Event()
        self.release = threading.Event()
        self._held = False
        self._lock = threading.Lock()
        real = credentials._record_desired_if_latest

        def hooked(username, generation, value):
            with self._lock:
                first = not self._held
                self._held = True
            if first:
                self.reached.set()
                assert self.release.wait(JOIN_TIMEOUT), (
                    "the test never released the paused failure handler"
                )
            return real(username, generation, value)

        monkeypatch.setattr(credentials, "_record_desired_if_latest", hooked)

    def wait_until_reached(self):
        assert self.reached.wait(JOIN_TIMEOUT), (
            "the older request never reached its failure handler"
        )

    def let_it_finish(self):
        self.release.set()


class _InAnotherThread:
    """Runs one credential call off the main thread and keeps its result."""

    def __init__(self, call):
        self.result = None
        self.error = None

        def run():
            try:
                self.result = call()
            except BaseException as exc:  # noqa: BLE001 — re-raised in join()
                self.error = exc

        self._thread = threading.Thread(target=run, name="older-credential-request")

    def start(self):
        self._thread.start()
        return self

    def join(self):
        self._thread.join(timeout=JOIN_TIMEOUT)
        assert not self._thread.is_alive(), "the older credential request never returned"
        if self.error is not None:
            raise self.error
        return self.result


def _targets_after_settling(credentials, fake):
    """Every credential target the fake still holds, once nothing is running."""
    settle()
    return fake.target_names()


# ---------------------------------------------------------------------------
# Ordering 1 — an older failed save must not overwrite a newer saved key
# ---------------------------------------------------------------------------

def test_a_stale_failed_save_does_not_overwrite_a_newer_successful_save(monkeypatch):
    """The reported reproduction, exactly.

    The older save mutates the backend and then raises — the shape the
    pinned Windows backend really has, since `set_password` performs two
    writes. While its failure handler is paused, a second save stores a new
    key and reports `MUTATION_APPLIED`. Releasing the first must not undo
    that: the user was told the newer key was saved.
    """
    from app.core import credentials

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake._raise_after_set = 1

    paused = _PausedAtLatestCheck(monkeypatch, credentials)
    older = _InAnotherThread(
        lambda: credentials.set_stored_api_key_detailed(OLDER_REQUEST),
    ).start()
    paused.wait_until_reached()

    newer = credentials.set_stored_api_key_detailed(NEWER_REQUEST)
    assert newer.outcome == credentials.MUTATION_APPLIED, (
        "the newer save did not succeed, so this test is not testing what it claims"
    )

    paused.let_it_finish()
    older_result = older.join()

    targets = _targets_after_settling(credentials, fake)
    assert fake.value_at(credentials.SERVICE_NAME) == NEWER_REQUEST, (
        "an older failed save overwrote a newer save that had already reported success"
    )
    assert targets == [credentials.SERVICE_NAME], (
        f"one logical secret must occupy one target; found {targets}"
    )
    assert not fake.holds_value_anywhere(OLD_KEY), "a superseded secret was left in the store"
    assert not fake.holds_value_anywhere(OLDER_REQUEST), (
        "the older request's value was left in the store"
    )
    assert older_result.outcome == credentials.MUTATION_UNCERTAIN
    assert not older_result.ok
    assert not older_result.provably_unchanged


def test_the_superseded_save_says_so_rather_than_claiming_a_rollback(monkeypatch):
    """A superseded request must not report the reason of one that rolled
    back. `credential_pair` turns "unconfirmed" into "JARVIS has asked for
    your previous key to be put back", and here it deliberately has not —
    saying so would be a promise about the store that nobody made."""
    from app.core import credentials

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake._raise_after_set = 1

    paused = _PausedAtLatestCheck(monkeypatch, credentials)
    older = _InAnotherThread(
        lambda: credentials.set_stored_api_key_detailed(OLDER_REQUEST),
    ).start()
    paused.wait_until_reached()
    assert credentials.set_stored_api_key_detailed(NEWER_REQUEST).ok
    paused.let_it_finish()
    older_result = older.join()
    settle()

    assert older_result.superseded, (
        "a request whose intent was replaced reported an ordinary rollback reason"
    )
    assert older_result.reason == credentials.MUTATION_REASON_SUPERSEDED


def test_a_superseded_failure_records_no_new_desired_generation(monkeypatch):
    """The design rule, asserted directly on the state it protects.

    `_record_desired_if_latest()` answering `None` means the older request
    has no claim on the credential any more. Nothing it does afterwards may
    create a newer generation — that is precisely how the check came to be
    bypassed by the line after it.
    """
    from app.core import credentials

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake._raise_after_set = 1

    paused = _PausedAtLatestCheck(monkeypatch, credentials)
    older = _InAnotherThread(
        lambda: credentials.set_stored_api_key_detailed(OLDER_REQUEST),
    ).start()
    paused.wait_until_reached()
    assert credentials.set_stored_api_key_detailed(NEWER_REQUEST).ok

    with credentials._mutation_state_lock:
        generation_after_newer, value_after_newer = credentials._desired_values[
            credentials.USERNAME
        ]
    assert value_after_newer == NEWER_REQUEST

    paused.let_it_finish()
    older.join()
    settle()

    with credentials._mutation_state_lock:
        generation_now, value_now = credentials._desired_values[credentials.USERNAME]
    assert value_now == NEWER_REQUEST, (
        "the older request replaced the newest desired value after being told not to"
    )
    assert generation_now == generation_after_newer, (
        "the older request minted a newer generation for its own rollback value"
    )


# ---------------------------------------------------------------------------
# Ordering 2 — an older failed save must not resurrect a removed credential
# ---------------------------------------------------------------------------

def test_a_stale_failed_save_does_not_resurrect_a_credential_a_newer_remove_deleted(monkeypatch):
    """The worst of the three for a person to experience: Remove reported
    the key was gone, and a save that failed before it put one back."""
    from app.core import credentials

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake._raise_after_set = 1

    paused = _PausedAtLatestCheck(monkeypatch, credentials)
    older = _InAnotherThread(
        lambda: credentials.set_stored_api_key_detailed(OLDER_REQUEST),
    ).start()
    paused.wait_until_reached()

    removal = credentials.clear_stored_api_key_detailed()
    assert removal.outcome == credentials.MUTATION_APPLIED, (
        "the newer removal did not succeed, so this test is not testing what it claims"
    )

    paused.let_it_finish()
    older.join()

    targets = _targets_after_settling(credentials, fake)
    assert targets == [], (
        f"a removed credential came back after an older failed save; found {targets}"
    )
    assert credentials.get_stored_api_key() == ""


# ---------------------------------------------------------------------------
# Ordering 3 — an older failed remove must not delete a newly saved key
# ---------------------------------------------------------------------------

def test_a_stale_failed_remove_does_not_delete_a_credential_a_newer_save_stored(monkeypatch):
    """A removal's rollback target is absence, so a stale one is a delete
    aimed at whatever happens to be in the entry — including a key the user
    saved afterwards and was told was saved."""
    from app.core import credentials

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake._raise_after_first_delete = 1

    paused = _PausedAtLatestCheck(monkeypatch, credentials)
    older = _InAnotherThread(credentials.clear_stored_api_key_detailed).start()
    paused.wait_until_reached()

    newer = credentials.set_stored_api_key_detailed(NEWER_REQUEST)
    assert newer.outcome == credentials.MUTATION_APPLIED, (
        "the newer save did not succeed, so this test is not testing what it claims"
    )

    paused.let_it_finish()
    older_result = older.join()

    targets = _targets_after_settling(credentials, fake)
    assert fake.value_at(credentials.SERVICE_NAME) == NEWER_REQUEST, (
        "an older failed removal deleted a key that a newer save had stored"
    )
    assert targets == [credentials.SERVICE_NAME], (
        f"one logical secret must occupy one target; found {targets}"
    )
    assert not older_result.ok


def test_the_superseded_removal_says_so_rather_than_claiming_nothing_changed(monkeypatch):
    """`provably_unchanged` is the only basis for telling someone nothing
    happened. A superseded removal established no such thing."""
    from app.core import credentials

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake._raise_after_first_delete = 1

    paused = _PausedAtLatestCheck(monkeypatch, credentials)
    older = _InAnotherThread(credentials.clear_stored_api_key_detailed).start()
    paused.wait_until_reached()
    assert credentials.set_stored_api_key_detailed(NEWER_REQUEST).ok
    paused.let_it_finish()
    older_result = older.join()
    settle()

    assert not older_result.provably_unchanged
    assert older_result.superseded
    assert older_result.reason == credentials.MUTATION_REASON_SUPERSEDED


# ---------------------------------------------------------------------------
# The ordinary failure path must keep working
# ---------------------------------------------------------------------------

def test_an_uncontested_failed_replacement_still_reconciles_to_the_previous_key(monkeypatch):
    """The correction must not be "stop reconciling". With no newer intent,
    `_record_desired_if_latest()` accepts the rollback and the previous key
    has to be put back — which is blocker 7's whole point."""
    from app.core import credentials

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake._raise_after_set = 1

    result = credentials.set_stored_api_key_detailed(NEW_KEY)
    settle()

    assert not result.ok
    assert not result.superseded, "nothing newer was requested, so nothing superseded this"
    assert fake.value_at(credentials.SERVICE_NAME) == OLD_KEY, (
        "a half-applied replacement was not reconciled back to the previous key"
    )
    assert fake.target_names() == [credentials.SERVICE_NAME]
    assert not fake.holds_value_anywhere(NEW_KEY)


def test_a_superseded_request_leaves_no_copy_of_the_secret_it_replaced(monkeypatch):
    """The compound-target invariant has to survive the concurrent case too.

    The older save copies the previous secret to the compound target on its
    way to failing. Whoever ends up owning the credential, that copy must
    not still be there when everything settles.
    """
    from app.core import credentials

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OTHER_KEY)
    fake._raise_after_set = 1

    paused = _PausedAtLatestCheck(monkeypatch, credentials)
    older = _InAnotherThread(
        lambda: credentials.set_stored_api_key_detailed(OLDER_REQUEST),
    ).start()
    paused.wait_until_reached()
    assert credentials.set_stored_api_key_detailed(NEWER_REQUEST).ok
    paused.let_it_finish()
    older.join()

    targets = _targets_after_settling(credentials, fake)
    compound = f"{credentials.USERNAME}@{credentials.SERVICE_NAME}"
    assert compound not in targets, "a copy of a superseded secret was left behind"
    assert not fake.holds_value_anywhere(OTHER_KEY)
    assert not fake.holds_value_anywhere(OLDER_REQUEST)


# ---------------------------------------------------------------------------
# What the person holding the machine is told
#
# The correction creates a fourth thing that can happen to a request, so
# there has to be a fourth sentence. The existing "unconfirmed" copy says
# JARVIS "has asked for your previous key to be put back" — which, for a
# superseded request, it deliberately has not done and must not do.
# ---------------------------------------------------------------------------

def _superseded():
    from app.core import credentials

    return credentials.MutationResult(
        credentials.MUTATION_UNCERTAIN, credentials.MUTATION_REASON_SUPERSEDED,
    )


def _pair_save_with(result):
    from unittest.mock import patch

    from app.core.ai import credential_pair

    with patch("app.core.credentials.stored_api_key_snapshot", return_value=(True, OLD_KEY)), \
            patch("app.core.credentials.set_stored_api_key_detailed", return_value=result):
        return credential_pair.save(NEWER_REQUEST, "", "configured_unverified")


def _pair_clear_with(result):
    from unittest.mock import patch

    from app.core.ai import credential_pair

    with patch("app.core.credentials.clear_stored_api_key_detailed", return_value=result):
        return credential_pair.clear()


def test_a_superseded_save_is_not_described_as_a_rollback_that_was_requested():
    outcome = _pair_save_with(_superseded())

    assert not outcome.ok
    assert outcome.stored is False
    lowered = outcome.message.lower()
    assert "previous key to be put back" not in lowered, (
        "the message promises a rollback that a superseded request deliberately did not ask for"
    )
    assert "nothing was changed" not in lowered, (
        "a superseded request established nothing about the store"
    )
    # It has to say the thing that is actually true, or the user cannot act.
    assert "newer" in lowered or "another change" in lowered, (
        "the message does not tell the user a newer change to the key is what is in place"
    )


def test_a_superseded_removal_does_not_claim_the_key_is_gone_or_that_nothing_happened():
    outcome = _pair_clear_with(_superseded())

    assert not outcome.ok
    lowered = outcome.message.lower()
    assert "api key removed" not in lowered
    assert "nothing was changed" not in lowered
    assert "newer" in lowered or "another change" in lowered


def test_the_ordinary_unconfirmed_messages_are_untouched():
    """The superseded case is a new branch, not a rewrite of the existing
    one: a genuinely timed-out write still says the previous key was asked
    for, because on that path it genuinely was."""
    from app.core import credentials

    timed_out = credentials.MutationResult(credentials.MUTATION_UNCERTAIN, "timed_out")

    assert "previous key to be put back" in _pair_save_with(timed_out).message.lower()
    assert "press remove" in _pair_clear_with(timed_out).message.lower()


# ---------------------------------------------------------------------------
# The structural rules behind the correction
# ---------------------------------------------------------------------------

def test_the_reconciliation_queue_never_mints_its_own_generation():
    """`_queue_reconciliation()` must be handed a generation that
    `_record_desired_if_latest()` already accepted, never mint one itself.

    Asserted on the source rather than only on behaviour, because the
    behavioural failure needs a race to show up and the defect is one line:
    a future edit that reintroduces `_record_desired(...)` here should fail
    immediately, on every platform, without a thread.
    """
    import ast
    import pathlib

    from app.core import credentials

    tree = ast.parse(pathlib.Path(credentials.__file__).read_text(encoding="utf-8"))
    queue = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_queue_reconciliation"
    )
    called = {
        node.func.id for node in ast.walk(queue)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_record_desired" not in called, (
        "_queue_reconciliation() mints a new newest generation again — that is the "
        "defect: it hands an older request's rollback value a claim it was just denied"
    )
    assert "generation" in {argument.arg for argument in queue.args.args}, (
        "_queue_reconciliation() no longer takes an accepted generation"
    )


def test_discard_superseded_is_never_entered_while_the_backend_lock_is_held(monkeypatch):
    """Backs the corrected docstring with the fact it now states.

    `_discard_superseded()` used to document itself as "called with
    `_backend_lock` already held", which is the opposite of the truth: every
    read and delete inside it acquires that lock on an isolated thread, so
    entering it under the lock would deadlock rather than fail. The
    documentation is corrected; this is what makes the corrected sentence
    checkable, across the success, timeout and failure paths.
    """
    from app.core import credentials

    observed = []
    real = credentials._discard_superseded

    def watched(keyring, username, survivor):
        observed.append(credentials._backend_lock.locked())
        return real(keyring, username, survivor)

    monkeypatch.setattr(credentials, "_discard_superseded", watched)

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)

    assert credentials.set_stored_api_key_detailed(NEW_KEY).ok
    fake._raise_after_set = 1
    credentials.set_stored_api_key_detailed(OLDER_REQUEST)
    settle()

    assert observed, "no path reached _discard_superseded, so this proves nothing"
    assert not any(observed), (
        "_discard_superseded was entered while _backend_lock was held; its own isolated "
        "reads re-acquire that lock, so this is a deadlock waiting for a slower machine"
    )
