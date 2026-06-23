"""FRIDAY application entrypoint — wires every subsystem into one running assistant.

Layer 1: build_app(settings) -> App
    Pure construction + wiring.  No I/O, no sockets, no subprocess, no preloads.
    Returns an App dataclass holding references to every live component.
    Fully testable in CI with only the [dev] extra (voice/hud extras not required).

Layer 2: async run(app, settings)
    The live loop.  Pays model preload costs, starts telemetry, optionally spawns
    the Electron HUD subprocess, runs the capture→STT→brain→TTS pipeline.

Layer 3: main()
    Sync entrypoint (``friday = "core.app:main"`` in [project.scripts]).

Orchestrator→speak_stream bridge design
-----------------------------------------
The orchestrator emits text deltas to registered callbacks and places a ``_DONE``
sentinel in its output queue when a brain turn finishes.  To feed ``speak_stream``
we need an ``AsyncIterator[str]`` that terminates cleanly at turn boundaries.

Design chosen: per-turn ``asyncio.Queue`` fed by an output callback.

    1. Before calling ``orchestrator.send(text)``, we put a fresh per-turn
       ``asyncio.Queue`` in place.
    2. A one-shot callback (registered via ``subscribe``) forwards every delta to
       that queue, then puts a ``None`` sentinel when the turn ends.
    3. ``_queue_to_iter(q)`` wraps the queue as an ``AsyncIterator[str]``.
    4. ``speak_stream(text_stream=_queue_to_iter(q), ...)`` consumes the iterator.

Turn boundary detection: the orchestrator already transitions SPEAKING→IDLE and
puts ``_DONE`` in the output queue at turn end.  Rather than peek at ``_DONE``
(a private sentinel), we rely on state transitions: ``subscribe_state`` fires
``SPEAKING → IDLE`` at turn end.  We use a pair of events:

  - ``_turn_q``  — per-turn queue holding ``str | None``
  - ``_turn_state_cb`` registered via ``subscribe_state`` detects SPEAKING→IDLE
    and puts ``None`` into the queue.

This is clean because:
  - No peek into the orchestrator's private ``_DONE``.
  - The queue's ``None`` sentinel terminates the async generator exactly once.
  - A new queue is created per turn so there is no cross-turn contamination.
  - Tested independently of hardware in unit tests.

Barge-in wiring
---------------
``tts_active`` and ``barge_in_event`` are module-level ``asyncio.Event`` objects
created once.  The SAME two objects are passed to BOTH ``capture_loop`` and
``speak_stream``.  capture.py sets ``barge_in_event`` when speech onset fires
while ``tts_active`` is set.  stream.py clears ``barge_in_event`` at the start
of each turn and checks it inside ``_play_worker``.

RMS → audio_level
-----------------
Each PCM frame from ``capture_loop`` is yielded as int16 bytes.  We compute a
normalised RMS via numpy:

    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(samples ** 2)))

Range is 0.0–1.0.  ``telemetry.set_audio_level(rms)`` is cheap (a single float
assignment) and runs in the main async loop without executor overhead.

HUD subprocess guard
--------------------
The Electron HUD is started with ``subprocess.Popen(['npm', 'run', 'start'], cwd=hud_dir)``.
Guard layers:
  1. ``FRIDAY_NO_HUD=1`` env var or ``settings``-level flag disables it entirely.
  2. ``npm`` binary must exist on PATH; electron is installed in hud/node_modules.
  3. ``hud/`` directory must exist.
  4. Any ``OSError`` / ``FileNotFoundError`` during Popen is caught; a warning is
     logged and the assistant continues headless.
The HUD process inherits no secrets via argv (env vars are filtered; the process
gets ``os.environ`` minus ``GEMINI_API_KEY``).
The process is terminated (SIGTERM + wait) during clean shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from core.brain import Brain
from core.config import Settings
from core.orchestrator import Orchestrator
from core.state import State
from core.telemetry import TelemetryServer
from memory.recall import Memory
from core.os_bridge import OsBridge
from web.agent import WebAgent

logger = logging.getLogger(__name__)

# Path to the HUD directory (sibling of the repo root, next to core/).
_HUD_DIR: Path = Path(__file__).parent.parent / "hud"


# ---------------------------------------------------------------------------
# App dataclass — holds every wired subsystem reference
# ---------------------------------------------------------------------------


@dataclass
class App:
    """Fully-wired FRIDAY application graph (no I/O yet).

    Fields are set by ``build_app()`` and consumed by ``run()``.  Tests inspect
    these fields to assert the wiring is correct.
    """

    memory: Memory
    os_bridge: OsBridge
    web_agent: WebAgent
    telemetry: TelemetryServer
    brain: Brain
    orchestrator: Orchestrator
    settings: Settings


# ---------------------------------------------------------------------------
# Console confirm callback for gated OS actions (run_command)
# ---------------------------------------------------------------------------


def _console_confirm(description: str) -> bool:
    """Prompt the operator on the console to approve a gated OS action.

    Wired into ``OsBridge`` for ``run_command`` (which stays gated even in
    trusted mode). Returns True only on an explicit 'y'/'yes'.

    NOTE: this is a blocking ``input()`` call — it intentionally pauses the
    assistant until the human answers, because executing an arbitrary command
    warrants deliberate attention. The description is the redacted action label
    (binary name only; never the full argv — see OsBridge M-2 hardening), so no
    secret in an argument is printed here. A non-interactive stdin (EOF) is
    treated as a denial (fail-safe). A richer HUD-based async confirm is a
    tracked follow-up.
    """
    try:
        answer = input(f"\n[FRIDAY] Approve OS action — {description}? [y/N] ")
    except EOFError:
        logger.warning("Console confirm: no interactive stdin — denying.")
        return False
    return answer.strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------
# Layer 1: build_app — pure construction, no I/O
# ---------------------------------------------------------------------------


def build_app(settings: Settings) -> App:
    """Construct and wire every FRIDAY subsystem.  Performs NO I/O.

    This function is intentionally side-effect-free:
    - No network calls (no Gemini handshake, no socket bind).
    - No model preloads (no voice module imported here — lazy imports inside run()).
    - No subprocess spawned.
    - No audio device opened.

    The returned ``App`` object is a pure dependency graph ready for
    ``run()`` to bring alive.

    Parameters
    ----------
    settings:
        Validated runtime configuration (from ``Settings.from_env()``).

    Returns
    -------
    App
        Fully wired application graph.
    """
    logger.debug("build_app: constructing subsystems.")

    # --- Memory (mnemo) -------------------------------------------------------
    memory = Memory()

    # --- OS bridge (vector): live execution, trusted synthetic input + ------
    # confirmed commands (locked human decision).
    #   dry_run=False        → actions actually execute (not simulated).
    #   trusted=True         → click/type run freely (hands-free synthetic input).
    #   confirm_callback=... → run_command (always gated even when trusted) asks
    #                          for an explicit console y/N before executing.
    os_bridge = OsBridge(
        dry_run=False,
        trusted=True,
        confirm_callback=_console_confirm,
    )

    # --- Web agent (scout): allow_destructive=True (locked human decision) --
    web_agent = WebAgent(settings, allow_destructive=True)

    # --- Telemetry server (halo) ----------------------------------------------
    telemetry = TelemetryServer(settings)

    # --- Brain (Gemini) — real tools wired (NOT the no-op stubs) ------------
    brain = Brain(
        settings,
        memory=memory,
        os_tool=os_bridge,
        web_tool=web_agent,
    )

    # --- Orchestrator — brain pre-wired; orchestrator does NOT re-create it --
    orchestrator = Orchestrator(brain=brain)

    # --- Subscribe telemetry to state changes so HUD reflects FRIDAY state ---
    orchestrator.subscribe_state(telemetry.on_state_change)

    logger.debug(
        "build_app: wiring complete — memory=%r, os_bridge=%r, web_agent=%r, "
        "telemetry=%r, brain=%r, orchestrator=%r",
        type(memory).__name__,
        type(os_bridge).__name__,
        type(web_agent).__name__,
        type(telemetry).__name__,
        type(brain).__name__,
        type(orchestrator).__name__,
    )

    return App(
        memory=memory,
        os_bridge=os_bridge,
        web_agent=web_agent,
        telemetry=telemetry,
        brain=brain,
        orchestrator=orchestrator,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Turn-stream bridge: orchestrator deltas → AsyncIterator[str]
# ---------------------------------------------------------------------------


async def _queue_to_iter(q: asyncio.Queue[str | None]) -> AsyncIterator[str]:
    """Drain *q* as an async iterator, terminating on the ``None`` sentinel.

    Used to bridge the orchestrator's per-callback delta stream into the
    ``AsyncIterator[str]`` that ``speak_stream`` consumes.

    Latency note: each ``await q.get()`` blocks until the brain worker puts
    the next delta (or None) into the queue.  The chunker in stream.py buffers
    these in its own queue so TTS can overlap with brain output.
    """
    while True:
        item = await q.get()
        if item is None:
            return
        yield item


def _make_turn_bridge(
    orchestrator: Orchestrator,
) -> "asyncio.Queue[str | None]":
    """Create a fresh per-turn queue and wire it as a one-shot subscriber pair.

    Returns
    -------
    asyncio.Queue[str | None]
        A queue whose items are text deltas from the current brain turn.
        Use ``_queue_to_iter(returned_q)`` as the ``text_stream`` argument to
        ``speak_stream``.  A ``None`` sentinel is placed in the queue when the
        turn ends (SPEAKING→IDLE state transition), terminating the iterator.

    Wiring
    ------
    1. A fresh ``asyncio.Queue[str | None]`` is created.
    2. An output callback forwards every delta to the queue.
    3. A state observer watches for SPEAKING→IDLE (turn end) and puts ``None``
       (the termination sentinel) into the queue, then unregisters both callbacks.

    This function is intentionally cheap (no I/O) so it can be called in a
    tight loop per turn.
    """
    text_q: asyncio.Queue[str | None] = asyncio.Queue()
    done_sent = [False]  # mutable flag — accessed by both closures

    def _on_delta(delta: str) -> None:
        """Forward each text delta to the per-turn queue."""
        try:
            text_q.put_nowait(delta)
        except asyncio.QueueFull:
            # Queue is unbounded (default) so this should never happen,
            # but guard defensively.
            logger.warning("Turn text queue full — dropping delta.")

    def _on_state(old: State, new: State) -> None:
        """Detect SPEAKING→IDLE (turn end) and terminate the iterator."""
        if new == State.IDLE and old == State.SPEAKING and not done_sent[0]:
            done_sent[0] = True
            try:
                text_q.put_nowait(None)  # terminate _queue_to_iter
            except asyncio.QueueFull:
                pass
            # Unregister both callbacks so they don't leak across turns.
            orchestrator.unsubscribe(_on_delta)          # removes from output callbacks
            orchestrator.unsubscribe_state(_on_state)    # removes from state observers

    orchestrator.subscribe(_on_delta)
    orchestrator.subscribe_state(_on_state)

    return text_q


# ---------------------------------------------------------------------------
# HUD subprocess spawn + guard
# ---------------------------------------------------------------------------


def _spawn_hud(hud_dir: Path) -> subprocess.Popen[bytes] | None:
    """Attempt to spawn the Electron HUD subprocess.

    Guard layers (all failures are non-fatal — FRIDAY continues headless):
    1. ``hud_dir`` must exist.
    2. ``npm`` must be on PATH.
    3. ``Popen`` must not raise ``OSError`` / ``FileNotFoundError``.

    The HUD process inherits a filtered environment: ``GEMINI_API_KEY`` is
    stripped from the child's env so the secret is never reachable via
    ``/proc/<pid>/environ`` from the HUD process.

    Returns
    -------
    subprocess.Popen or None
        The running process, or None if launch failed (warning already logged).
    """
    if not hud_dir.is_dir():
        logger.warning(
            "HUD directory not found at %s — running headless (no HUD).", hud_dir
        )
        return None

    import shutil  # stdlib; available everywhere

    npm = shutil.which("npm")
    if npm is None:
        logger.warning("npm not found on PATH — running headless (no HUD).")
        return None

    # Strip the Gemini API key from the child environment so the HUD process
    # cannot access it via /proc/<pid>/environ.
    child_env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}

    try:
        proc = subprocess.Popen(
            [npm, "run", "start"],
            cwd=str(hud_dir),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Isolate from our SIGINT
        )
        logger.info("HUD subprocess started (pid=%d).", proc.pid)
        return proc
    except (OSError, FileNotFoundError) as exc:
        logger.warning("HUD subprocess failed to start: %s — continuing headless.", exc)
        return None


def _terminate_hud(proc: subprocess.Popen[bytes] | None) -> None:
    """Gracefully terminate the HUD subprocess.  Idempotent."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        logger.info("HUD subprocess terminated.")
    except OSError:
        pass  # already dead


