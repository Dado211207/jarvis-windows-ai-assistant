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
one has committed, because the newer one could not have started. The
generation counters below are kept anyway, as a tripwire — a boundary with
one implementation is a boundary with one bug — so a future path that
escapes this module is caught by an assertion rather than by a race.

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

        Always true while `begin()` holds the gate, which is the point: it
        is a tripwire for a future code path that reaches a store without
        going through this module, not the mechanism that makes the
        invariant hold.
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
