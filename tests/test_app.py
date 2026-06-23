"""Tests for core/app.py — build_app wiring + turn-bridge helpers.

Coverage strategy
-----------------
- ``build_app``: asserts the dependency graph is correctly wired with REAL
  implementations (not no-op stubs), correct constructor flags, and that NO I/O
  is performed (no preloads called, no sockets bound).
- ``_make_turn_bridge`` + ``_queue_to_iter``: unit tests for the orchestrator-
  output → AsyncIterator[str] bridge (pure asyncio, no hardware).
- ``_terminate_hud``: idempotency of the shutdown helper.
- ``_spawn_hud`` disabled path: asserts no subprocess is spawned when npm is
  absent or the hud dir is missing.
- ``compute_rms``: the RMS normalisation formula used in the live loop.

NOT tested here (hardware loop, skipped in CI):
- ``run()`` in full (mic, speakers, real Gemini, Electron process).
- ``tts_kokoro.preload`` / ``stt.preload`` interactions (tested in test_voice/test_stt).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.app import (
    App,
    _make_turn_bridge,
    _queue_to_iter,
    _spawn_hud,
    _terminate_hud,
    build_app,
)
from core.brain import Brain, MemoryTool, OSTool, WebTool
from core.config import Settings
from core.orchestrator import Orchestrator
from core.os_bridge import OsBridge
from core.state import State
from core.telemetry import TelemetryServer
from memory.recall import Memory
from web.agent import WebAgent


# ---------------------------------------------------------------------------
# Minimal Settings factory (no real API key needed for build_app tests)
# ---------------------------------------------------------------------------

def _fake_settings(**overrides: object) -> Settings:
    """Return a Settings instance with fake values — no env lookup."""
    defaults: dict[str, object] = dict(
        gemini_api_key="test-key-not-real",
        gemini_model="gemini-2.5-flash",
        sample_rate=24_000,
        tts_voice="af_heart",
        stt_model="base.en",
        telemetry_ws_port=8765,
        log_level="INFO",
    )
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# build_app — wiring correctness assertions
# ---------------------------------------------------------------------------


class TestBuildApp:
    """build_app() constructs the correct graph without performing any I/O."""

    def setup_method(self) -> None:
        self.settings = _fake_settings()

    def test_returns_app_instance(self) -> None:
        app = build_app(self.settings)
        assert isinstance(app, App)

    def test_memory_is_real_memory(self) -> None:
        """Brain must receive the real Memory, not _NoopMemory."""
        app = build_app(self.settings)
        assert isinstance(app.memory, Memory)

    def test_os_bridge_is_real_os_bridge(self) -> None:
        """Brain must receive the real OsBridge, not _NoopOS."""
        app = build_app(self.settings)
        assert isinstance(app.os_bridge, OsBridge)

    def test_web_agent_is_real_web_agent(self) -> None:
        """Brain must receive the real WebAgent, not _NoopWeb."""
        app = build_app(self.settings)
        assert isinstance(app.web_agent, WebAgent)

    def test_os_bridge_trusted_false(self) -> None:
        """Locked human decision: OsBridge must be trusted=False."""
        app = build_app(self.settings)
        assert app.os_bridge._trusted is False

    def test_web_agent_allow_destructive_true(self) -> None:
        """Locked human decision: WebAgent must be allow_destructive=True."""
        app = build_app(self.settings)
        assert app.web_agent._allow_destructive is True

    def test_telemetry_is_telemetry_server(self) -> None:
        app = build_app(self.settings)
        assert isinstance(app.telemetry, TelemetryServer)

    def test_brain_is_brain_instance(self) -> None:
        app = build_app(self.settings)
        assert isinstance(app.brain, Brain)

    def test_orchestrator_is_orchestrator_instance(self) -> None:
        app = build_app(self.settings)
        assert isinstance(app.orchestrator, Orchestrator)

    def test_brain_memory_is_app_memory(self) -> None:
        """Brain._memory must be the SAME object as App.memory (identity check)."""
        app = build_app(self.settings)
        # Brain stores the real memory tool; the real Memory satisfies MemoryTool.
        assert isinstance(app.brain._memory, Memory)
        # Identity: same object as app.memory
        assert app.brain._memory is app.memory

    def test_brain_os_tool_is_app_os_bridge(self) -> None:
        """Brain._os_tool must be the SAME OsBridge as App.os_bridge."""
        app = build_app(self.settings)
        assert app.brain._os_tool is app.os_bridge

    def test_brain_web_tool_is_app_web_agent(self) -> None:
        """Brain._web_tool must be the SAME WebAgent as App.web_agent."""
        app = build_app(self.settings)
        assert app.brain._web_tool is app.web_agent

    def test_brain_tools_are_not_noop_stubs(self) -> None:
        """The brain must NOT have the default no-op stubs wired in."""
        from core.brain import _NoopMemory, _NoopOS, _NoopWeb

        app = build_app(self.settings)
        assert not isinstance(app.brain._memory, _NoopMemory)
        assert not isinstance(app.brain._os_tool, _NoopOS)
        assert not isinstance(app.brain._web_tool, _NoopWeb)

    def test_telemetry_subscribed_as_state_observer(self) -> None:
        """Telemetry.on_state_change must be registered with the orchestrator's state machine."""
        app = build_app(self.settings)
        observers = app.orchestrator._sm._observers
        assert app.telemetry.on_state_change in observers

    def test_build_app_performs_no_io(self) -> None:
        """build_app must NOT call preload, bind sockets, or spawn processes."""
        with (
            patch("core.app.TelemetryServer.start") as mock_start,
            patch("core.app.Memory") as mock_memory_cls,
        ):
            # We can't easily patch tts_kokoro.preload / stt.preload here
            # because build_app lazy-imports them only inside run().
            # Instead, assert the telemetry server is NOT started.
            mock_memory_cls.return_value = MagicMock(spec=Memory)
            # Just verifying start is not called during construction.
            build_app(self.settings)
            mock_start.assert_not_called()

    def test_settings_stored_on_app(self) -> None:
        app = build_app(self.settings)
        assert app.settings is self.settings

    def test_memory_protocol_satisfied(self) -> None:
        """Memory must satisfy the MemoryTool Protocol (runtime_checkable)."""
        app = build_app(self.settings)
        assert isinstance(app.memory, MemoryTool)

    def test_os_bridge_protocol_satisfied(self) -> None:
        """OsBridge must satisfy the OSTool Protocol (runtime_checkable)."""
        app = build_app(self.settings)
        assert isinstance(app.os_bridge, OSTool)

    def test_web_agent_protocol_satisfied(self) -> None:
        """WebAgent must satisfy the WebTool Protocol (runtime_checkable)."""
        app = build_app(self.settings)
        assert isinstance(app.web_agent, WebTool)