# ---------------------------------------------------------------------------
# Layer 2: run — the live async loop
# ---------------------------------------------------------------------------


async def run(app: App, settings: Settings) -> None:
    """Run the FRIDAY live pipeline.  Not CI-tested (hardware loop).

    Boot sequence
    -------------
    1. Preload TTS and STT models (pays the cold-start cost before first turn).
    2. Start the telemetry WebSocket server.
    3. Optionally spawn the Electron HUD (disabled by FRIDAY_NO_HUD=1 or absence
       of npm/hud dir).
    4. Start the orchestrator (brain worker + output consumer background tasks).
    5. Enter the capture→STT→brain→TTS loop.

    Shutdown (CancelledError / KeyboardInterrupt)
    --------------------------------------------
    - Orchestrator.stop() — stops brain worker and output consumer.
    - TelemetryServer.stop() — closes WebSocket server.
    - HUD process terminated (SIGTERM + wait).
    - Capture loop cancellation propagates naturally (CancelledError in the
      async-for loop exits the generator).
    All shutdown steps are idempotent; no traceback spew on clean exit.

    Parameters
    ----------
    app:
        Fully wired App from ``build_app()``.
    settings:
        Runtime settings (used for voice params and HUD flag).
    """
    # Lazy-import voice modules here so build_app/core/app.py imports clean
    # in CI without the [voice] extra installed.
    from voice import tts_kokoro, stt
    from voice.stream import speak_stream
    from voice.capture import capture_loop

    hud_proc: subprocess.Popen[bytes] | None = None

    # Barge-in events: shared between capture_loop and speak_stream.
    tts_active = asyncio.Event()
    barge_in_event = asyncio.Event()

    try:
        # -- 1. Preload models -------------------------------------------------
        logger.info("Preloading TTS and STT models (pays cold-start cost) ...")
        await tts_kokoro.preload(settings.tts_voice)
        await stt.preload(settings.stt_model)
        logger.info("Model preload complete.")

        # -- 2. Start telemetry -----------------------------------------------
        await app.telemetry.start()

        # -- 3. Optionally spawn HUD ------------------------------------------
        no_hud = os.environ.get("FRIDAY_NO_HUD", "").strip() not in ("", "0")
        if not no_hud:
            hud_proc = _spawn_hud(_HUD_DIR)
        else:
            logger.info("HUD disabled (FRIDAY_NO_HUD is set).")

        # -- 4. Start orchestrator --------------------------------------------
        # Make the OS-execution posture explicit at boot (live, not simulated).
        logger.warning(
            "OS control is LIVE: click/type execute automatically (trusted); "
            "run_command requires console y/N approval. Web agent autonomy is ON "
            "(allow_destructive=True, SSRF-filtered + timed out)."
        )
        await app.orchestrator.start()

        # -- 5. Capture→STT→brain→TTS loop ------------------------------------
        logger.info("FRIDAY ready. Listening ...")

        try:
            import numpy as np

            async for pcm in capture_loop(
                tts_active=tts_active,
                barge_in_event=barge_in_event,
                sample_rate=16_000,
            ):
                # --- RMS → audio_level (drives HUD rings) --------------------
                # pcm is raw int16 little-endian bytes from capture_loop.
                # Normalise to float32 [-1, 1] and compute RMS.
                samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(samples ** 2)))
                app.telemetry.set_audio_level(rms)

                # --- STT: PCM bytes → transcript text -----------------------
                text = await stt.transcribe(pcm)
                if not text.strip():
                    continue  # silence or noise — skip

                # --- Brain: wire per-turn text queue ------------------------
                # Create a fresh queue + callbacks for this turn.
                text_q = _make_turn_bridge(app.orchestrator)

                # Enqueue user text to the orchestrator (brain worker picks it up).
                await app.orchestrator.send(text)

                # --- TTS: speak the brain's response -----------------------
                # speak_stream consumes _queue_to_iter(text_q) until the None
                # sentinel (which _on_state puts in when SPEAKING→IDLE fires).
                await speak_stream(
                    _queue_to_iter(text_q),
                    voice=settings.tts_voice,
                    sample_rate=settings.sample_rate,
                    tts_active=tts_active,
                    barge_in_event=barge_in_event,
                )

        except asyncio.CancelledError:
            logger.info("Capture loop cancelled — shutting down.")
            raise

    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("FRIDAY shutdown requested.")
    finally:
        # --- Clean shutdown (idempotent, no traceback spew) -----------------
        logger.info("Stopping orchestrator ...")
        await app.orchestrator.stop()

        logger.info("Stopping telemetry server ...")
        await app.telemetry.stop()

        logger.info("Terminating HUD subprocess ...")
        _terminate_hud(hud_proc)

        logger.info("FRIDAY shut down cleanly.")


# ---------------------------------------------------------------------------
# Layer 3: main — sync entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Synchronous CLI entrypoint: ``friday`` command.

    Loads settings, builds the app graph, and runs the async live loop.
    KeyboardInterrupt (Ctrl-C) exits cleanly with no traceback.
    """
    import sys

    # Logging setup — must happen before any module-level logger calls.
    # Import here (not at module top) to keep the import side-effect local.
    try:
        settings = Settings.from_env()
    except Exception as exc:  # ConfigError or anything else
        # Use print here because logging may not be configured yet.
        print(f"[FRIDAY] Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )
    logger.info("FRIDAY starting. %r", settings)

    app = build_app(settings)

    try:
        asyncio.run(run(app, settings))
    except KeyboardInterrupt:
        # asyncio.run() propagates KeyboardInterrupt after clean loop shutdown.
        # Suppress the traceback — the user pressed Ctrl-C intentionally.
        pass
