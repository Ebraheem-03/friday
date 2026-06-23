"""WebSocket telemetry server — real-time HUD data source.

This module is the Python-side of the FRIDAY HUD telemetry pipeline (Step 8a).
The Electron/React HUD (Step 8b) connects to this server over localhost WebSocket
and consumes JSON frames at a fixed cadence.

Runtime purity (CLAUDE.md §0)
------------------------------
No MCP in this file. websockets and psutil are runtime deps in the [hud] extra.
All imports of both libraries are *lazy* (inside methods, guarded by
try/except ImportError) so this module is importable in CI where the [hud]
extra is NOT installed. If a required library is absent the server logs a clear
error and degrades gracefully — it never raises on import.

Security (CLAUDE.md §3)
------------------------
- The server ALWAYS binds to 127.0.0.1 (localhost) ONLY. It must never be
  changed to 0.0.0.0 or any external interface. FRIDAY controls the host
  machine; exposing this socket externally would allow any process on the
  network to inject/consume state + resource telemetry.
- The broadcast frame contains ONLY: timestamp, state string, cpu_pct,
  ram_pct, audio_level. It NEVER carries screen captures, audio buffers,
  file paths, environment variables, secrets, or any user-identifiable data.
- Client connections are logged at DEBUG level with client count only (no IP
  address, no cookies, no headers).

Frame schema (the 8b contract — do NOT change field names without a new ADR)
------------------------------------------------------------------------------
Every broadcast is a single JSON object:

    {
        "ts":          float,   # epoch seconds (time.time())
        "state":       str,     # one of: "idle","listening","thinking","speaking","acting"
        "cpu_pct":     float,   # 0.0–100.0, sampled via psutil.cpu_percent(None)
        "ram_pct":     float,   # 0.0–100.0, psutil.virtual_memory().percent
        "audio_level": float    # 0.0–1.0, set by voice pipeline via set_audio_level()
    }

TypedDict mirror (importable without [hud] installed — pure stdlib)
-------------------------------------------------------------------
See :class:`TelemetryFrame`.

Integration TODOs (Step 9 — handled by atlas, NOT this file)
-------------------------------------------------------------
    # TODO(atlas): start TelemetryServer at app boot + subscribe to StateMachine
    # TODO(atlas): wire voice pipeline's RMS output to TelemetryServer.set_audio_level()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, TypedDict

from core.config import Settings
from core.state import State

if TYPE_CHECKING:
    # These are runtime-only; the TYPE_CHECKING guard keeps them out of normal
    # import resolution so the module loads cleanly without [hud] installed.
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bind address constant — do NOT change to 0.0.0.0 (see security note above)
# ---------------------------------------------------------------------------

_BIND_HOST: str = "127.0.0.1"

# Default broadcast interval — 10 Hz.
_DEFAULT_INTERVAL: float = 0.1

# Audio level clamp bounds
_AUDIO_MIN: float = 0.0
_AUDIO_MAX: float = 1.0


# ---------------------------------------------------------------------------
# Public frame schema (TypedDict — stdlib only, safe to import anywhere)
# ---------------------------------------------------------------------------


class TelemetryFrame(TypedDict):
    """Typed representation of a single HUD telemetry broadcast frame.

    This is the wire contract between Step 8a (Python server) and Step 8b
    (Electron/React client). Field names are stable; do not rename without
    updating the HUD client and filing a new ADR.

    Fields
    ------
    ts : float
        UTC epoch seconds (``time.time()``).
    state : str
        Current FRIDAY state string. One of: ``"idle"``, ``"listening"``,
        ``"thinking"``, ``"speaking"``, ``"acting"``.
    cpu_pct : float
        Host CPU usage percentage in the range 0.0–100.0.
    ram_pct : float
        Host RAM usage percentage in the range 0.0–100.0.
    audio_level : float
        Normalised audio energy level in the range 0.0–1.0.  Provided by
        the voice pipeline via :meth:`TelemetryServer.set_audio_level`.
        Defaults to 0.0 until the voice pipeline is wired in Step 9.
    """

    ts: float
    state: str
    cpu_pct: float
    ram_pct: float
    audio_level: float


# ---------------------------------------------------------------------------
# TelemetryServer
# ---------------------------------------------------------------------------


class TelemetryServer:
    """Async WebSocket server that broadcasts HUD telemetry frames to all clients.

    Parameters
    ----------
    settings : Settings
        Runtime config; the server binds to ``settings.telemetry_ws_port``.
    interval : float
        Broadcast cadence in seconds. Default: 0.1 (10 Hz).

    Lifecycle
    ---------
    Start the server with :meth:`start` and stop it with :meth:`stop`.
    Both methods are idempotent and safe to call from an asyncio event loop.
    An async context manager is also available::

        async with TelemetryServer(settings) as srv:
            ...

    State integration
    -----------------
    Register as a StateMachine observer::

        state_machine.subscribe(server.on_state_change)

    Or call :meth:`set_state` directly.

    Audio level
    -----------
    The voice pipeline (echo, wired at Step 9) calls
    ``server.set_audio_level(rms)`` to push normalised energy values.

    Example
    -------
    >>> srv = TelemetryServer(settings)
    >>> await srv.start()
    >>> srv.set_state(State.LISTENING)
    >>> srv.set_audio_level(0.42)
    >>> await srv.stop()
    """

    def __init__(
        self,
        settings: Settings,
        *,
        interval: float = _DEFAULT_INTERVAL,
    ) -> None:
        self._port: int = settings.telemetry_ws_port
        self._interval: float = interval

        # Mutable telemetry state — updated by setters / observer.
        self._state_str: str = State.IDLE.value
        self._audio_level: float = 0.0

        # Connected websocket clients.
        self._clients: set[object] = set()

        # Background task handles.
        self._server: object | None = None          # websockets server object
        self._broadcast_task: asyncio.Task[None] | None = None

        # Whether the [hud] deps are available — resolved lazily on first start.
        self._deps_ok: bool | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the WebSocket server and the broadcast loop.

        Binds to 127.0.0.1 on ``settings.telemetry_ws_port``. Safe to call
        multiple times — a second call while already running is a no-op.

        If the [hud] extra (websockets + psutil) is not installed, this method
        logs an error and returns without raising so the rest of FRIDAY boots.
        """
        if self._broadcast_task is not None:
            logger.debug("TelemetryServer.start() called while already running — no-op.")
            return

        if not self._check_deps():
            return

        try:
            websockets = self._import_websockets()
        except ImportError:
            # _check_deps should have caught this; belt-and-suspenders.
            logger.error(
                "TelemetryServer: websockets not available. HUD telemetry disabled."
            )
            return

        # Prime the psutil CPU sampler so the first non-blocking read is valid.
        self._prime_cpu_sampler()

        logger.info(
            "TelemetryServer starting on ws://%s:%d (interval=%.3fs).",
            _BIND_HOST,
            self._port,
            self._interval,
        )

        self._server = await websockets.serve(
            self._handle_client,
            _BIND_HOST,
            self._port,
        )

        self._broadcast_task = asyncio.create_task(
            self._broadcast_loop(), name="telemetry-broadcast"
        )

    async def stop(self) -> None:
        """Shut down the broadcast loop and close all client connections.

        Safe to call multiple times — idempotent after the first call.
        """
        if self._broadcast_task is None and self._server is None:
            return

        logger.info("TelemetryServer stopping.")

        if self._broadcast_task is not None:
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except asyncio.CancelledError:
                pass
            self._broadcast_task = None

        if self._server is not None:
            self._server.close()  # type: ignore[union-attr]
            try:
                await self._server.wait_closed()  # type: ignore[union-attr]
            except Exception:
                pass
            self._server = None

        self._clients.clear()
        logger.info("TelemetryServer stopped.")

    async def __aenter__(self) -> "TelemetryServer":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Public setters — called by StateMachine observer and voice pipeline
    # ------------------------------------------------------------------

    def set_state(self, state: State) -> None:
        """Update the state string that is included in the next broadcast frame.

        Parameters
        ----------
        state : State
            The new state. The string value (``state.value``) is broadcast.
        """
        self._state_str = state.value
        logger.debug("TelemetryServer state -> %s", self._state_str)

    def on_state_change(self, old_state: State, new_state: State) -> None:
        """StateMachine observer callback — signature matches StateObserver.

        Register with::

            state_machine.subscribe(server.on_state_change)

        Parameters
        ----------
        old_state : State
            Previous state (unused; included to match StateObserver signature).
        new_state : State
            State being transitioned into.
        """
        # old_state is intentionally unused — we only need the new value.
        _ = old_state
        self.set_state(new_state)

    def set_audio_level(self, level: float) -> None:
        """Set the normalised audio energy level (0.0–1.0).

        Values outside [0.0, 1.0] are clamped. The voice pipeline (echo)
        calls this with each VAD RMS sample. In Step 8a there is no audio
        source — the default is 0.0 until Step 9 wires the voice pipeline.

        Parameters
        ----------
        level : float
            Normalised audio energy. Clamped to [0.0, 1.0].
        """
        self._audio_level = max(_AUDIO_MIN, min(_AUDIO_MAX, float(level)))

    # ------------------------------------------------------------------
    # Internal: client handler
    # ------------------------------------------------------------------

    async def _handle_client(self, websocket: object) -> None:
        """Accept and track a new WebSocket client connection.

        Called by the websockets library for each new incoming connection.
        The client is added to the _clients set and removed on disconnect.
        """
        self._clients.add(websocket)
        logger.debug(
            "HUD client connected. Total clients: %d", len(self._clients)
        )
        try:
            # Keep the handler alive (and the client tracked) until the
            # connection closes. We only broadcast outbound — we don't
            # consume inbound messages, so we just wait for the close.
            await websocket.wait_closed()  # type: ignore[union-attr]
        finally:
            self._clients.discard(websocket)
            logger.debug(
                "HUD client disconnected. Total clients: %d", len(self._clients)
            )

    # ------------------------------------------------------------------
    # Internal: broadcast loop
    # ------------------------------------------------------------------

    async def _broadcast_loop(self) -> None:
        """Periodically build and send a telemetry frame to all clients.

        Runs at self._interval cadence. A slow or dead client is dropped
        without blocking other clients or crashing the loop. This coroutine
        is run as a background task and is cancelled by stop().
        """
        logger.debug("Telemetry broadcast loop started (interval=%.3fs).", self._interval)
        while True:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                logger.debug("Telemetry broadcast loop cancelled.")
                raise

            frame = self._build_frame()
            payload = json.dumps(frame)

            if self._clients:
                # Fan-out: send to all clients concurrently; drop failures.
                await asyncio.gather(
                    *[self._safe_send(client, payload) for client in list(self._clients)],
                    return_exceptions=True,
                )

    async def _safe_send(self, client: object, payload: str) -> None:
        """Send *payload* to *client*; drop the client if the send fails.

        A failed send means the client has disconnected or is unresponsive.
        We remove it from the set and log at DEBUG — never propagate the
        exception so the broadcast loop and other clients are unaffected.
        """
        try:
            await client.send(payload)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            # Connection reset, broken pipe, etc. — drop the dead client.
            self._clients.discard(client)
            logger.debug("HUD client dropped (send failed: %s). Remaining: %d",
                         type(exc).__name__, len(self._clients))

    # ------------------------------------------------------------------
    # Internal: frame building
    # ------------------------------------------------------------------

    def _build_frame(self) -> TelemetryFrame:
        """Sample current system metrics and build a TelemetryFrame dict.

        CPU is sampled non-blocking (psutil.cpu_percent(None)); it was primed
        once at start() so the first sample is meaningful rather than 0.0.
        """
        cpu_pct, ram_pct = self._sample_psutil()
        return TelemetryFrame(
            ts=time.time(),
            state=self._state_str,
            cpu_pct=cpu_pct,
            ram_pct=ram_pct,
            audio_level=self._audio_level,
        )

    @staticmethod
    def _prime_cpu_sampler() -> None:
        """Call psutil.cpu_percent(interval=None) once to initialise the counter.

        psutil's non-blocking cpu_percent(interval=None) returns 0.0 on the
        very first call because it has no prior sample to compare against.
        Calling it once here means all subsequent calls in the broadcast loop
        return a valid reading without stalling the async loop.
        """
        try:
            import psutil  # noqa: PLC0415 — lazy import by design
            psutil.cpu_percent(interval=None)
        except ImportError:
            pass  # deps missing — _check_deps() will have logged and returned

    @staticmethod
    def _sample_psutil() -> tuple[float, float]:
        """Return (cpu_pct, ram_pct) using psutil; return (0.0, 0.0) on failure."""
        try:
            import psutil  # noqa: PLC0415 — lazy import by design
            cpu = float(psutil.cpu_percent(interval=None))
            ram = float(psutil.virtual_memory().percent)
            return cpu, ram
        except ImportError:
            return 0.0, 0.0
        except Exception as exc:  # noqa: BLE001
            logger.debug("psutil sample failed: %s", exc)
            return 0.0, 0.0

    # ------------------------------------------------------------------
    # Internal: dep check + lazy import helpers
    # ------------------------------------------------------------------

    def _check_deps(self) -> bool:
        """Return True if both websockets and psutil are importable.

        Result is cached after the first check. If either dep is missing, an
        error is logged exactly once.
        """
        if self._deps_ok is not None:
            return self._deps_ok

        missing: list[str] = []
        try:
            import websockets as _ws  # noqa: PLC0415, F401
        except ImportError:
            missing.append("websockets")
        try:
            import psutil as _ps  # noqa: PLC0415, F401
        except ImportError:
            missing.append("psutil")

        if missing:
            logger.error(
                "TelemetryServer: missing [hud] dependencies: %s. "
                "Install with: pip install '.[hud]'. HUD telemetry disabled.",
                ", ".join(missing),
            )
            self._deps_ok = False
        else:
            self._deps_ok = True

        return self._deps_ok

    @staticmethod
    def _import_websockets() -> object:
        """Import and return the websockets module (raises ImportError if absent)."""
        import websockets  # noqa: PLC0415 — lazy import by design
        return websockets

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"TelemetryServer("
            f"port={self._port}, "
            f"state={self._state_str!r}, "
            f"clients={len(self._clients)}, "
            f"running={self._broadcast_task is not None})"
        )
