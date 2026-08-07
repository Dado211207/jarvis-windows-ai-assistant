"""Genuine multi-threaded concurrency tests.

Existing coverage (test_approvals.py's test_confirm_cannot_execute_twice)
proves double-execution is refused when confirm() is called twice in a
row — a sequential call, not a race. FastAPI can run sync request
handlers concurrently in its threadpool, so two /actions/{id}/confirm
requests for the same action really can reach PendingActionStore.confirm()
at close to the same instant. These tests use real threads and a Barrier
to force that contention and prove the lock-based guarantees hold under
it, not just when called one after another.
"""

import threading

from app.core.pending_actions import PendingActionStore
from app.core.runtime_state import InvalidTransitionError, RuntimeState, RuntimeStateMachine


def _make_pending_action(store: PendingActionStore):
    return store.create(
        command="clear logs",
        tool_name="clear_logs",
        action_name="Clear Logs",
        description="test",
        risk_level="medium",
        parameters={},
    )


def test_pending_action_confirm_is_race_safe_under_real_threads():
    store = PendingActionStore()
    action = _make_pending_action(store)

    results = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def try_confirm():
        barrier.wait()
        outcome = store.confirm(action.id)
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=try_confirm) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1, "exactly one of 8 simultaneous confirm() calls must win"
    assert store.get(action.id).status == "confirmed"


def test_pending_action_confirm_vs_cancel_race_only_one_wins():
    store = PendingActionStore()
    action = _make_pending_action(store)

    results = {}
    barrier = threading.Barrier(2)

    def do_confirm():
        barrier.wait()
        results["confirm"] = store.confirm(action.id)

    def do_cancel():
        barrier.wait()
        results["cancel"] = store.cancel(action.id)

    t1 = threading.Thread(target=do_confirm)
    t2 = threading.Thread(target=do_cancel)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    winners = [v for v in results.values() if v is not None]
    assert len(winners) == 1, "confirm and cancel racing the same action must not both win"
    assert store.get(action.id).status in ("confirmed", "cancelled")


def test_runtime_state_transition_has_exactly_one_winner_under_contention():
    """Many threads racing to leave STANDBY for EXECUTING at once — the
    lock must serialize them so exactly one succeeds and the rest see
    InvalidTransitionError (EXECUTING has no self-loop), never a torn or
    duplicated state change."""
    machine = RuntimeStateMachine()
    machine.transition(RuntimeState.STANDBY)

    winners = []
    losers = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def try_execute():
        barrier.wait()
        try:
            machine.transition(RuntimeState.EXECUTING)
            with results_lock:
                winners.append(threading.get_ident())
        except InvalidTransitionError:
            with results_lock:
                losers.append(threading.get_ident())

    threads = [threading.Thread(target=try_execute) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1
    assert len(losers) == 7
    assert machine.state == RuntimeState.EXECUTING


def test_runtime_state_try_transition_never_raises_under_contention():
    """try_transition() (used by command dispatch) must degrade to
    'skipped, logged' under the exact same contention that makes
    transition() raise — never propagate InvalidTransitionError and
    never fail the command it's attached to."""
    machine = RuntimeStateMachine()
    machine.transition(RuntimeState.STANDBY)

    outcomes = []
    outcomes_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def try_execute():
        barrier.wait()
        ok = machine.try_transition(RuntimeState.EXECUTING)
        with outcomes_lock:
            outcomes.append(ok)

    threads = [threading.Thread(target=try_execute) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 7
    assert machine.state == RuntimeState.EXECUTING
