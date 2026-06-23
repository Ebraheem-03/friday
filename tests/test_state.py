"""Unit tests for core/state.py.

Coverage
--------
- All valid transitions succeed and update current state
- Invalid transition raises InvalidTransition (names from -> to)
- Observers fire with correct (old, new) args on each transition
- A raising observer does not corrupt the machine state
- reset() works from every state
- subscribe / unsubscribe lifecycle
- State enum has expected members
- ALLOWED_TRANSITIONS includes every documented pair
"""

from __future__ import annotations

import pytest

from core.state import (
    ALLOWED_TRANSITIONS,
    InvalidTransition,
    State,
    StateMachine,
)


# ---------------------------------------------------------------------------
# State enum sanity
# ---------------------------------------------------------------------------

class TestStateEnum:
    def test_all_states_present(self) -> None:
        names = {s.name for s in State}
        assert names == {"IDLE", "LISTENING", "THINKING", "SPEAKING", "ACTING"}

    def test_state_values_are_strings(self) -> None:
        for state in State:
            assert isinstance(state.value, str)


# ---------------------------------------------------------------------------
# Valid transitions
# ---------------------------------------------------------------------------

VALID_TRANSITION_CASES = [
    (State.IDLE, State.LISTENING, "wake word"),
    (State.LISTENING, State.THINKING, "end of utterance"),
    (State.LISTENING, State.IDLE, "timeout / abort from LISTENING"),
    (State.THINKING, State.SPEAKING, "brain produced speech"),
    (State.THINKING, State.ACTING, "brain issued tool-call"),
    (State.THINKING, State.IDLE, "error during thinking"),
    (State.SPEAKING, State.IDLE, "TTS finished"),
    (State.SPEAKING, State.LISTENING, "barge-in"),
    (State.SPEAKING, State.THINKING, "chained reply"),
    (State.ACTING, State.THINKING, "action completed"),
    (State.ACTING, State.SPEAKING, "action -> direct speech"),
    (State.ACTING, State.IDLE, "action aborted"),
]


class TestValidTransitions:
    @pytest.mark.parametrize("from_state, to_state, label", VALID_TRANSITION_CASES)
    def test_valid_transition_succeeds(
        self, from_state: State, to_state: State, label: str
    ) -> None:
        sm = StateMachine(initial=from_state)
        sm.transition(to_state)
        assert sm.current == to_state, f"Failed: {label}"

    def test_initial_state_is_idle(self) -> None:
        sm = StateMachine()
        assert sm.current == State.IDLE

    def test_custom_initial_state(self) -> None:
        sm = StateMachine(initial=State.THINKING)
        assert sm.current == State.THINKING


# ---------------------------------------------------------------------------
# Invalid transitions
# ---------------------------------------------------------------------------

INVALID_TRANSITION_CASES = [
    # Cannot go forward more than one step normally
    (State.IDLE, State.THINKING, "idle -> thinking (skips LISTENING)"),
    (State.IDLE, State.SPEAKING, "idle -> speaking"),
    (State.IDLE, State.ACTING, "idle -> acting"),
    (State.LISTENING, State.SPEAKING, "listening -> speaking (skips THINKING)"),
    (State.LISTENING, State.ACTING, "listening -> acting (skips THINKING)"),
    (State.THINKING, State.LISTENING, "thinking -> listening (no barge-in here)"),
    (State.ACTING, State.LISTENING, "acting -> listening (must route through THINKING)"),
]


class TestInvalidTransitions:
    @pytest.mark.parametrize("from_state, to_state, label", INVALID_TRANSITION_CASES)
    def test_invalid_transition_raises(
        self, from_state: State, to_state: State, label: str
    ) -> None:
        sm = StateMachine(initial=from_state)
        with pytest.raises(InvalidTransition) as exc_info:
            sm.transition(to_state)
        err = exc_info.value
        assert err.from_state == from_state, label
        assert err.to_state == to_state, label

    def test_invalid_transition_error_message_names_states(self) -> None:
        sm = StateMachine(initial=State.IDLE)
        with pytest.raises(InvalidTransition) as exc_info:
            sm.transition(State.ACTING)
        msg = str(exc_info.value)
        assert "IDLE" in msg
        assert "ACTING" in msg

    def test_state_unchanged_after_invalid_transition(self) -> None:
        """Machine state must not change when transition is rejected."""
        sm = StateMachine(initial=State.IDLE)
        with pytest.raises(InvalidTransition):
            sm.transition(State.ACTING)
        assert sm.current == State.IDLE


# ---------------------------------------------------------------------------
# Observers
# ---------------------------------------------------------------------------

