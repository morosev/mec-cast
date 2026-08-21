"""The run state machine. Pure functions, so none of this needs a service."""

from __future__ import annotations

import pytest

from mec_cast_admin.state import (
    OCCUPIES_SLOT,
    TERMINAL,
    Action,
    Event,
    RunState,
    TransitionError,
    advance,
    allowed_actions,
    is_terminal,
    occupies_slot,
)


class TestTransitions:
    def test_the_happy_path_runs_start_to_stop(self):
        s = RunState.DRAFT
        for event, expected in [
            (Event.START, RunState.STARTING),
            (Event.QUORUM_MET, RunState.RUNNING),
            (Event.STOP, RunState.STOPPING),
            (Event.REPORTS_COMPLETE, RunState.STOPPED),
        ]:
            s = advance(s, event)
            assert s is expected

    def test_a_lost_participant_degrades_and_recovers(self):
        s = advance(advance(RunState.DRAFT, Event.START), Event.QUORUM_MET)
        s = advance(s, Event.PARTICIPANT_LOST)
        assert s is RunState.DEGRADED
        assert advance(s, Event.PARTICIPANT_RECOVERED) is RunState.RUNNING

    def test_losing_a_second_participant_stays_degraded(self):
        # Two nodes dropping must not be a different state from one dropping.
        assert advance(RunState.DEGRADED, Event.PARTICIPANT_LOST) is RunState.DEGRADED

    def test_a_run_that_never_reaches_quorum_fails(self):
        assert advance(RunState.STARTING, Event.START_TIMEOUT) is RunState.FAILED

    def test_a_node_dying_while_draining_does_not_strand_the_run(self):
        # Otherwise `stopping` is a trap: the reports never complete and the
        # single active-run slot is never released.
        assert advance(RunState.STOPPING, Event.ALL_OFFLINE) is RunState.STOPPED

    @pytest.mark.parametrize("state", sorted(TERMINAL - {RunState.REMOVED}))
    def test_terminal_states_accept_only_removal(self, state):
        assert advance(state, Event.REMOVE) is RunState.REMOVED
        for event in Event:
            if event is Event.REMOVE:
                continue
            with pytest.raises(TransitionError):
                advance(state, event)

    def test_a_stopped_run_is_never_restarted(self):
        # Restarting would append a second experiment into the first's CSV.
        with pytest.raises(TransitionError):
            advance(RunState.STOPPED, Event.START)

    def test_removed_is_absorbing(self):
        for event in Event:
            with pytest.raises(TransitionError):
                advance(RunState.REMOVED, event)

    def test_rejection_names_the_state_and_event(self):
        with pytest.raises(TransitionError) as excinfo:
            advance(RunState.RUNNING, Event.START)
        assert excinfo.value.state is RunState.RUNNING
        assert excinfo.value.event is Event.START
        assert "running" in str(excinfo.value)


class TestAllowedActions:
    def test_every_state_declares_its_actions(self):
        for state in RunState:
            assert isinstance(allowed_actions(state), list)

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (RunState.DRAFT, ["start", "remove"]),
            (RunState.STARTING, ["stop"]),
            (RunState.RUNNING, ["stop"]),
            (RunState.DEGRADED, ["stop"]),
            (RunState.STOPPING, []),
            (RunState.STOPPED, ["remove"]),
            (RunState.FAILED, ["remove"]),
            (RunState.REMOVED, []),
        ],
    )
    def test_the_buttons_match_the_table(self, state, expected):
        assert allowed_actions(state) == expected

    def test_every_advertised_action_is_actually_legal(self):
        # The contract the UI relies on: if a button is enabled, pressing it
        # must not 409. This is the test that makes serving `allowed` safe.
        for state in RunState:
            for action in allowed_actions(state):
                advance(state, Event(action))


class TestSlotOccupancy:
    def test_drafts_do_not_occupy_the_slot(self):
        # Several runs may be queued; only one may be active.
        assert not occupies_slot(RunState.DRAFT)

    def test_terminal_states_do_not_occupy_the_slot(self):
        for state in TERMINAL:
            assert not occupies_slot(state)

    def test_active_states_occupy_the_slot(self):
        for state in (
            RunState.STARTING,
            RunState.RUNNING,
            RunState.DEGRADED,
            RunState.STOPPING,
        ):
            assert occupies_slot(state)

    def test_slot_and_terminal_sets_are_disjoint(self):
        assert not (OCCUPIES_SLOT & TERMINAL)

    def test_is_terminal_agrees_with_the_set(self):
        for state in RunState:
            assert is_terminal(state) == (state in TERMINAL)


def test_action_values_are_events():
    # `advance(state, Event(action))` is only safe while this holds.
    for action in Action:
        assert Event(str(action))
