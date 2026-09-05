"""One credential change at a time — the whole change, not each store.

**Why this exists.** Saving the Anthropic key is four steps across two
stores: read a snapshot, write the credential, write the Workspace ID and
verification state, and roll the credential back if that second write
failed. `app/core/credentials.py` makes each *credential* mutation safe
against a concurrent one — latest intent wins, an older failed write never
overwrites a newer successful one. That is a guarantee about one store, and
it was mistaken for a guarantee about the operation.

It is not. Two overlapping saves interleaved *between* the steps, and both
reported success:

    older_result    applied
    newer_result    applied
    final_key       NEWER-KEY
    final_workspace OLDER-WORKSPACE

Every part behaved as designed. The older request's credential write really
had landed, so the credential layer really did owe it `MUTATION_APPLIED`;
`_cleanup_survivor()` really did correctly follow the newer generation. What
nobody had said is that **"my credential write landed" is not the same
permission as "I may still commit my own description of the credential"**,
and there was nothing above the two stores able to tell those apart. The
same window damages the other orderings in the opposite direction: a stale
metadata *clear* leaves a stored key describing nothing.

**The rule.** A credential-pair operation runs from its snapshot through to
its returned `PairOutcome` with nothing else touching either store. Not the
credential write alone — the snapshot, both stores, the rollback, the
runtime-downgrade note and the outcome that describes them.

**Why a lock and not a generation check at each step.** Generations tell an
operation that it has been overtaken; they do not stop it having read a
snapshot that is already stale, and every additional step would need its own
check, correct in its own way, forever. Serialising the operation makes the
question disappear: an older operation cannot still be running once a newer
one has committed, because the newer one could not have started.

**What the generation counter is, and what it is not.** It is the *pair
revision*: a non-secret, monotonic number that changes whenever a
credential-pair transaction runs. Readers stamp it onto the snapshot they
took (`app/core/ai/credential_view.py`) and a delayed provider failure
presents it back, so a rejection can only ever downgrade the exact pair that
made the request. `Transaction.is_newest` asserts it inside the locked body,
where it is an internal invariant of this module and always true.

It is **not** a detector for a writer that never came through here: such a
writer does not touch this counter, so the comparison would stay true while
it did its damage. What actually catches that is a structural test —
`tests/test_credential_request_ordering.py` walks every module under `app/`
and fails if anything outside a named allowlist calls the credential
mutators. An earlier version of this comment claimed otherwise, and the
claim was wrong.

**Two orderings, two counters.** The revision orders *transactions*, which
is what a reader needs. An `Intent` orders *requests*, which is what the
HTTP layer needs: `POST /settings/api-key` asks Anthropic to verify the key
before it may store it, and that network call must not happen under this
lock. So the route takes an intent when it is admitted, verifies, and
presents the intent here. The rule is **the request admitted later wins** —
the order the person acted in, not the order Anthropic happened to answer
in — and an older request that arrives late is refused rather than applied.

**Why not the UI.** Disabling both buttons is worth doing and is done, but
it cannot be the correction: two concurrent `POST /settings/api-key`
requests need no button at all, and FastAPI runs sync routes on a thread
pool. A boundary enforced only by the page is not a boundary.

**Nothing here waits forever.** A request that cannot get in is refused
with an outcome that says nothing was attempted — which is true, and is
therefore safe to tell someone.

This module holds no credential value, writes nothing, and logs no value:
its entire state is three integers and a lock.
"""

import contextlib
import threading
from dataclasses import dataclass

from app.logging_config import get_logger

logger = get_logger("core.ai.credential_transaction")

#: How long a second credential change waits for the one in front of it.
#:
#: Sized against what one transaction can actually take: the credential
#: store's own calls are each bounded by `credentials.TIMEOUT_SECONDS`, and a
#: save can make several of them (snapshot, write, the compound-target
#: discard's read/delete/read, and a rollback write). A queued request
#: therefore has room to be served rather than refused in every ordinary
#: case, including the one this exists for — somebody pressing Save and then
#: Remove — while a backend that has genuinely stopped answering still ends
#: in a refusal instead of a hung request.
WAIT_SECONDS = 45.0

#: Serialises the whole operation. Never held across anything unbounded:
#: every store call inside it carries its own timeout.
_gate = threading.Lock()

#: Guards the counters below, and wakes `wait_for_waiters`.
_state = threading.Condition()
_generation = 0
_committed = 0
_waiting = 0
#: Requests admitted, and the newest one that has claimed the credential.
#: Separate from the generation above because a request is admitted before
#: its Anthropic verification and claims only afterwards.
_intents = 0
_claimed_intent = 0


class TransactionBusy(Exception):
    """Another credential change is in progress and this one did not start.

    Carries the kind of operation that was refused, never a value.
    """


@dataclass(frozen=True)
class Transaction:
    """One credential-pair change, identified by a monotonic id."""

    id: int

    @property
    def is_newest(self) -> bool:
        """Whether no transaction has been started since this one.

        An internal invariant of this module: it is always true while
        `begin()` holds the gate, and asserting it before each metadata
        write documents *why* the write is allowed rather than leaving the
        reader to reconstruct the argument. It cannot see a writer that
        never entered this module — see the module docstring for what does.
        """
        with _state:
            return _generation == self.id

    def commit(self) -> None:
        """Record that this transaction reached its end.

        Idempotent, and called automatically when `begin()`'s block exits
        normally — a `save()` has eight return paths and none of them should
        have to remember this.
        """
        global _committed
        with _state:
            _committed = max(_committed, self.id)


