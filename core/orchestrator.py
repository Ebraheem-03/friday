"""Async supervisor wiring voice <-> brain <-> memory <-> os/web.

Architecture
------------
Text flows through two bounded asyncio.Queues:

    [input producer]  -->  input_q  -->  [brain worker]
                                              |
                                        output_q
                                              |
                                     [output consumer]

Backpressure: both queues have a fixed maxsize. If the output consumer is
slower than the brain (e.g. TTS is busy), output_q fills up and the brain
worker blocks on put(), naturally pacing the input side.

State machine
-------------
The StateMachine from core.state is driven as text flows:
    IDLE -> LISTENING  (input arrives in input_q)
    LISTENING -> THINKING  (brain worker starts)
    THINKING -> SPEAKING   (first text chunk yielded to output_q)
    SPEAKING -> IDLE       (output consumer signals completion)
    THINKING -> IDLE       (on error/abort)

Graceful shutdown
-----------------
Call `orchestrator.stop()` from any coroutine. It:
  1. Sets a stop event so the input loop can exit cleanly.
  2. Cancels the brain worker task and output consumer task.
  3. Drains both queues (no orphaned items).
  4. Awaits all cancelled tasks, suppressing CancelledError.

Voice/audio is wired in Step 4 (echo). For now, callers enqueue plain text
via `orchestrator.send(user_text)` and consume results via
`orchestrator.subscribe(callback)`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from core.brain import Brain, ConversationHistory
from core.config import Settings
from core.state import State, StateMachine
from memory.recall import Memory

logger = logging.getLogger(__name__)

# Sentinel object placed in the output queue to signal end-of-response.
_DONE = object()

# Default queue depth. Shallow queues keep latency low and apply early backpressure.
_QUEUE_MAXSIZE = 8


# ---------------------------------------------------------------------------
# Output callback type
# ---------------------------------------------------------------------------

OutputCallback = Callable[[str], Awaitable[None] | None]
"""Async or sync callback invoked for each text delta arriving from the brain."""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """Async supervisor that pipelines user text through the Brain.

    Parameters
    ----------
    settings:
        Typed runtime config (passed through to Brain if *brain* is None).
    brain:
        A pre-constructed Brain instance. If None, one is created from *settings*.
        Providing a pre-built brain makes the orchestrator fully testable with a
        mock brain.
    input_maxsize:
        Depth of the input queue. Default: _QUEUE_MAXSIZE.
    output_maxsize:
        Depth of the output queue. Default: _QUEUE_MAXSIZE.

    Example
    -------
    >>> async def on_text(delta: str) -> None:
    ...     print(delta, end="", flush=True)
    >>> orch = Orchestrator(settings=cfg)
    >>> orch.subscribe(on_text)
    >>> async with orch:
    ...     await orch.send("Hello!")
    ...     await asyncio.sleep(5)  # let brain respond
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        brain: Brain | None = None,
        memory: Memory | None = None,
        input_maxsize: int = _QUEUE_MAXSIZE,
        output_maxsize: int = _QUEUE_MAXSIZE,
    ) -> None:
        if brain is None:
            if settings is None:
                raise ValueError("Either 'settings' or 'brain' must be provided.")
            # Persistent memory: use the injected instance, else open the default
            # SQLite store (~/.local/share/friday/memory.db). Only constructed on
            # the real-boot path — callers injecting a Brain manage their own.
            if memory is None:
                memory = Memory()
                self._owns_memory = True
            else:
                self._owns_memory = False
            self._memory: Memory | None = memory
            brain = Brain(settings, memory=memory)
        else:
            self._memory = memory
            self._owns_memory = False
        self._brain = brain
        self._sm = StateMachine()
        self._input_q: asyncio.Queue[str] = asyncio.Queue(maxsize=input_maxsize)
        self._output_q: asyncio.Queue[str | object] = asyncio.Queue(maxsize=output_maxsize)
        self._callbacks: list[OutputCallback] = []
        self._stop_event = asyncio.Event()
        self._history: ConversationHistory = []

        # Background task handles — set when started.
        self._brain_task: asyncio.Task[None] | None = None
        self._consumer_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle (async context manager)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "Orchestrator":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def start(self) -> None:
        """Start the brain worker and output consumer background tasks."""
        logger.info("Orchestrator starting.")
        self._stop_event.clear()
        self._brain_task = asyncio.create_task(
            self._brain_worker(), name="brain-worker"
        )
        self._consumer_task = asyncio.create_task(
            self._output_consumer(), name="output-consumer"
        )

    async def stop(self) -> None:
        """Signal shutdown and wait for background tasks to finish cleanly.

        Safe to call multiple times; idempotent after the first call.
        """
        logger.info("Orchestrator stopping.")
        self._stop_event.set()

        tasks = [t for t in (self._brain_task, self._consumer_task) if t is not None]
        for task in tasks:
            task.cancel()

        # Suppress CancelledError from each task.
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Drain queues so no items are orphaned.
        self._drain(self._input_q)
        self._drain(self._output_q)

        self._brain_task = None
        self._consumer_task = None

        # Close the memory store only if we opened it ourselves.
        if self._owns_memory and self._memory is not None:
            self._memory.close()
            self._memory = None

        logger.info("Orchestrator stopped.")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def send(self, user_text: str) -> None:
        """Enqueue *user_text* for the brain to process.

        Applies backpressure: blocks if input_q is full (i.e. brain is behind).
        Raises RuntimeError if orchestrator has not been started.
        """
        if self._brain_task is None:
            raise RuntimeError("Orchestrator is not running. Call start() first.")
        await self._input_q.put(user_text)

    def subscribe(self, callback: OutputCallback) -> None:
        """Register *callback* to receive text deltas from the brain.

        The callback is invoked with each text chunk as it arrives. It may be
        a plain sync function (str -> None) or an async coroutine function
        (str -> Awaitable[None]). Multiple callbacks are supported.
        """
        self._callbacks.append(callback)

    def unsubscribe(self, callback: OutputCallback) -> bool:
        """Remove a previously registered callback. Returns True if found."""
        try:
            self._callbacks.remove(callback)
            return True
        except ValueError:
            return False

    @property
    def state(self) -> State:
        """Current state machine state (read-only)."""
        return self._sm.current

    def subscribe_state(self, observer: Callable[[State, State], None]) -> None:
        """Register a state-change observer (forwarded to StateMachine)."""
        self._sm.subscribe(observer)

    def unsubscribe_state(self, observer: Callable[[State, State], None]) -> bool:
        """Remove a previously registered state observer. Returns True if found."""
        return self._sm.unsubscribe(observer)

    # ------------------------------------------------------------------
    # Background workers
    # ------------------------------------------------------------------

    async def _brain_worker(self) -> None:
        """Pull user text from input_q, call Brain.stream, push deltas to output_q.

        State transitions:
            IDLE -> LISTENING  (item dequeued from input_q)
            LISTENING -> THINKING  (brain.stream called)
            THINKING -> SPEAKING   (first delta put to output_q)
            SPEAKING -> IDLE       (DONE sentinel put to output_q)
            THINKING -> IDLE       (on any exception)
        """
        logger.debug("Brain worker started.")
        while not self._stop_event.is_set():
            try:
                # Wait for input with a short timeout so the stop_event is polled.
                user_text = await asyncio.wait_for(
                    self._input_q.get(), timeout=0.1
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                logger.debug("Brain worker cancelled during input wait.")
                raise

            self._sm.transition(State.LISTENING)
            logger.debug("Brain worker got input: %r", user_text[:60])

            try:
                self._sm.transition(State.THINKING)
                first_chunk = True
                async for delta in self._brain.stream(user_text, self._history):
                    if first_chunk:
                        self._sm.transition(State.SPEAKING)
                        first_chunk = False
                    # Backpressure: if output_q is full, this blocks.
                    await self._output_q.put(delta)

                # If the model returned nothing (edge case), skip SPEAKING.
                if first_chunk:
                    # No chunks were yielded — transition directly to IDLE.
                    self._sm.transition(State.IDLE)
                else:
                    # Signal end of this response.
                    await self._output_q.put(_DONE)

            except asyncio.CancelledError:
                logger.debug("Brain worker cancelled during stream.")
                self._sm.reset()
                raise
            except Exception:
                logger.exception("Brain worker error; resetting to IDLE.")
                self._sm.reset()
            finally:
                self._input_q.task_done()

        logger.debug("Brain worker exiting (stop_event set).")

    async def _output_consumer(self) -> None:
        """Pull deltas from output_q and dispatch to registered callbacks.

        The DONE sentinel ends the current utterance and resets state to IDLE.
        """
        logger.debug("Output consumer started.")
        while not self._stop_event.is_set():
            try:
                item = await asyncio.wait_for(
                    self._output_q.get(), timeout=0.1
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                logger.debug("Output consumer cancelled during wait.")
                raise

            try:
                if item is _DONE:
                    # End of response — transition SPEAKING -> IDLE.
                    if self._sm.current == State.SPEAKING:
                        self._sm.transition(State.IDLE)
                else:
                    # Dispatch text delta to all subscribers.
                    delta: str = item  # type: ignore[assignment]
                    for cb in self._callbacks:
                        try:
                            result = cb(delta)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception:
                            logger.exception(
                                "Output callback %r raised; continuing.", cb
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Output consumer error; continuing.")
            finally:
                self._output_q.task_done()

        logger.debug("Output consumer exiting (stop_event set).")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _drain(q: asyncio.Queue) -> None:  # type: ignore[type-arg]
        """Remove all items from *q* without blocking."""
        while not q.empty():
            try:
                q.get_nowait()
                q.task_done()
            except asyncio.QueueEmpty:
                break

    def __repr__(self) -> str:
        return (
            f"Orchestrator(state={self._sm.current.name}, "
            f"input_q={self._input_q.qsize()}/{self._input_q.maxsize}, "
            f"output_q={self._output_q.qsize()}/{self._output_q.maxsize})"
        )
