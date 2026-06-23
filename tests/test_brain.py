"""Unit tests for core/brain.py — mocked, no network, no quota.

Coverage
--------
- Brain.stream yields correct text deltas from mocked streaming response.
- Text-only response: no tool-calls emitted.
- Function-call routing: a fabricated function_call routes to the correct
  Protocol stub and the response is fed back (second stream call verified).
- Unknown tool-call: Brain returns an error string without raising.
- Missing args in tool-call: Brain handles gracefully with defaults.
- _NoopMemory, _NoopOS, _NoopWeb stubs return the expected placeholder strings.
- gemini_model field: Settings.from_env reads GEMINI_MODEL with correct default.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.brain import Brain, MemoryTool, OSTool, WebTool, _NoopMemory, _NoopOS, _NoopWeb, _MAX_TOOL_ROUNDS
from core.config import Settings


# ---------------------------------------------------------------------------
# Helpers: fake Settings
# ---------------------------------------------------------------------------

def _fake_settings(model: str = "test-model") -> Settings:
    """Build a Settings object without reading .env or requiring a real key."""
    return Settings(
        gemini_api_key="fake-key-for-testing",
        gemini_model=model,
        sample_rate=24000,
        tts_voice="af_heart",
        tts_engine="kokoro",
        tts_gemini_voice="Kore",
        tts_gemini_model="gemini-2.5-flash-preview-tts",
        stt_model="base.en",
        telemetry_ws_port=8765,
        log_level="DEBUG",
        wake_word="friday",
        wake_word_enabled=True,
    )


# ---------------------------------------------------------------------------
# Helpers: fake streaming chunks
# ---------------------------------------------------------------------------

def _text_chunk(text: str) -> MagicMock:
    """Build a fake GenerateContentResponse chunk with only text."""
    chunk = MagicMock()
    chunk.text = text
    chunk.function_calls = None
    return chunk


def _fc_chunk(name: str, args: dict[str, Any]) -> MagicMock:
    """Build a fake GenerateContentResponse chunk with a function_call."""
    fc = MagicMock()
    fc.name = name
    fc.args = args

    chunk = MagicMock()
    chunk.text = None
    chunk.function_calls = [fc]
    return chunk


async def _async_iter(items: list) -> AsyncIterator:
    """Yield items as an async iterator (simulates aio stream)."""
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def settings() -> Settings:
    return _fake_settings()


@pytest.fixture()
def noop_memory() -> _NoopMemory:
    return _NoopMemory()


@pytest.fixture()
def noop_os() -> _NoopOS:
    return _NoopOS()


@pytest.fixture()
def noop_web() -> _NoopWeb:
    return _NoopWeb()


# ---------------------------------------------------------------------------
# Helper: build a Brain with a mocked genai.Client
# ---------------------------------------------------------------------------

def _make_brain_with_mock(
    stream_chunks: list,
    *,
    memory: MemoryTool | None = None,
    os_tool: OSTool | None = None,
    web_tool: WebTool | None = None,
) -> tuple[Brain, MagicMock]:
    """Return (brain, mock_client) with generate_content_stream returning chunks."""
    settings = _fake_settings()

    with patch("core.brain.genai.Client") as MockClient:
        mock_client = MagicMock()
        MockClient.return_value = mock_client

        # client.aio.models.generate_content_stream is awaitable -> async generator
        mock_stream = _async_iter(stream_chunks)
        mock_client.aio.models.generate_content_stream = AsyncMock(
            return_value=mock_stream
        )

        brain = Brain(settings, memory=memory, os_tool=os_tool, web_tool=web_tool)
        # Swap out the real client with our mock directly so patch context is not needed
        brain._client = mock_client
        return brain, mock_client


# ---------------------------------------------------------------------------
# Tests: text-only streaming
# ---------------------------------------------------------------------------

class TestBrainStreamText:
    @pytest.mark.asyncio
    async def test_yields_text_deltas(self) -> None:
        """Brain.stream yields each text chunk from the model in order."""
        chunks = [_text_chunk("Hello"), _text_chunk(" world"), _text_chunk("!")]
        brain, mock_client = _make_brain_with_mock(chunks)

        results: list[str] = []
        async for delta in brain.stream("Hi", history=[]):
            results.append(delta)

        assert results == ["Hello", " world", "!"]

    @pytest.mark.asyncio
    async def test_passes_model_and_contents_to_sdk(self) -> None:
        """Brain calls the SDK with the correct model name and content."""
        chunks = [_text_chunk("ok")]
        brain, mock_client = _make_brain_with_mock(chunks)

        async for _ in brain.stream("test input", history=[]):
            pass

        mock_client.aio.models.generate_content_stream.assert_called_once()
        call_kwargs = mock_client.aio.models.generate_content_stream.call_args.kwargs
        assert call_kwargs["model"] == "test-model"
        # contents should have one user turn
        assert len(call_kwargs["contents"]) == 1
        assert call_kwargs["contents"][0].role == "user"

    @pytest.mark.asyncio
    async def test_empty_response_yields_nothing(self) -> None:
        """If model returns no chunks, stream yields nothing without error."""
        brain, _ = _make_brain_with_mock([])

        results: list[str] = []
        async for delta in brain.stream("empty", history=[]):
            results.append(delta)

        assert results == []

    @pytest.mark.asyncio
    async def test_none_text_chunks_not_yielded(self) -> None:
        """Chunks where .text is None/empty are not yielded."""
        chunks = [_text_chunk(""), _text_chunk("real"), _text_chunk(None)]
        brain, _ = _make_brain_with_mock(chunks)

        results: list[str] = []
        async for delta in brain.stream("x", history=[]):
            results.append(delta)

        assert results == ["real"]

    @pytest.mark.asyncio
    async def test_history_prepended_to_contents(self) -> None:
        """Brain prepends history items before the new user turn."""
        from google.genai import types
        chunks = [_text_chunk("answer")]
        brain, mock_client = _make_brain_with_mock(chunks)

        history = [types.Content(role="user", parts=[types.Part.from_text(text="prev")])]
        async for _ in brain.stream("new", history=history):
            pass

        call_kwargs = mock_client.aio.models.generate_content_stream.call_args.kwargs
        assert len(call_kwargs["contents"]) == 2
        assert call_kwargs["contents"][0].role == "user"  # history item
        assert call_kwargs["contents"][1].role == "user"  # new user turn


# ---------------------------------------------------------------------------
# Tests: function-call routing
# ---------------------------------------------------------------------------

class TestBrainToolCalls:
    @pytest.mark.asyncio
    async def test_remember_tool_routes_to_memory(self) -> None:
        """A 'remember' function_call is dispatched to MemoryTool.remember."""

        class FakeMemory:
            called_with: tuple[str, str] | None = None

            async def remember(self, key: str, value: str) -> str:
                FakeMemory.called_with = (key, value)
                return "stored"

            async def recall(self, query: str) -> str:  # noqa: ARG002
                return ""

        fake_mem = FakeMemory()

        # First call: function-call chunk; second call: follow-up text
        call_count = 0

        async def fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield _fc_chunk("remember", {"key": "name", "value": "Alice"})
            else:
                yield _text_chunk("Remembered.")

        brain, mock_client = _make_brain_with_mock([], memory=fake_mem)  # type: ignore[arg-type]
        mock_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=fake_stream
        )

        results: list[str] = []
        async for delta in brain.stream("Remember my name is Alice", history=[]):
            results.append(delta)

        assert FakeMemory.called_with == ("name", "Alice")
        assert results == ["Remembered."]
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_recall_tool_routes_to_memory(self) -> None:
        """A 'recall' function_call is dispatched to MemoryTool.recall."""

        class FakeMemory:
            recalled: str | None = None

            async def remember(self, key: str, value: str) -> str:  # noqa: ARG002
                return ""

            async def recall(self, query: str) -> str:
                FakeMemory.recalled = query
                return "Alice"

        call_count = 0

        async def fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield _fc_chunk("recall", {"query": "my name"})
            else:
                yield _text_chunk("Your name is Alice.")

        brain, mock_client = _make_brain_with_mock([], memory=FakeMemory())  # type: ignore[arg-type]
        mock_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=fake_stream
        )

        results: list[str] = []
        async for delta in brain.stream("What is my name?", history=[]):
            results.append(delta)

        assert FakeMemory.recalled == "my name"
        assert results == ["Your name is Alice."]

    @pytest.mark.asyncio
    async def test_os_action_tool_routes_to_os_tool(self) -> None:
        """An 'os_action' function_call is dispatched to OSTool.os_action."""

        class FakeOS:
            called: tuple[str, dict] | None = None

            async def os_action(self, action: str, params: dict) -> str:
                FakeOS.called = (action, params)
                return "done"

        call_count = 0

        async def fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield _fc_chunk("os_action", {"action": "click", "params": {"x": 10, "y": 20}})
            else:
                yield _text_chunk("Clicked.")

        brain, mock_client = _make_brain_with_mock([], os_tool=FakeOS())  # type: ignore[arg-type]
        mock_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=fake_stream
        )

        results: list[str] = []
        async for delta in brain.stream("Click there", history=[]):
            results.append(delta)

        assert FakeOS.called == ("click", {"x": 10, "y": 20})
        assert results == ["Clicked."]

    @pytest.mark.asyncio
    async def test_web_task_tool_routes_to_web_tool(self) -> None:
        """A 'web_task' function_call is dispatched to WebTool.web_task."""

        class FakeWeb:
            called: tuple[str, str] | None = None

            async def web_task(self, task: str, url: str = "") -> str:
                FakeWeb.called = (task, url)
                return "done"

        call_count = 0

        async def fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield _fc_chunk("web_task", {"task": "search weather", "url": ""})
            else:
                yield _text_chunk("Weather found.")

        brain, mock_client = _make_brain_with_mock([], web_tool=FakeWeb())  # type: ignore[arg-type]
        mock_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=fake_stream
        )

        results: list[str] = []
        async for delta in brain.stream("Search for weather", history=[]):
            results.append(delta)

        assert FakeWeb.called == ("search weather", "")
        assert results == ["Weather found."]

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_string_to_model(self) -> None:
        """An unrecognised tool name returns an error string without raising."""
        call_count = 0

        async def fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield _fc_chunk("nonexistent_tool", {})
            else:
                yield _text_chunk("Handled.")

        brain, mock_client = _make_brain_with_mock([])
        mock_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=fake_stream
        )

        results: list[str] = []
        async for delta in brain.stream("do unknown thing", history=[]):
            results.append(delta)

        # Should not raise; should yield the follow-up text
        assert results == ["Handled."]
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_tool_call_missing_args_uses_defaults(self) -> None:
        """Tool call with empty args dict does not raise — defaults used."""

        class FakeMemory:
            async def remember(self, key: str, value: str) -> str:
                # Called with empty defaults; must not raise
                assert isinstance(key, str)
                assert isinstance(value, str)
                return "ok"

            async def recall(self, query: str) -> str:
                return ""

        call_count = 0

        async def fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield _fc_chunk("remember", {})  # empty args
            else:
                yield _text_chunk("Done.")

        brain, mock_client = _make_brain_with_mock([], memory=FakeMemory())  # type: ignore[arg-type]
        mock_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=fake_stream
        )

        results = []
        async for delta in brain.stream("remember nothing", history=[]):
            results.append(delta)

        assert results == ["Done."]

    @pytest.mark.asyncio
    async def test_function_response_fed_back_to_model(self) -> None:
        """After tool execution, Brain calls the model a second time with function-response."""
        call_count = 0
        second_call_contents: list | None = None

        async def fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator:
            nonlocal call_count, second_call_contents
            call_count += 1
            if call_count == 1:
                yield _fc_chunk("recall", {"query": "x"})
            else:
                second_call_contents = kwargs.get("contents")
                yield _text_chunk("Result.")

        brain, mock_client = _make_brain_with_mock([])
        mock_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=fake_stream
        )

        async for _ in brain.stream("recall x", history=[]):
            pass

        assert call_count == 2
        # The second call should have a function-response content appended
        assert second_call_contents is not None
        assert len(second_call_contents) >= 2  # user turn + function-response turn


# ---------------------------------------------------------------------------
# Tests: no-op stubs
# ---------------------------------------------------------------------------

class TestNoopStubs:
    @pytest.mark.asyncio
    async def test_noop_memory_remember_returns_string(
        self, noop_memory: _NoopMemory
    ) -> None:
        result = await noop_memory.remember("k", "v")
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_noop_memory_recall_returns_string(
        self, noop_memory: _NoopMemory
    ) -> None:
        result = await noop_memory.recall("query")
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_noop_os_action_returns_string(self, noop_os: _NoopOS) -> None:
        result = await noop_os.os_action("click", {"x": 0})
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_noop_web_task_returns_string(self, noop_web: _NoopWeb) -> None:
        result = await noop_web.web_task("search", url="")
        assert isinstance(result, str)

    def test_noop_memory_satisfies_protocol(self, noop_memory: _NoopMemory) -> None:
        assert isinstance(noop_memory, MemoryTool)

    def test_noop_os_satisfies_protocol(self, noop_os: _NoopOS) -> None:
        assert isinstance(noop_os, OSTool)

    def test_noop_web_satisfies_protocol(self, noop_web: _NoopWeb) -> None:
        assert isinstance(noop_web, WebTool)


# ---------------------------------------------------------------------------
# Tests: gemini_model field in Settings
# ---------------------------------------------------------------------------

class TestGeminiModelConfig:
    def test_default_gemini_model(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """GEMINI_MODEL defaults to 'gemini-2.5-flash' when not set."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        dotenv = tmp_path / ".env"
        dotenv.write_text("")

        cfg = Settings.from_env(dotenv_path=dotenv)
        assert cfg.gemini_model == "gemini-2.5-flash"

    def test_custom_gemini_model_from_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """GEMINI_MODEL env var overrides the default."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("GEMINI_MODEL", "gemini-2.0-flash")
        dotenv = tmp_path / ".env"
        dotenv.write_text("")

        cfg = Settings.from_env(dotenv_path=dotenv)
        assert cfg.gemini_model == "gemini-2.0-flash"

    def test_gemini_model_from_dotenv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """GEMINI_MODEL can be set in .env."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        dotenv = tmp_path / ".env"
        dotenv.write_text("GEMINI_MODEL=gemini-1.5-pro\n")

        cfg = Settings.from_env(dotenv_path=dotenv)
        assert cfg.gemini_model == "gemini-1.5-pro"

    def test_repr_includes_gemini_model(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Settings repr shows gemini_model."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        dotenv = tmp_path / ".env"
        dotenv.write_text("")

        cfg = Settings.from_env(dotenv_path=dotenv)
        assert "gemini-2.5-flash" in repr(cfg)

    def test_brain_uses_model_from_settings(self) -> None:
        """Brain reads gemini_model from Settings, not hardcoded."""
        cfg = _fake_settings(model="gemini-special")
        with patch("core.brain.genai.Client"):
            brain = Brain(cfg)
        assert brain._model == "gemini-special"


# ---------------------------------------------------------------------------
# Tests: tool-call depth limit (sentinel fix — must not regress)
# ---------------------------------------------------------------------------

class TestToolCallDepthLimit:
    @pytest.mark.asyncio
    async def test_depth_limit_stops_infinite_tool_chain(self) -> None:
        """Brain stops issuing tool-call round-trips at _MAX_TOOL_ROUNDS.

        A model that always returns a function_call (never plain text) must not
        recurse forever.  After _MAX_TOOL_ROUNDS rounds the generator halts
        without raising and without executing another API call.
        """
        call_count = 0

        async def always_tool_call(*args: Any, **kwargs: Any) -> AsyncIterator:
            nonlocal call_count
            call_count += 1
            # Always return a function-call — never plain text.
            yield _fc_chunk("recall", {"query": "infinite"})

        brain, mock_client = _make_brain_with_mock([])
        mock_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=always_tool_call
        )

        results: list[str] = []
        async for delta in brain.stream("loop forever", history=[]):
            results.append(delta)

        # The generator must terminate; no text was yielded (model never sent text).
        assert results == []
        # Must have called the API exactly _MAX_TOOL_ROUNDS + 1 times (initial + N recursions).
        assert call_count == _MAX_TOOL_ROUNDS + 1

    @pytest.mark.asyncio
    async def test_normal_tool_call_unaffected_by_depth_guard(self) -> None:
        """A single tool-call round-trip (depth=1) still works after the guard is added."""
        call_count = 0

        async def one_tool_then_text(*args: Any, **kwargs: Any) -> AsyncIterator:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield _fc_chunk("recall", {"query": "test"})
            else:
                yield _text_chunk("Here is the answer.")

        brain, mock_client = _make_brain_with_mock([])
        mock_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=one_tool_then_text
        )

        results: list[str] = []
        async for delta in brain.stream("test", history=[]):
            results.append(delta)

        assert results == ["Here is the answer."]
        assert call_count == 2


# ---------------------------------------------------------------------------
# Live test guard (not collected by default CI)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not __import__("os").environ.get("FRIDAY_LIVE"),
    reason="Live tests require FRIDAY_LIVE=1 and a real GEMINI_API_KEY",
)
@pytest.mark.asyncio
async def test_brain_live_smoke() -> None:  # pragma: no cover
    """Live integration test — run with FRIDAY_LIVE=1.

    Not run in CI. Atlas runs scripts/smoke_brain.py for the full live smoke.
    """
    from core.config import load_settings

    settings = load_settings()
    brain = Brain(settings)

    results: list[str] = []
    async for delta in brain.stream("Say hello in 5 words.", history=[]):
        results.append(delta)

    full_text = "".join(results)
    assert len(full_text) > 0, "Expected non-empty response from live Gemini API"
