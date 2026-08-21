"""The run state machine.

Pure: no I/O, no clock, no globals. The REST routes and the WebSocket handler
both go through :func:`advance`, and the operator page decides nothing — each
row is served an ``allowed`` list that the buttons read directly. One place
holds the rules, so the test over this module is also the test of the UI.
"""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    DRAFT = "draft"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    REMOVED = "removed"


class Event(StrEnum):
    """Operator intents and observations from the registry."""

    START = "start"
    STOP = "stop"
    REMOVE = "remove"

    QUORUM_MET = "quorum_met"
    PARTICIPANT_LOST = "participant_lost"
    PARTICIPANT_RECOVERED = "participant_recovered"
    ALL_OFFLINE = "all_offline"
    REPORTS_COMPLETE = "reports_complete"
    START_TIMEOUT = "start_timeout"


class Action(StrEnum):
    """What an operator may do to a run. Drives the row's buttons."""

    START = "start"
    STOP = "stop"
    REMOVE = "remove"


TERMINAL: frozenset[RunState] = frozenset({RunState.STOPPED, RunState.FAILED, RunState.REMOVED})

#: States that occupy the single-active-run slot. ``DRAFT`` does not: a run
#: that has never started holds no Recorder, so any number may be queued.
OCCUPIES_SLOT: frozenset[RunState] = frozenset(
    {RunState.STARTING, RunState.RUNNING, RunState.DEGRADED, RunState.STOPPING}
)

_TRANSITIONS: dict[tuple[RunState, Event], RunState] = {
    (RunState.DRAFT, Event.START): RunState.STARTING,
    (RunState.DRAFT, Event.REMOVE): RunState.REMOVED,
    (RunState.STARTING, Event.QUORUM_MET): RunState.RUNNING,
    (RunState.STARTING, Event.STOP): RunState.STOPPING,
    (RunState.STARTING, Event.START_TIMEOUT): RunState.FAILED,
    (RunState.STARTING, Event.ALL_OFFLINE): RunState.FAILED,
    (RunState.RUNNING, Event.PARTICIPANT_LOST): RunState.DEGRADED,
    (RunState.RUNNING, Event.STOP): RunState.STOPPING,
    (RunState.RUNNING, Event.ALL_OFFLINE): RunState.FAILED,
    (RunState.DEGRADED, Event.PARTICIPANT_RECOVERED): RunState.RUNNING,
    (RunState.DEGRADED, Event.PARTICIPANT_LOST): RunState.DEGRADED,
    (RunState.DEGRADED, Event.STOP): RunState.STOPPING,
    (RunState.DEGRADED, Event.ALL_OFFLINE): RunState.FAILED,
    (RunState.STOPPING, Event.REPORTS_COMPLETE): RunState.STOPPED,
    # A node that dies while draining must not strand the run in `stopping`.
    (RunState.STOPPING, Event.ALL_OFFLINE): RunState.STOPPED,
    (RunState.STOPPED, Event.REMOVE): RunState.REMOVED,
    (RunState.FAILED, Event.REMOVE): RunState.REMOVED,
}

_ALLOWED: dict[RunState, tuple[Action, ...]] = {
    RunState.DRAFT: (Action.START, Action.REMOVE),
    RunState.STARTING: (Action.STOP,),
    RunState.RUNNING: (Action.STOP,),
    RunState.DEGRADED: (Action.STOP,),
    RunState.STOPPING: (),
    RunState.STOPPED: (Action.REMOVE,),
    RunState.FAILED: (Action.REMOVE,),
    RunState.REMOVED: (),
}


class TransitionError(ValueError):
    """An event that is not legal from the current state."""

    def __init__(self, state: RunState, event: Event) -> None:
        super().__init__(f"cannot {event} a run that is {state}")
        self.state = state
        self.event = event


def advance(state: RunState, event: Event) -> RunState:
    """Return the state after ``event``.

    Raises:
        TransitionError: if the transition is not in the table. Callers turn
            this into a 409 whose body names the current state, which is what
            an operator needs to see.
    """
    try:
        return _TRANSITIONS[(state, event)]
    except KeyError:
        raise TransitionError(state, event) from None


def allowed_actions(state: RunState) -> list[str]:
    """Operator actions legal from ``state``, as plain strings for the API."""
    return [str(a) for a in _ALLOWED[state]]


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL


def occupies_slot(state: RunState) -> bool:
    """True when the run holds the single active-run slot."""
    return state in OCCUPIES_SLOT