class TestObservers:
    def test_observer_fires_on_transition(self) -> None:
        calls: list[tuple[State, State]] = []
        sm = StateMachine()
        sm.subscribe(lambda old, new: calls.append((old, new)))

        sm.transition(State.LISTENING)

        assert calls == [(State.IDLE, State.LISTENING)]

    def test_observer_receives_correct_old_new(self) -> None:
        received: list[tuple[State, State]] = []
        sm = StateMachine(initial=State.LISTENING)
        sm.subscribe(lambda o, n: received.append((o, n)))

        sm.transition(State.THINKING)

        assert received[0] == (State.LISTENING, State.THINKING)

    def test_multiple_observers_all_fire(self) -> None:
        counts = [0, 0]
        sm = StateMachine()
        sm.subscribe(lambda o, n: counts.__setitem__(0, counts[0] + 1))
        sm.subscribe(lambda o, n: counts.__setitem__(1, counts[1] + 1))

        sm.transition(State.LISTENING)

        assert counts == [1, 1]

    def test_observer_fires_for_each_transition(self) -> None:
        calls: list[tuple[State, State]] = []
        sm = StateMachine()
        sm.subscribe(lambda o, n: calls.append((o, n)))

        sm.transition(State.LISTENING)
        sm.transition(State.THINKING)
        sm.transition(State.SPEAKING)

        assert calls == [
            (State.IDLE, State.LISTENING),
            (State.LISTENING, State.THINKING),
            (State.THINKING, State.SPEAKING),
        ]

    def test_no_observer_fires_on_invalid_transition(self) -> None:
        calls: list[tuple[State, State]] = []
        sm = StateMachine()
        sm.subscribe(lambda o, n: calls.append((o, n)))

        with pytest.raises(InvalidTransition):
            sm.transition(State.ACTING)

        assert calls == [], "Observer must not be called on rejected transition"

    def test_raising_observer_does_not_corrupt_state(self) -> None:
        """A throwing observer must not prevent state update from being visible."""
        sm = StateMachine()

        def bad_observer(old: State, new: State) -> None:
            raise RuntimeError("observer exploded")

        sm.subscribe(bad_observer)
        # The transition should still complete despite the observer crash
        sm.transition(State.LISTENING)
        assert sm.current == State.LISTENING

    def test_raising_observer_does_not_block_subsequent_observers(self) -> None:
        """After a crashing observer, remaining observers still run."""
        calls: list[str] = []
        sm = StateMachine()

        sm.subscribe(lambda o, n: (_ for _ in ()).throw(RuntimeError("boom")))
        sm.subscribe(lambda o, n: calls.append("ok"))

        sm.transition(State.LISTENING)  # must not raise
        assert "ok" in calls

    def test_unsubscribe_removes_observer(self) -> None:
        calls: list[int] = []
        sm = StateMachine()

        obs = lambda o, n: calls.append(1)  # noqa: E731
        sm.subscribe(obs)
        sm.unsubscribe(obs)

        sm.transition(State.LISTENING)
        assert calls == []

    def test_unsubscribe_returns_false_for_unknown_observer(self) -> None:
        sm = StateMachine()
        result = sm.unsubscribe(lambda o, n: None)
        assert result is False

    def test_unsubscribe_returns_true_when_found(self) -> None:
        sm = StateMachine()
        obs = lambda o, n: None  # noqa: E731
        sm.subscribe(obs)
        assert sm.unsubscribe(obs) is True


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

class TestReset:
    @pytest.mark.parametrize("start_state", list(State))
    def test_reset_from_any_state(self, start_state: State) -> None:
        sm = StateMachine(initial=start_state)
        sm.reset()
        assert sm.current == State.IDLE

    def test_reset_notifies_observers(self) -> None:
        calls: list[tuple[State, State]] = []
        sm = StateMachine(initial=State.SPEAKING)
        sm.subscribe(lambda o, n: calls.append((o, n)))

        sm.reset()

        assert calls == [(State.SPEAKING, State.IDLE)]

    def test_reset_from_idle_is_no_op_state_wise(self) -> None:
        """reset() from IDLE stays IDLE (IDLE -> IDLE is in ALLOWED_TRANSITIONS)."""
        sm = StateMachine()
        sm.reset()
        assert sm.current == State.IDLE


# ---------------------------------------------------------------------------
# ALLOWED_TRANSITIONS completeness
# ---------------------------------------------------------------------------

class TestAllowedTransitions:
    def test_all_documented_base_pairs_are_present(self) -> None:
        """Every pair in the module docstring transition table must be allowed."""
        documented = [
            (State.IDLE, State.LISTENING),
            (State.LISTENING, State.THINKING),
            (State.LISTENING, State.IDLE),
            (State.THINKING, State.SPEAKING),
            (State.THINKING, State.ACTING),
            (State.THINKING, State.IDLE),
            (State.SPEAKING, State.IDLE),
            (State.SPEAKING, State.LISTENING),
            (State.SPEAKING, State.THINKING),
            (State.ACTING, State.THINKING),
            (State.ACTING, State.SPEAKING),
            (State.ACTING, State.IDLE),
        ]
        for pair in documented:
            assert pair in ALLOWED_TRANSITIONS, f"Missing documented transition: {pair}"

    def test_any_state_to_idle_is_allowed(self) -> None:
        """Emergency reset: every state can transition to IDLE."""
        for state in State:
            assert (state, State.IDLE) in ALLOWED_TRANSITIONS, (
                f"Missing reset transition: {state.name} -> IDLE"
            )


# ---------------------------------------------------------------------------
# repr
# ---------------------------------------------------------------------------

class TestRepr:
    def test_repr_includes_state_name(self) -> None:
        sm = StateMachine(initial=State.THINKING)
        assert "THINKING" in repr(sm)

    def test_repr_includes_observer_count(self) -> None:
        sm = StateMachine()
        sm.subscribe(lambda o, n: None)
        sm.subscribe(lambda o, n: None)
        assert "2" in repr(sm)
