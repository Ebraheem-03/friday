"""Conversation/session state machine.

States: IDLE -> LISTENING -> THINKING -> SPEAKING -> ACTING

Transition table (authoritative)
---------------------------------
FROM          TO              Trigger / condition
----          --              -------------------
IDLE          LISTENING       Wake-word detected / push-to-talk pressed
LISTENING     THINKING        VAD end-of-utterance / force-send
LISTENING     IDLE            Timeout / abort
THINKING      SPEAKING        Brain produced a spoken response
THINKING      ACTING          Brain issued a tool-call (OS/web/memory action)
THINKING      IDLE            Abort / error
SPEAKING      IDLE            TTS finished naturally
SPEAKING      LISTENING       Barge-in detected mid-speech
SPEAKING      THINKING        Chained follow-up (self-reply loop guard lives in orchestrator)
ACTING        THINKING        Action completed; brain continues reasoning
ACTING        SPEAKING        Action result rendered directly as speech
ACTING        IDLE            Action aborted / error
ANY           IDLE            Emergency reset (abort/error from any state)

Design notes:
- Pure stdlib, no asyncio (orchestrator wires async scheduling later).
- Observer callbacks are synchronous; a throwing observer is caught-and-logged
  per-observer so a single bad subscriber cannot corrupt machine state.
- StateMachine is NOT thread-safe. If the orchestrator ever calls it from
  multiple threads, wrap with a threading.Lock.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class InvalidTransition(Exception):
    """Raised when a state transition is not permitted.

    Attributes
    ----------
    from_state : State
    to_state : State
    """

    def __init__(self, from_state: "State", to_state: "State") -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Invalid state transition: {from_state.name} -> {to_state.name}"
        )


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class State(enum.Enum):
    """All possible conversation states for the FRIDAY assistant."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ACTING = "acting"


# ---------------------------------------------------------------------------
# Transition guard
# ---------------------------------------------------------------------------

# Allowed transitions expressed as a frozenset of (from, to) pairs.
# IDLE is reachable from ANY state (reset/abort path).
_BASE_TRANSITIONS: frozenset[tuple[State, State]] = frozenset(
    {
        # Normal conversation flow
        (State.IDLE, State.LISTENING),
        (State.LISTENING, State.THINKING),
        # Abort / timeout from LISTENING back to IDLE
        (State.LISTENING, State.IDLE),
        # Brain produced spoken output
        (State.THINKING, State.SPEAKING),
        # Brain issued a non-speech tool-call
        (State.THINKING, State.ACTING),
        # Abort / error while thinking
        (State.THINKING, State.IDLE),
        # TTS finished naturally
        (State.SPEAKING, State.IDLE),
        # Barge-in: user speaks while FRIDAY is talking
        (State.SPEAKING, State.LISTENING),
        # Chained reply: brain generates another response right after speaking
        (State.SPEAKING, State.THINKING),
        # Action completed; brain resumes reasoning
        (State.ACTING, State.THINKING),
        # Action produced direct speech (e.g. "Done. The file was deleted.")
        (State.ACTING, State.SPEAKING),
        # Action aborted / error
        (State.ACTING, State.IDLE),
    }
)

# Build the full allowed set: add ANY -> IDLE for emergency reset
_RESET_TRANSITIONS: frozenset[tuple[State, State]] = frozenset(
    {(s, State.IDLE) for s in State}
)

ALLOWED_TRANSITIONS: frozenset[tuple[State, State]] = _BASE_TRANSITIONS | _RESET_TRANSITIONS


# ---------------------------------------------------------------------------
# Observer type alias
# ---------------------------------------------------------------------------

StateObserver = Callable[[State, State], None]
"""Signature: observer(old_state, new_state) -> None."""


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class StateMachine:
    """Guarded state machine for the FRIDAY conversation lifecycle.

    Parameters
    ----------
    initial : State
        Starting state. Defaults to State.IDLE.

    Example
    -------
    >>> sm = StateMachine()
    >>> sm.transition(State.LISTENING)
    >>> sm.transition(State.THINKING)
    >>> sm.current
    State.THINKING
    """

    def __init__(self, initial: State = State.IDLE) -> None:
        self._state: State = initial
        self._observers: list[StateObserver] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def current(self) -> State:
        """The current state (read-only)."""
        return self._state

    def transition(self, to: State) -> None:
        """Attempt a transition to *to*.

        Raises
        ------
        InvalidTransition
            If the (current, to) pair is not in ALLOWED_TRANSITIONS.
        """
        from_state = self._state
        if (from_state, to) not in ALLOWED_TRANSITIONS:
            raise InvalidTransition(from_state, to)

        self._state = to
        self._notify_observers(from_state, to)

    def reset(self) -> None:
        """Unconditionally reset to IDLE (emergency abort path).

        This is a convenience wrapper around ``transition(State.IDLE)``; since
        ANY -> IDLE is always allowed, it never raises InvalidTransition.
        """
        self.transition(State.IDLE)

    # ------------------------------------------------------------------
    # Observer management
    # ------------------------------------------------------------------

    def subscribe(self, observer: StateObserver) -> None:
        """Register a callback invoked on every successful state transition.

        The callback receives ``(old_state, new_state)`` as positional args.
        Registering the same callable twice results in it being called twice —
        callers are responsible for deduplication.
        """
        self._observers.append(observer)

    def unsubscribe(self, observer: StateObserver) -> bool:
        """Remove the first matching observer registration.

        Returns True if the observer was found and removed, False otherwise.
        """
        try:
            self._observers.remove(observer)
            return True
        except ValueError:
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _notify_observers(self, old: State, new: State) -> None:
        """Call every registered observer; catch-and-log any exception.

        A misbehaving observer must not prevent the state transition from being
        visible to subsequent callers — the state has already been updated
        before this method is called.
        """
        for obs in self._observers:
            try:
                obs(old, new)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Observer %r raised during transition %s -> %s; continuing.",
                    obs,
                    old.name,
                    new.name,
                )

    def __repr__(self) -> str:
        return f"StateMachine(current={self._state.name}, observers={len(self._observers)})"