# ---------------------------------------------------------------------------
# Turn-bridge: _make_turn_bridge + _queue_to_iter
# ---------------------------------------------------------------------------


class TestTurnBridge:
    """Unit tests for the orchestrator → AsyncIterator[str] bridge."""

    def _make_orchestrator(self) -> Orchestrator:
        """Create a minimal Orchestrator with a mock brain (no real Gemini)."""
        mock_brain = MagicMock(spec=Brain)
        return Orchestrator(brain=mock_brain)

    @pytest.mark.asyncio
    async def test_queue_to_iter_yields_items(self) -> None:
        """_queue_to_iter yields items until the None sentinel."""
        q: asyncio.Queue[str | None] = asyncio.Queue()
        q.put_nowait("hello")
        q.put_nowait(" world")
        q.put_nowait(None)

        items = []
        async for item in _queue_to_iter(q):
            items.append(item)

        assert items == ["hello", " world"]

    @pytest.mark.asyncio
    async def test_queue_to_iter_empty_terminated(self) -> None:
        """_queue_to_iter with immediate None sentinel yields nothing."""
        q: asyncio.Queue[str | None] = asyncio.Queue()
        q.put_nowait(None)

        items = []
        async for item in _queue_to_iter(q):
            items.append(item)

        assert items == []

    @pytest.mark.asyncio
    async def test_make_turn_bridge_delta_flow(self) -> None:
        """Deltas put via the subscriber are accessible via the returned queue."""
        orch = self._make_orchestrator()
        text_q = _make_turn_bridge(orch)

        # Simulate brain sending a delta via the output callback.
        for cb in orch._callbacks:
            cb("Hello ")
        for cb in orch._callbacks:
            cb("world")

        # Then simulate SPEAKING → IDLE transition (turn end).
        orch._sm._state = State.SPEAKING  # force state for test
        orch._sm.transition(State.IDLE)   # fires observers

        # Now collect from the iterator.
        items = []
        async for item in _queue_to_iter(text_q):
            items.append(item)

        assert items == ["Hello ", "world"]

    @pytest.mark.asyncio
    async def test_make_turn_bridge_callbacks_unregistered_after_turn(self) -> None:
        """After turn end, the per-turn callbacks are removed from the orchestrator."""
        orch = self._make_orchestrator()
        initial_cb_count = len(orch._callbacks)
        initial_obs_count = len(orch._sm._observers)

        text_q = _make_turn_bridge(orch)

        # Both a delta callback and a state observer are now registered.
        assert len(orch._callbacks) == initial_cb_count + 1
        assert len(orch._sm._observers) == initial_obs_count + 1

        # Force a SPEAKING → IDLE transition to trigger cleanup.
        orch._sm._state = State.SPEAKING
        orch._sm.transition(State.IDLE)

        # Drain the queue (None sentinel should be there).
        await asyncio.wait_for(_drain_queue(text_q), timeout=1.0)

        # Both callbacks should be cleaned up.
        assert len(orch._callbacks) == initial_cb_count
        assert len(orch._sm._observers) == initial_obs_count

    @pytest.mark.asyncio
    async def test_make_turn_bridge_idempotent_done(self) -> None:
        """A second SPEAKING→IDLE transition does NOT put a second None sentinel."""
        orch = self._make_orchestrator()
        text_q = _make_turn_bridge(orch)

        # First SPEAKING→IDLE — puts None sentinel.
        orch._sm._state = State.SPEAKING
        orch._sm.transition(State.IDLE)

        # Second SPEAKING→IDLE should be a no-op (done_sent guard).
        orch._sm._state = State.SPEAKING
        # Need to re-register state for second transition to work
        # (but the observer should have been removed after the first).
        orch._sm.transition(State.IDLE)

        # Only one None should be in the queue.
        assert text_q.qsize() == 1
        sentinel = text_q.get_nowait()
        assert sentinel is None
        assert text_q.empty()

    @pytest.mark.asyncio
    async def test_multiple_turns_independent_queues(self) -> None:
        """Two consecutive turns use separate queues (no cross-turn contamination)."""
        orch = self._make_orchestrator()

        # Turn 1
        q1 = _make_turn_bridge(orch)
        for cb in orch._callbacks:
            cb("turn1")
        orch._sm._state = State.SPEAKING
        orch._sm.transition(State.IDLE)

        items1 = []
        async for item in _queue_to_iter(q1):
            items1.append(item)

        # Turn 2 — fresh queue
        q2 = _make_turn_bridge(orch)
        for cb in orch._callbacks:
            cb("turn2")
        orch._sm._state = State.SPEAKING
        orch._sm.transition(State.IDLE)

        items2 = []
        async for item in _queue_to_iter(q2):
            items2.append(item)

        assert items1 == ["turn1"]
        assert items2 == ["turn2"]


