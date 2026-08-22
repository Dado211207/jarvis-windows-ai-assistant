"""Tests for app/core/runtime_state.py — the single authoritative runtime
state model."""

import pytest

from app.core.events import EventType, event_bus
from app.core.runtime_state import (
    InvalidTransitionError,
    RuntimeState,
    RuntimeStateMachine,
)


@pytest.fixture
def machine():
    return RuntimeStateMachine()


def test_initial_state_is_booting(machine):
    assert machine.state == RuntimeState.BOOTING


def test_valid_transition_boot_to_standby(machine):
    result = machine.transition(RuntimeState.STANDBY)
    assert result == RuntimeState.STANDBY
    assert machine.state == RuntimeState.STANDBY


def test_invalid_transition_raises(machine):
    """BOOTING cannot jump straight to EXECUTING — must pass through STANDBY."""
    with pytest.raises(InvalidTransitionError):
        machine.transition(RuntimeState.EXECUTING)
    # State is unchanged after a rejected transition.
    assert machine.state == RuntimeState.BOOTING


def test_can_transition_reports_without_mutating(machine):
    assert machine.can_transition(RuntimeState.STANDBY) is True
    assert machine.can_transition(RuntimeState.EXECUTING) is False
    assert machine.state == RuntimeState.BOOTING


def test_full_voice_command_cycle(machine):
    """BOOTING -> STANDBY -> LISTENING -> TRANSCRIBING -> THINKING ->
    EXECUTING -> SPEAKING -> STANDBY, the shape a real push-to-talk command
    with no approval needed takes."""
    machine.transition(RuntimeState.STANDBY)
    machine.transition(RuntimeState.LISTENING)
    machine.transition(RuntimeState.TRANSCRIBING)
    machine.transition(RuntimeState.THINKING)
    machine.transition(RuntimeState.EXECUTING)
    machine.transition(RuntimeState.SPEAKING)
    machine.transition(RuntimeState.STANDBY)
    assert machine.state == RuntimeState.STANDBY


def test_approval_cycle(machine):
    """THINKING -> AWAITING_APPROVAL -> EXECUTING (approved) and the
    cancelled/expired branch AWAITING_APPROVAL -> STANDBY."""
    machine.transition(RuntimeState.STANDBY)
    machine.transition(RuntimeState.THINKING)
    machine.transition(RuntimeState.AWAITING_APPROVAL)
    assert machine.can_transition(RuntimeState.EXECUTING) is True
    assert machine.can_transition(RuntimeState.STANDBY) is True
    machine.transition(RuntimeState.STANDBY)  # cancelled/expired path
    assert machine.state == RuntimeState.STANDBY


def test_standby_can_go_directly_to_awaiting_approval(machine):
    """A deterministic (regex-routed) command that maps straight to an
    approval-required tool never goes through THINKING at all — there is
    no LLM call for deterministic routing (see app/core/router.py). The
    state machine must allow STANDBY -> AWAITING_APPROVAL directly, not
    only via THINKING, or command dispatch would have to fake a THINKING
    pulse that never really happened just to satisfy the graph."""
    machine.transition(RuntimeState.STANDBY)
    machine.transition(RuntimeState.AWAITING_APPROVAL)
    assert machine.state == RuntimeState.AWAITING_APPROVAL


def test_error_reachable_from_every_active_state():
    """Any active (non-terminal-ish) state must be able to fail into ERROR
    — a crash mid-anything must always have somewhere safe to go."""
    for state in RuntimeState:
        if state in (RuntimeState.OFFLINE, RuntimeState.ERROR):
            continue  # OFFLINE only comes back via BOOTING; ERROR->ERROR is not a transition
        m = RuntimeStateMachine()
        m.force_state(state)
        assert m.can_transition(RuntimeState.ERROR), f"{state} cannot reach ERROR"


def test_offline_reachable_from_every_state():
    """A shutdown/disconnect can happen at any point, including mid-error."""
    for state in RuntimeState:
        if state == RuntimeState.OFFLINE:
            continue  # OFFLINE->OFFLINE is not a transition
        m = RuntimeStateMachine()
        m.force_state(state)
        assert m.can_transition(RuntimeState.OFFLINE), f"{state} cannot reach OFFLINE"


def test_offline_can_only_return_via_booting():
    m = RuntimeStateMachine()
    m.force_state(RuntimeState.OFFLINE)
    for state in RuntimeState:
        if state == RuntimeState.BOOTING:
            assert m.can_transition(state)
        else:
            assert not m.can_transition(state)


def test_error_recovers_to_standby(machine):
    machine.force_state(RuntimeState.ERROR)
    machine.transition(RuntimeState.STANDBY)
    assert machine.state == RuntimeState.STANDBY


def test_transition_publishes_typed_event(machine):
    before = event_bus.latest_seq()
    machine.transition(RuntimeState.STANDBY, correlation_id="corr-123", reason="startup complete")
    events = event_bus.since(before)
    assert len(events) == 1
    e = events[0]
    assert e.type == EventType.RUNTIME_STATE
    assert e.correlation_id == "corr-123"
    assert e.payload["from"] == "booting"
    assert e.payload["to"] == "standby"
    assert e.payload["reason"] == "startup complete"
    assert e.timestamp is not None


def test_invalid_transition_does_not_publish_event(machine):
    before = event_bus.latest_seq()
    with pytest.raises(InvalidTransitionError):
        machine.transition(RuntimeState.EXECUTING)
    assert event_bus.since(before) == []


def test_force_state_bypasses_validation_for_test_setup():
    m = RuntimeStateMachine()
    m.force_state(RuntimeState.EXECUTING)
    assert m.state == RuntimeState.EXECUTING


def test_invalid_transition_error_message_includes_both_states(machine):
    with pytest.raises(InvalidTransitionError) as exc_info:
        machine.transition(RuntimeState.SPEAKING)
    assert "booting" in str(exc_info.value)
    assert "speaking" in str(exc_info.value)


def test_module_singleton_exists():
    from app.core.runtime_state import runtime
    assert isinstance(runtime, RuntimeStateMachine)