@contextlib.contextmanager
def begin(kind: str, wait: float = None):
    """Run the enclosed credential-pair operation alone.

    *kind* is a short operation name for diagnostics ("save", "clear"); it
    is never a credential value. *wait* overrides `WAIT_SECONDS` and exists
    for the tests that prove the bound is real.

    Raises `TransactionBusy` — before touching anything — when the wait runs
    out. Callers turn that into an outcome that says nothing was changed,
    which is exactly what happened.
    """
    global _generation, _waiting

    limit = WAIT_SECONDS if wait is None else wait

    # Counted as waiting from the moment this call is entered rather than
    # from the moment it blocks. That is deliberately the stronger
    # statement: past this point the request is committed to the
    # coordinator and has provably not touched either store, which is what
    # makes `wait_for_waiters` a sound thing to synchronise on.
    with _state:
        _waiting += 1
        _state.notify_all()
    try:
        acquired = _gate.acquire(True, limit) if limit > 0 else _gate.acquire(False)
    finally:
        with _state:
            _waiting -= 1
            _state.notify_all()

    if not acquired:
        logger.warning(
            "A credential change (%s) was refused: another one is still running.", kind,
        )
        raise TransactionBusy(kind)

    try:
        with _state:
            _generation += 1
            transaction = Transaction(_generation)
        try:
            yield transaction
        finally:
            transaction.commit()
    finally:
        _gate.release()


@contextlib.contextmanager
def _hold(kind: str, wait: float = None):
    """Hold the gate without minting a transaction or moving the revision.

    The shared body of `read_gate()` and `pair_state_gate()`. Both need
    mutual exclusion against a credential change; neither is one.
    """
    global _waiting

    limit = WAIT_SECONDS if wait is None else wait
    with _state:
        _waiting += 1
        _state.notify_all()
    try:
        acquired = _gate.acquire(True, limit) if limit > 0 else _gate.acquire(False)
    finally:
        with _state:
            _waiting -= 1
            _state.notify_all()
    if not acquired:
        raise TransactionBusy(kind)
    try:
        yield
    finally:
        _gate.release()


@contextlib.contextmanager
def read_gate(wait: float = None):
    """Hold the gate without minting a transaction.

    For a reader that needs both stores to describe the same credential.
    It deliberately does **not** advance the revision: a read changes
    nothing, and a revision that moved on every read would mean no snapshot
    was ever reusable and every provider failure looked stale.

    Raises `TransactionBusy` if the wait runs out, exactly as `begin()`
    does. A reader that cannot get in has a coherent older snapshot to fall
    back on; it must never assemble one out of two separate reads.
    """
    with _hold("read", wait):
        yield


@contextlib.contextmanager
def pair_state_gate(wait: float = None):
    """Hold the gate to change how the stored pair is *described*.

    **The rule this encodes.** The revision identifies *which credential
    pair is stored* — the key and its Workspace ID together. It advances
    when a transaction changes that pair, because afterwards it is a
    different credential. A verification-state change describes the **same**
    pair differently: the key has not moved, only what was last observed
    about it. So this gate deliberately does not mint a transaction and does
    not advance the revision.

    Using `begin()` here would be a defect with a plausible shape. It would
    increment the revision *before* the expected one could be checked, so
    every legitimate rejection of the current credential would compare
    against a number that had already moved and be discarded as stale — the
    exact opposite failure to the one being fixed, and a silent one: nothing
    would ever be downgraded again.

    What it does provide is mutual exclusion against `save()` and `clear()`
    for the whole check-and-write, which is what
    `providers.note_runtime_failure()` needs. Validating the revision and
    then writing outside the gate let a save commit in between, and the
    rejection of the replaced key landed on the key that replaced it.

    Nothing unbounded may run inside it, and in particular no Anthropic
    request: the provider call that produced the failure has already
    finished by the time this is taken.
    """
    with _hold("verification-state", wait):
        yield


def pair_revision() -> int:
    """The credential pair's current revision — non-secret, monotonic.

    Changes whenever a transaction runs, which is the conservative reading:
    a transaction that ended up writing nothing still moves it, so a
    delayed failure is skipped rather than attributed on a guess. A real
    rejection of the current pair simply recurs on the next request.
    """
    with _state:
        return _generation


@dataclass(frozen=True)
class Intent:
    """One admitted request's place in the order the user acted in.

    Taken *before* the Anthropic verification, so the ordering is the one
    the person can observe rather than the one the network produced.
    """

    id: int


def admit(kind: str) -> Intent:
    """Take an intent for a request that is about to change the credential."""
    global _intents
    with _state:
        _intents += 1
        return Intent(_intents)


def claim(intent: Intent) -> bool:
    """Whether *intent* may still change the credential, claiming it if so.

    False once a request admitted after it has already claimed: that later
    request is what the user asked for most recently, and overwriting it
    with an older one whose verification happened to finish late would make
    the outcome depend on Anthropic's latency.

    Called from inside `begin()`'s locked body, so the test and the claim
    are atomic with respect to every other credential-pair operation.
    """
    global _claimed_intent
    with _state:
        if intent.id <= _claimed_intent:
            return False
        _claimed_intent = intent.id
        return True


def committed_generation() -> int:
    """The id of the newest transaction that has reached its end."""
    with _state:
        return _committed


def waiting() -> int:
    """How many credential changes are queued, including any about to run."""
    with _state:
        return _waiting


def wait_for_waiters(count: int, timeout: float) -> bool:
    """Block until at least *count* changes are queued. Never raises.

    Not called on any request path. It exists so a test can synchronise on
    "the second request has entered the coordinator and therefore cannot
    have touched a store" without sleeping — the same reason
    `credentials.wait_for_pending_mutations()` exists.
    """
    import time

    deadline = time.monotonic() + max(0.0, timeout)
    with _state:
        while _waiting < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _state.wait(remaining)
    return True