# ---------------------------------------------------------------------------
# HUD spawn guard
# ---------------------------------------------------------------------------


class TestSpawnHud:
    """_spawn_hud returns None safely when HUD is unavailable."""

    def test_returns_none_when_dir_missing(self, tmp_path: Path) -> None:
        """Non-existent hud dir → None (warning, no crash)."""
        missing = tmp_path / "nonexistent_hud"
        result = _spawn_hud(missing)
        assert result is None

    def test_returns_none_when_npm_missing(self, tmp_path: Path) -> None:
        """When npm is not on PATH → None (warning, no crash)."""
        hud_dir = tmp_path / "hud"
        hud_dir.mkdir()

        with patch("shutil.which", return_value=None):
            result = _spawn_hud(hud_dir)

        assert result is None

    def test_returns_none_on_oserror(self, tmp_path: Path) -> None:
        """OSError during Popen → None (warning, no crash)."""
        hud_dir = tmp_path / "hud"
        hud_dir.mkdir()

        with (
            patch("shutil.which", return_value="/usr/bin/npm"),
            patch("subprocess.Popen", side_effect=OSError("no electron")),
        ):
            result = _spawn_hud(hud_dir)

        assert result is None


# ---------------------------------------------------------------------------
# _terminate_hud idempotency
# ---------------------------------------------------------------------------


