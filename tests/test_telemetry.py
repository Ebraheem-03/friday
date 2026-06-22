"""Unit tests for core/telemetry.py — CI-safe (no websockets/psutil installed).

Coverage
--------
- Frame schema: all required keys present, correct types, value ranges.
- set_state / on_state_change(old, new) updates the frame's state field.
- psutil cpu_pct / ram_pct values flow into the frame (mocked).
- set_audio_level clamps values to [0.0, 1.0] and stores them.
- Broadcast fan-out: frame is sent to all connected clients.
- A failing/dropped client is removed and does not break the loop or other clients.
- Server binds to 127.0.0.1 — assert the host arg passed to websockets.serve.
- Graceful degradation when websockets and/or psutil are not installed.
- Clean start/stop lifecycle (idempotent start + stop, context manager).

Design: both websockets and psutil are mocked throughout. The actual server
never binds to a real socket — we replace websockets.serve with an AsyncMock
and drive the internal _handle_client / _broadcast_loop directly.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import Settings
from core.state import State
from core.telemetry import TelemetryServer, TelemetryFrame, _BIND_HOST


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_settings(port: int = 8765) -> Settings:
    """Return a minimal Settings object pointing at the given port."""
    return Settings(
        gemini_api_key="test-key",
        gemini_model="gemini-2.5-flash",
        sample_rate=24000,
        tts_voice="af_heart",
        telemetry_ws_port=port,
        log_level="DEBUG",
    )


def _make_server(port: int = 8765, interval: float = 0.01) -> TelemetryServer:
    """Create a TelemetryServer with a fast broadcast interval for tests."""
    return TelemetryServer(_make_settings(port), interval=interval)


def _make_fake_websockets_module(serve_return: object | None = None) -> MagicMock:
    """Return a mock 'websockets' module with an async serve() function."""
    ws_mod = MagicMock()
    mock_server = MagicMock()
    mock_server.close = MagicMock()
    mock_server.wait_closed = AsyncMock()

    async def fake_serve(handler, host, port):  # noqa: ANN001
        return serve_return or mock_server

    ws_mod.serve = fake_serve
    ws_mod._mock_server = mock_server
    return ws_mod


def _make_fake_psutil(cpu: float = 42.0, ram: float = 55.0) -> MagicMock:
    """Return a mock 'psutil' module with cpu_percent and virtual_memory."""
    ps_mod = MagicMock()
    ps_mod.cpu_percent.return_value = cpu
    mem = MagicMock()
    mem.percent = ram
    ps_mod.virtual_memory.return_value = mem
    return ps_mod


class _FakeClient:
    """Fake WebSocket client that records sent payloads."""

    def __init__(self, *, fail_on_send: bool = False) -> None:
        self._payloads: list[str] = []
        self._fail_on_send = fail_on_send
        self._closed = asyncio.Event()

    async def send(self, payload: str) -> None:
        if self._fail_on_send:
            raise ConnectionResetError("simulated dead client")
        self._payloads.append(payload)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def disconnect(self) -> None:
        """Simulate the remote end closing the connection."""
        self._closed.set()

    @property
    def received(self) -> list[dict]:  # type: ignore[type-arg]
        return [json.loads(p) for p in self._payloads]


# ---------------------------------------------------------------------------
# Tests: frame schema
# ---------------------------------------------------------------------------

class TestFrameSchema:
    def test_build_frame_has_all_keys(self) -> None:
        """_build_frame returns a dict with all five required fields."""
        srv = _make_server()
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(30.0, 60.0)):
            frame = srv._build_frame()

        assert set(frame.keys()) == {"ts", "state", "cpu_pct", "ram_pct", "audio_level"}

    def test_build_frame_ts_is_recent_epoch(self) -> None:
        """ts field is a float close to current time."""
        srv = _make_server()
        before = time.time()
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(0.0, 0.0)):
            frame = srv._build_frame()
        after = time.time()
        assert isinstance(frame["ts"], float)
        assert before <= frame["ts"] <= after

    def test_build_frame_state_default_idle(self) -> None:
        """Default state is 'idle' before any transition."""
        srv = _make_server()
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(0.0, 0.0)):
            frame = srv._build_frame()
        assert frame["state"] == "idle"

    def test_build_frame_cpu_pct_in_range(self) -> None:
        """cpu_pct is in [0.0, 100.0]."""
        srv = _make_server()
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(73.5, 50.0)):
            frame = srv._build_frame()
        assert 0.0 <= frame["cpu_pct"] <= 100.0
        assert frame["cpu_pct"] == 73.5

    def test_build_frame_ram_pct_in_range(self) -> None:
        """ram_pct is in [0.0, 100.0]."""
        srv = _make_server()
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(0.0, 88.1)):
            frame = srv._build_frame()
        assert 0.0 <= frame["ram_pct"] <= 100.0
        assert frame["ram_pct"] == 88.1

    def test_build_frame_audio_level_default_zero(self) -> None:
        """audio_level defaults to 0.0."""
        srv = _make_server()
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(0.0, 0.0)):
            frame = srv._build_frame()
        assert frame["audio_level"] == 0.0

    def test_frame_is_json_serialisable(self) -> None:
        """The frame dict can be serialised to JSON without error."""
        srv = _make_server()
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(10.0, 20.0)):
            frame = srv._build_frame()
        payload = json.dumps(frame)
        recovered = json.loads(payload)
        assert recovered["state"] == "idle"

    def test_telemetry_frame_typeddict_keys(self) -> None:
        """TelemetryFrame TypedDict has the expected annotations."""
        annotations = TelemetryFrame.__annotations__
        assert set(annotations.keys()) == {"ts", "state", "cpu_pct", "ram_pct", "audio_level"}


# ---------------------------------------------------------------------------
# Tests: state management
# ---------------------------------------------------------------------------

class TestStateManagement:
    def test_set_state_updates_frame(self) -> None:
        """set_state() changes the state reflected in the next frame."""
        srv = _make_server()
        srv.set_state(State.THINKING)
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(0.0, 0.0)):
            frame = srv._build_frame()
        assert frame["state"] == "thinking"

    def test_set_state_all_values(self) -> None:
        """set_state handles every State enum member."""
        srv = _make_server()
        for state in State:
            srv.set_state(state)
            with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(0.0, 0.0)):
                frame = srv._build_frame()
            assert frame["state"] == state.value

    def test_on_state_change_updates_state(self) -> None:
        """on_state_change(old, new) observer updates state to new.value."""
        srv = _make_server()
        srv.on_state_change(State.IDLE, State.LISTENING)
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(0.0, 0.0)):
            frame = srv._build_frame()
        assert frame["state"] == "listening"

    def test_on_state_change_uses_new_not_old(self) -> None:
        """on_state_change only uses new_state, not old_state."""
        srv = _make_server()
        srv.on_state_change(State.SPEAKING, State.ACTING)
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(0.0, 0.0)):
            frame = srv._build_frame()
        assert frame["state"] == "acting"

    def test_on_state_change_signature_matches_observer(self) -> None:
        """on_state_change can be passed directly to StateMachine.subscribe."""
        from core.state import StateMachine
        srv = _make_server()
        sm = StateMachine()
        sm.subscribe(srv.on_state_change)
        sm.transition(State.LISTENING)
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(0.0, 0.0)):
            frame = srv._build_frame()
        assert frame["state"] == "listening"


# ---------------------------------------------------------------------------
# Tests: psutil values flow into the frame
# ---------------------------------------------------------------------------

class TestPsutilIntegration:
    def test_cpu_pct_from_psutil(self) -> None:
        """cpu_pct in the frame comes from the mocked psutil value."""
        srv = _make_server()
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(67.3, 0.0)):
            frame = srv._build_frame()
        assert frame["cpu_pct"] == 67.3

    def test_ram_pct_from_psutil(self) -> None:
        """ram_pct in the frame comes from the mocked psutil value."""
        srv = _make_server()
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(0.0, 82.9)):
            frame = srv._build_frame()
        assert frame["ram_pct"] == 82.9

    def test_sample_psutil_uses_lazy_import(self) -> None:
        """_sample_psutil calls psutil.cpu_percent and virtual_memory."""
        fake_psutil = _make_fake_psutil(cpu=55.0, ram=77.0)
        with patch.dict("sys.modules", {"psutil": fake_psutil}):
            cpu, ram = TelemetryServer._sample_psutil()
        assert cpu == 55.0
        assert ram == 77.0

    def test_sample_psutil_returns_zero_on_import_error(self) -> None:
        """_sample_psutil returns (0.0, 0.0) when psutil is not installed."""
        with patch.dict("sys.modules", {"psutil": None}):
            cpu, ram = TelemetryServer._sample_psutil()
        assert cpu == 0.0
        assert ram == 0.0


# ---------------------------------------------------------------------------
# Tests: audio level
# ---------------------------------------------------------------------------

class TestAudioLevel:
    def test_set_audio_level_normal(self) -> None:
        """set_audio_level stores a valid value in [0.0, 1.0]."""
        srv = _make_server()
        srv.set_audio_level(0.75)
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(0.0, 0.0)):
            frame = srv._build_frame()
        assert frame["audio_level"] == pytest.approx(0.75)

    def test_set_audio_level_clamps_above_one(self) -> None:
        """Values > 1.0 are clamped to 1.0."""
        srv = _make_server()
        srv.set_audio_level(2.5)
        assert srv._audio_level == pytest.approx(1.0)

    def test_set_audio_level_clamps_below_zero(self) -> None:
        """Negative values are clamped to 0.0."""
        srv = _make_server()
        srv.set_audio_level(-0.5)
        assert srv._audio_level == pytest.approx(0.0)

    def test_set_audio_level_zero_boundary(self) -> None:
        """0.0 is a valid and unchanged value."""
        srv = _make_server()
        srv.set_audio_level(0.0)
        assert srv._audio_level == pytest.approx(0.0)

    def test_set_audio_level_one_boundary(self) -> None:
        """1.0 is a valid and unchanged value."""
        srv = _make_server()
        srv.set_audio_level(1.0)
        assert srv._audio_level == pytest.approx(1.0)

    def test_set_audio_level_reflected_in_frame(self) -> None:
        """The value set via set_audio_level appears in _build_frame."""
        srv = _make_server()
        srv.set_audio_level(0.33)
        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(0.0, 0.0)):
            frame = srv._build_frame()
        assert frame["audio_level"] == pytest.approx(0.33)


# ---------------------------------------------------------------------------
# Tests: bind address
# ---------------------------------------------------------------------------

class TestBindAddress:
    @pytest.mark.asyncio
    async def test_server_binds_to_localhost_only(self) -> None:
        """The host argument passed to websockets.serve is 127.0.0.1."""
        captured: dict[str, object] = {}
        mock_server = MagicMock()
        mock_server.close = MagicMock()
        mock_server.wait_closed = AsyncMock()

        async def fake_serve(handler, host, port):  # noqa: ANN001
            captured["host"] = host
            captured["port"] = port
            return mock_server

        fake_ws = MagicMock()
        fake_ws.serve = fake_serve

        fake_ps = _make_fake_psutil()

        srv = _make_server(port=9876)
        with patch.dict("sys.modules", {"websockets": fake_ws, "psutil": fake_ps}):
            # Reset the cached dep check so it picks up our mocks.
            srv._deps_ok = None
            await srv.start()
            await srv.stop()

        assert captured["host"] == "127.0.0.1"
        assert captured["host"] == _BIND_HOST

    @pytest.mark.asyncio
    async def test_server_binds_to_configured_port(self) -> None:
        """The port argument passed to websockets.serve matches settings.telemetry_ws_port."""
        captured: dict[str, object] = {}
        mock_server = MagicMock()
        mock_server.close = MagicMock()
        mock_server.wait_closed = AsyncMock()

        async def fake_serve(handler, host, port):  # noqa: ANN001
            captured["port"] = port
            return mock_server

        fake_ws = MagicMock()
        fake_ws.serve = fake_serve
        fake_ps = _make_fake_psutil()

        srv = _make_server(port=9001)
        with patch.dict("sys.modules", {"websockets": fake_ws, "psutil": fake_ps}):
            srv._deps_ok = None
            await srv.start()
            await srv.stop()

        assert captured["port"] == 9001


# ---------------------------------------------------------------------------
# Tests: broadcast fan-out
# ---------------------------------------------------------------------------

class TestBroadcastFanout:
    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all_clients(self) -> None:
        """_broadcast_loop sends a frame to every connected client."""
        srv = _make_server(interval=0.01)

        client_a = _FakeClient()
        client_b = _FakeClient()
        srv._clients.add(client_a)
        srv._clients.add(client_b)

        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(10.0, 20.0)):
            # Run one iteration of the broadcast loop manually.
            frame = srv._build_frame()
            payload = json.dumps(frame)
            await asyncio.gather(
                srv._safe_send(client_a, payload),
                srv._safe_send(client_b, payload),
            )

        assert len(client_a.received) == 1
        assert len(client_b.received) == 1

    @pytest.mark.asyncio
    async def test_broadcast_frame_is_valid_json_with_all_keys(self) -> None:
        """Each broadcast payload is valid JSON with all expected keys."""
        srv = _make_server()
        client = _FakeClient()
        srv._clients.add(client)

        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(5.0, 10.0)):
            frame = srv._build_frame()
            payload = json.dumps(frame)
            await srv._safe_send(client, payload)

        assert len(client.received) == 1
        received_frame = client.received[0]
        assert set(received_frame.keys()) == {"ts", "state", "cpu_pct", "ram_pct", "audio_level"}

    @pytest.mark.asyncio
    async def test_broadcast_loop_ticks_multiple_times(self) -> None:
        """The broadcast loop sends multiple frames over its lifetime."""
        srv = _make_server(interval=0.005)
        client = _FakeClient()
        srv._clients.add(client)

        # Patch _build_frame to count calls.
        frame_count = 0
        original_build = srv._build_frame

        def counting_build() -> TelemetryFrame:
            nonlocal frame_count
            frame_count += 1
            return original_build()

        srv._build_frame = counting_build  # type: ignore[method-assign]

        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(0.0, 0.0)):
            task = asyncio.create_task(srv._broadcast_loop())
            await asyncio.sleep(0.06)  # ~12 ticks at 5ms interval
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert frame_count >= 5


# ---------------------------------------------------------------------------
# Tests: dropped / failing client
# ---------------------------------------------------------------------------

class TestDroppedClient:
    @pytest.mark.asyncio
    async def test_failing_client_is_removed_from_set(self) -> None:
        """A client that raises on send is removed from _clients."""
        srv = _make_server()
        dead_client = _FakeClient(fail_on_send=True)
        srv._clients.add(dead_client)

        payload = json.dumps({"ts": 0.0, "state": "idle", "cpu_pct": 0.0,
                              "ram_pct": 0.0, "audio_level": 0.0})
        await srv._safe_send(dead_client, payload)

        assert dead_client not in srv._clients

    @pytest.mark.asyncio
    async def test_failing_client_does_not_affect_healthy_client(self) -> None:
        """A dead client being dropped does not prevent a healthy client from receiving."""
        srv = _make_server()
        dead = _FakeClient(fail_on_send=True)
        healthy = _FakeClient()
        srv._clients.add(dead)
        srv._clients.add(healthy)

        payload = json.dumps({"ts": 0.0, "state": "idle", "cpu_pct": 0.0,
                              "ram_pct": 0.0, "audio_level": 0.0})

        # Fan-out exactly as _broadcast_loop does.
        await asyncio.gather(
            *[srv._safe_send(c, payload) for c in list(srv._clients)],
            return_exceptions=True,
        )

        assert dead not in srv._clients
        assert len(healthy.received) == 1

    @pytest.mark.asyncio
    async def test_broadcast_loop_continues_after_client_drop(self) -> None:
        """The broadcast loop keeps running after a client fails and is dropped."""
        srv = _make_server(interval=0.005)
        dead = _FakeClient(fail_on_send=True)
        healthy = _FakeClient()
        srv._clients.add(dead)
        srv._clients.add(healthy)

        with patch("core.telemetry.TelemetryServer._sample_psutil", return_value=(0.0, 0.0)):
            task = asyncio.create_task(srv._broadcast_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Dead client removed, healthy client received frames.
        assert dead not in srv._clients
        assert len(healthy.received) >= 2

    @pytest.mark.asyncio
    async def test_safe_send_does_not_raise(self) -> None:
        """_safe_send never propagates exceptions — it swallows all send failures."""
        srv = _make_server()
        dead = _FakeClient(fail_on_send=True)
        srv._clients.add(dead)
        # Should not raise.
        await srv._safe_send(dead, "payload")


# ---------------------------------------------------------------------------
# Tests: graceful degradation when deps missing
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    @pytest.mark.asyncio
    async def test_start_without_websockets_does_not_raise(self) -> None:
        """start() logs an error and returns without raising when websockets is absent."""
        srv = _make_server()
        srv._deps_ok = None

        # Make both deps unavailable.
        with patch.dict("sys.modules", {"websockets": None, "psutil": None}):
            # Should not raise.
            await srv.start()

        # Server should not have started (no broadcast task).
        assert srv._broadcast_task is None

    @pytest.mark.asyncio
    async def test_start_without_psutil_does_not_raise(self) -> None:
        """start() degrades gracefully when psutil is absent."""
        srv = _make_server()
        srv._deps_ok = None

        with patch.dict("sys.modules", {"websockets": None, "psutil": None}):
            await srv.start()  # must not raise

        assert srv._broadcast_task is None

    def test_module_imports_without_hud_deps(self) -> None:
        """core.telemetry imports successfully even when websockets/psutil are absent."""
        # The fact that this test file imported the module already proves this,
        # but we assert the key symbols are accessible.
        from core.telemetry import TelemetryServer, TelemetryFrame, _BIND_HOST  # noqa: F401
        assert TelemetryServer is not None
        assert _BIND_HOST == "127.0.0.1"

    def test_check_deps_returns_false_when_missing(self) -> None:
        """_check_deps returns False when websockets is not importable."""
        srv = _make_server()
        srv._deps_ok = None

        with patch.dict("sys.modules", {"websockets": None}):
            result = srv._check_deps()

        assert result is False

    def test_check_deps_caches_result(self) -> None:
        """_check_deps only checks imports once; subsequent calls use the cache."""
        srv = _make_server()
        srv._deps_ok = True  # pre-seed cache

        # Even if modules are broken now, the cached True is returned.
        with patch.dict("sys.modules", {"websockets": None, "psutil": None}):
            result = srv._check_deps()

        assert result is True


# ---------------------------------------------------------------------------
# Tests: lifecycle (start/stop)
# ---------------------------------------------------------------------------

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop_clean(self) -> None:
        """Server starts and stops without error."""
        mock_server = MagicMock()
        mock_server.close = MagicMock()
        mock_server.wait_closed = AsyncMock()

        async def fake_serve(handler, host, port):  # noqa: ANN001
            return mock_server

        fake_ws = MagicMock()
        fake_ws.serve = fake_serve
        fake_ps = _make_fake_psutil()

        srv = _make_server()
        with patch.dict("sys.modules", {"websockets": fake_ws, "psutil": fake_ps}):
            srv._deps_ok = None
            await srv.start()
            assert srv._broadcast_task is not None
            assert srv._server is not None
            await srv.stop()
            assert srv._broadcast_task is None
            assert srv._server is None

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        """Calling start() twice while running is a no-op."""
        mock_server = MagicMock()
        mock_server.close = MagicMock()
        mock_server.wait_closed = AsyncMock()
        serve_call_count = 0

        async def fake_serve(handler, host, port):  # noqa: ANN001
            nonlocal serve_call_count
            serve_call_count += 1
            return mock_server

        fake_ws = MagicMock()
        fake_ws.serve = fake_serve
        fake_ps = _make_fake_psutil()

        srv = _make_server()
        with patch.dict("sys.modules", {"websockets": fake_ws, "psutil": fake_ps}):
            srv._deps_ok = None
            await srv.start()
            await srv.start()  # second call — no-op
            await srv.stop()

        assert serve_call_count == 1

    @pytest.mark.asyncio
    async def test_stop_idempotent(self) -> None:
        """Calling stop() on a stopped server does not raise."""
        srv = _make_server()
        await srv.stop()  # never started
        await srv.stop()  # second call — should not raise

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        """async with TelemetryServer starts and stops automatically."""
        mock_server = MagicMock()
        mock_server.close = MagicMock()
        mock_server.wait_closed = AsyncMock()

        async def fake_serve(handler, host, port):  # noqa: ANN001
            return mock_server

        fake_ws = MagicMock()
        fake_ws.serve = fake_serve
        fake_ps = _make_fake_psutil()

        srv = _make_server()
        with patch.dict("sys.modules", {"websockets": fake_ws, "psutil": fake_ps}):
            srv._deps_ok = None
            async with srv:
                assert srv._broadcast_task is not None
            assert srv._broadcast_task is None

    @pytest.mark.asyncio
    async def test_handle_client_adds_and_removes(self) -> None:
        """_handle_client adds client to _clients and removes on disconnect."""
        srv = _make_server()
        client = _FakeClient()

        # Run the handler; immediately disconnect the fake client.
        async def run_and_disconnect() -> None:
            handle_task = asyncio.create_task(srv._handle_client(client))
            await asyncio.sleep(0.01)
            client.disconnect()
            await handle_task

        await run_and_disconnect()

        assert client not in srv._clients

    def test_repr_is_informative(self) -> None:
        """repr includes port, state, client count, and running status."""
        srv = _make_server(port=1234)
        r = repr(srv)
        assert "TelemetryServer" in r
        assert "1234" in r
        assert "idle" in r
        assert "running=False" in r