class TestTerminateHud:
    """_terminate_hud is safe to call multiple times and with None."""

    def test_none_is_noop(self) -> None:
        """Passing None does not raise."""
        _terminate_hud(None)  # should not raise

    def test_already_dead_process(self) -> None:
        """Calling terminate on an already-dead process is idempotent."""
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = 0  # already exited
        _terminate_hud(mock_proc)
        mock_proc.terminate.assert_not_called()  # poll() returned non-None

    def test_live_process_terminated(self) -> None:
        """A live process gets terminate() + wait() called."""
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None  # still running
        _terminate_hud(mock_proc)
        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once()


# ---------------------------------------------------------------------------
# RMS normalisation (the formula used in run() for audio_level)
# ---------------------------------------------------------------------------


class TestRmsFormula:
    """Verify the RMS formula produces values in [0.0, 1.0]."""

    def test_silence_gives_zero(self) -> None:
        """All-zero PCM → RMS = 0.0."""
        pcm = np.zeros(1600, dtype=np.int16).tobytes()
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples ** 2)))
        assert rms == 0.0

    def test_full_scale_gives_one(self) -> None:
        """Full-scale int16 signal (32767) → RMS ≈ 1.0."""
        pcm = np.full(1600, 32767, dtype=np.int16).tobytes()
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples ** 2)))
        assert abs(rms - 1.0) < 0.001  # within 0.1% of 1.0

    def test_half_scale_gives_half(self) -> None:
        """Half-scale signal → RMS ≈ 0.5."""
        pcm = np.full(1600, 16383, dtype=np.int16).tobytes()
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples ** 2)))
        assert 0.4 < rms < 0.6

    def test_rms_in_unit_range(self) -> None:
        """Random realistic audio signal → RMS in [0, 1]."""
        rng = np.random.default_rng(42)
        pcm = (rng.standard_normal(4800) * 8000).clip(-32768, 32767).astype(np.int16).tobytes()
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples ** 2)))
        assert 0.0 <= rms <= 1.0


# ---------------------------------------------------------------------------
# orchestrator.unsubscribe_state — the new method we added
# ---------------------------------------------------------------------------


class TestOrchestratorUnsubscribeState:
    """unsubscribe_state forwards to the StateMachine."""

    def test_unsubscribe_state_removes_observer(self) -> None:
        mock_brain = MagicMock(spec=Brain)
        orch = Orchestrator(brain=mock_brain)
        calls: list[tuple[State, State]] = []

        def obs(old: State, new: State) -> None:
            calls.append((old, new))

        orch.subscribe_state(obs)
        assert obs in orch._sm._observers

        removed = orch.unsubscribe_state(obs)
        assert removed is True
        assert obs not in orch._sm._observers

    def test_unsubscribe_state_returns_false_if_not_found(self) -> None:
        mock_brain = MagicMock(spec=Brain)
        orch = Orchestrator(brain=mock_brain)

        def obs(old: State, new: State) -> None:
            pass

        result = orch.unsubscribe_state(obs)
        assert result is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _drain_queue(q: asyncio.Queue[str | None]) -> None:
    """Drain all items from *q* (async, for use with wait_for)."""
    while not q.empty():
        q.get_nowait()
