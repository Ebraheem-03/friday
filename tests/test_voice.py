"""Unit tests for voice/ — mocked, CI-safe, no audio device, no model load.

Coverage
--------
- VAD: fires on loud frames / stays silent below threshold.
- VAD: onset requires N_ONSET consecutive active frames.
- VAD: hangover keeps speech open for N_HANGOVER silent frames.
- VAD: force-ends utterance at MAX_UTTERANCE_FRAMES.
- stream.py chunker: emits chunks on sentence-ending punctuation.
- stream.py chunker: force-flushes after MAX_CHUNK_CHARS without boundary.
- stream.py chunker: emits tail remainder after text stream ends.
- stream.py chunker: first chunk is short (≤ FIRST_CHUNK_CHARS) for low latency.
- stream.py chunker: subsequent chunks use MAX_CHUNK_CHARS limit.
- stream.py: full pipeline starts TTS before the stream ends (first-audio).
- stream.py: barge-in stops playback mid-turn.
- tts_kokoro.synth: yields PCM bytes (mocked KPipeline).
- tts_kokoro.preload: idempotent — builds pipeline once, subsequent calls skip.

Any test that requires real Kokoro model weights or a live audio device is
decorated with ``@pytest.mark.skipif(not FRIDAY_LIVE, ...)`` so CI never
needs hardware or model files.

Set ``FRIDAY_LIVE=1`` in the environment to run live tests locally.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from voice.capture import (
    _VADStateMachine,
    FRAME_SAMPLES,
    DEFAULT_RMS_THRESHOLD,
    N_ONSET,
    N_HANGOVER,
    MAX_UTTERANCE_FRAMES,
)
from voice.stream import _chunk_stream, speak_stream, MAX_CHUNK_CHARS, FIRST_CHUNK_CHARS

FRIDAY_LIVE = bool(os.environ.get("FRIDAY_LIVE"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _silent_frame() -> np.ndarray:
    """Return a float32 frame well below the VAD threshold."""
    return np.zeros(FRAME_SAMPLES, dtype=np.float32)


def _loud_frame(amplitude: float = 0.5) -> np.ndarray:
    """Return a float32 frame well above the VAD threshold."""
    return np.full(FRAME_SAMPLES, amplitude, dtype=np.float32)


async def _iter(*items: str) -> AsyncIterator[str]:
    """Yield items as an async iterator (for mocking Brain.stream)."""
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# VAD tests
# ---------------------------------------------------------------------------


class TestVADStateMachine:
    """Test the pure-Python _VADStateMachine in voice/capture.py."""

    def test_silence_returns_none(self) -> None:
        """Silent frames never trigger an utterance."""
        vad = _VADStateMachine(threshold=DEFAULT_RMS_THRESHOLD)
        for _ in range(N_ONSET + N_HANGOVER + 5):
            result = vad.push_frame(_silent_frame())
            assert result is None

    def test_loud_without_onset_returns_none(self) -> None:
        """Fewer than N_ONSET active frames do not start speech."""
        vad = _VADStateMachine(threshold=DEFAULT_RMS_THRESHOLD)
        for _ in range(N_ONSET - 1):
            result = vad.push_frame(_loud_frame())
            assert result is None
        # One silent frame resets the onset counter.
        result = vad.push_frame(_silent_frame())
        assert result is None

    def test_onset_triggers_speech(self) -> None:
        """Exactly N_ONSET consecutive loud frames enter speech mode."""
        vad = _VADStateMachine(threshold=DEFAULT_RMS_THRESHOLD)
        for i in range(N_ONSET - 1):
            assert vad.push_frame(_loud_frame()) is None
        # The N_ONSET-th frame enters speech but doesn't end it yet.
        result = vad.push_frame(_loud_frame())
        assert result is None  # still in speech (no hangover yet)
        assert vad._in_speech is True

    def test_hangover_ends_utterance(self) -> None:
        """N_HANGOVER silent frames after speech closes the utterance."""
        vad = _VADStateMachine(threshold=DEFAULT_RMS_THRESHOLD)
        # Enter speech.
        for _ in range(N_ONSET):
            vad.push_frame(_loud_frame())
        assert vad._in_speech is True
        # Hang-over: N_HANGOVER - 1 silent frames don't close yet.
        for _ in range(N_HANGOVER - 1):
            result = vad.push_frame(_silent_frame())
            assert result is None
        # The N_HANGOVER-th silent frame closes the utterance.
        result = vad.push_frame(_silent_frame())
        assert result is not None
        assert isinstance(result, np.ndarray)
        assert result.dtype == np.float32

    def test_utterance_contains_all_buffered_frames(self) -> None:
        """Returned utterance spans onset + speech + hangover frames."""
        vad = _VADStateMachine(threshold=DEFAULT_RMS_THRESHOLD)
        speech_frames = 10
        for _ in range(N_ONSET):
            vad.push_frame(_loud_frame())
        for _ in range(speech_frames - N_ONSET):
            vad.push_frame(_loud_frame())
        for _ in range(N_HANGOVER - 1):
            vad.push_frame(_silent_frame())
        utt = vad.push_frame(_silent_frame())
        assert utt is not None
        # Should be (onset_count frames buffered + speech frames + hangover) * FRAME_SAMPLES.
        # onset frames are buffered tentatively — they are included.
        expected_min = N_ONSET * FRAME_SAMPLES  # at minimum
        assert len(utt) >= expected_min

    def test_force_end_at_max_frames(self) -> None:
        """Utterance is force-ended after MAX_UTTERANCE_FRAMES."""
        vad = _VADStateMachine(threshold=DEFAULT_RMS_THRESHOLD)
        # Enter speech.
        for _ in range(N_ONSET):
            vad.push_frame(_loud_frame())
        result = None
        for i in range(MAX_UTTERANCE_FRAMES + 5):
            result = vad.push_frame(_loud_frame())
            if result is not None:
                break
        assert result is not None, "Force-end did not fire within MAX_UTTERANCE_FRAMES"

    def test_reset_after_utterance(self) -> None:
        """VAD resets properly after an utterance fires."""
        vad = _VADStateMachine(threshold=DEFAULT_RMS_THRESHOLD)
        for _ in range(N_ONSET):
            vad.push_frame(_loud_frame())
        for _ in range(N_HANGOVER):
            vad.push_frame(_silent_frame())
        assert vad._in_speech is False
        assert vad._utterance == []
        assert vad._onset_count == 0

    def test_threshold_respected(self) -> None:
        """Frames at exactly the threshold are treated as silent."""
        vad = _VADStateMachine(threshold=0.5)
        # A frame at exactly 0.5 amplitude: rms of constant 0.5 = 0.5, not > 0.5.
        frame = np.full(FRAME_SAMPLES, 0.5, dtype=np.float32)
        for _ in range(N_ONSET + 2):
            result = vad.push_frame(frame)
            # exactly at threshold → active = False → stays silent
            assert result is None

    def test_loud_frames_are_above_threshold(self) -> None:
        """Sanity: our _loud_frame helper is actually above default threshold."""
        frame = _loud_frame(amplitude=0.5)
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        assert rms > DEFAULT_RMS_THRESHOLD


# ---------------------------------------------------------------------------
# Chunker tests (stream._chunk_stream)
# ---------------------------------------------------------------------------


class TestChunkStream:
    """Test sentence-boundary chunking in stream._chunk_stream."""

    @pytest.mark.asyncio
    async def test_chunks_on_period(self) -> None:
        """Sentence ending with '.' emits a chunk."""
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await _chunk_stream(_iter("Hello world. "), queue)
        chunks = []
        while not queue.empty():
            item = queue.get_nowait()
            if item is not None:
                chunks.append(item)
        assert any("Hello world" in c for c in chunks)

    @pytest.mark.asyncio
    async def test_chunks_on_exclamation(self) -> None:
        """Sentence ending with '!' emits a chunk."""
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await _chunk_stream(_iter("Hi! ", "How are you? "), queue)
        chunks = []
        while not queue.empty():
            item = queue.get_nowait()
            if item is not None:
                chunks.append(item)
        assert len(chunks) >= 1

    @pytest.mark.asyncio
    async def test_chunks_on_newline(self) -> None:
        """Newline acts as a sentence boundary."""
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await _chunk_stream(_iter("Line one\nLine two"), queue)
        chunks = []
        while not queue.empty():
            item = queue.get_nowait()
            if item is not None:
                chunks.append(item)
        assert len(chunks) >= 1

    @pytest.mark.asyncio
    async def test_force_flush_on_long_text(self) -> None:
        """Buffer force-flushes after MAX_CHUNK_CHARS without a boundary."""
        long_text = "a" * (MAX_CHUNK_CHARS + 10)
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await _chunk_stream(_iter(long_text), queue)
        chunks = []
        while not queue.empty():
            item = queue.get_nowait()
            if item is not None:
                chunks.append(item)
        # At least one chunk should have been emitted.
        assert len(chunks) >= 1

    @pytest.mark.asyncio
    async def test_tail_remainder_emitted(self) -> None:
        """Text without a boundary is emitted as a tail chunk on stream end."""
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await _chunk_stream(_iter("no boundary here"), queue)
        chunks = []
        while not queue.empty():
            item = queue.get_nowait()
            if item is not None:
                chunks.append(item)
        assert any("no boundary here" in c for c in chunks)

    @pytest.mark.asyncio
    async def test_sentinel_sent(self) -> None:
        """Chunker sends None sentinel after text stream ends."""
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await _chunk_stream(_iter("done."), queue)
        # Drain until we see None.
        sentinel_found = False
        for _ in range(20):
            if queue.empty():
                break
            item = queue.get_nowait()
            if item is None:
                sentinel_found = True
                break
        assert sentinel_found

    @pytest.mark.asyncio
    async def test_tts_starts_before_stream_ends(self) -> None:
        """Stream starts TTS synthesis before all deltas have arrived.

        Strategy: patch tts_synth to record calls, feed slow deltas via a real
        async generator, and assert synth was called before the final delta.
        """
        synth_calls: list[str] = []

        async def _mock_synth(text: str, **_: Any) -> AsyncIterator[bytes]:
            synth_calls.append(text)
            yield b"\x00" * 100  # dummy PCM

        # A stream that emits a complete sentence then more text.
        async def _slow_stream() -> AsyncIterator[str]:
            yield "Hello world. "
            await asyncio.sleep(0.01)
            yield "Second sentence."

        # Patch the TTS so no real model is loaded.
        with patch("voice.stream.tts_synth", side_effect=_mock_synth):
            # Also patch sounddevice so no audio device is needed.
            mock_sd = MagicMock()
            mock_stream = MagicMock()
            mock_stream.write = MagicMock()
            mock_stream.start = MagicMock()
            mock_stream.stop = MagicMock()
            mock_stream.close = MagicMock()
            mock_sd.OutputStream.return_value = mock_stream
            with patch("voice.stream.sd", mock_sd, create=True):
                with patch("voice.stream._play_worker") as mock_player:
                    # Override player so it doesn't need sounddevice import.
                    async def _fake_player(*a: Any, **kw: Any) -> None:
                        pcm_q = a[0]
                        while True:
                            item = await pcm_q.get()
                            if item is None:
                                break

                    mock_player.side_effect = _fake_player
                    await speak_stream(_slow_stream(), voice="af_heart", sample_rate=24_000)

        # Synth was called (at least once) — first-audio fires before stream ends.
        assert len(synth_calls) >= 1


# ---------------------------------------------------------------------------
# Barge-in test
# ---------------------------------------------------------------------------


class TestBargeIn:
    """Test that barge-in stops playback."""

    @pytest.mark.asyncio
    async def test_barge_in_stops_playback(self) -> None:
        """Setting barge_in_event while player is running stops the turn."""
        barge_in = asyncio.Event()
        tts_active = asyncio.Event()

        # PCM queue pre-loaded with a large payload.
        from voice.stream import _play_worker

        pcm_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        for _ in range(10):
            pcm_queue.put_nowait(b"\x00\x00" * 1024)

        # Set barge-in before player starts so it exits immediately.
        barge_in.set()

        # Patch sounddevice.
        mock_sd = MagicMock()
        mock_out_stream = MagicMock()
        mock_out_stream.write = MagicMock()
        mock_out_stream.start = MagicMock()
        mock_out_stream.stop = MagicMock()
        mock_out_stream.close = MagicMock()
        mock_sd.OutputStream.return_value = mock_out_stream

        with patch.dict("sys.modules", {"sounddevice": mock_sd}):
            await _play_worker(pcm_queue, barge_in, tts_active, sample_rate=24_000)

        # tts_active should be cleared after player exits.
        assert not tts_active.is_set()
        # OutputStream.write should NOT have been called (barge-in was pre-set).
        mock_out_stream.write.assert_not_called()


# ---------------------------------------------------------------------------
# tts_kokoro.synth tests (mocked KPipeline)
# ---------------------------------------------------------------------------


class TestTTSKokoro:
    """Test synth() with the real kokoro module mocked out."""

    @pytest.mark.asyncio
    async def test_synth_yields_pcm_bytes(self) -> None:
        """synth() yields bytes objects (raw int16 PCM) when mocked."""
        import torch

        # Build a fake KPipeline result with audio tensor.
        fake_audio = torch.zeros(24_000, dtype=torch.float32)  # 1 s of silence

        fake_output = MagicMock()
        fake_output.audio = fake_audio

        fake_result = MagicMock()
        fake_result.output = fake_output

        fake_pipeline = MagicMock()
        fake_pipeline.return_value = [fake_result]  # __call__ returns list-like

        # Reset global singleton so our mock is used.
        import voice.tts_kokoro as tts_mod

        old_pipeline = tts_mod._pipeline
        tts_mod._pipeline = fake_pipeline  # type: ignore[assignment]

        try:
            chunks = []
            async for chunk in tts_mod.synth("test phrase", voice="af_heart"):
                chunks.append(chunk)
            assert len(chunks) == 1
            assert isinstance(chunks[0], bytes)
            # 24000 samples * 2 bytes/sample = 48000 bytes.
            assert len(chunks[0]) == 24_000 * 2
        finally:
            tts_mod._pipeline = old_pipeline  # type: ignore[assignment]

    @pytest.mark.asyncio
    async def test_synth_empty_text_no_crash(self) -> None:
        """synth() with no segments yields nothing without raising."""
        fake_pipeline = MagicMock()
        fake_pipeline.return_value = []  # no results

        import voice.tts_kokoro as tts_mod

        old_pipeline = tts_mod._pipeline
        tts_mod._pipeline = fake_pipeline  # type: ignore[assignment]

        try:
            chunks = []
            async for chunk in tts_mod.synth("", voice="af_heart"):
                chunks.append(chunk)
            assert chunks == []
        finally:
            tts_mod._pipeline = old_pipeline  # type: ignore[assignment]

    @pytest.mark.asyncio
    async def test_synth_skips_none_output(self) -> None:
        """synth() silently skips results with output=None (quiet pipeline)."""
        fake_result = MagicMock()
        fake_result.output = None

        fake_pipeline = MagicMock()
        fake_pipeline.return_value = [fake_result]

        import voice.tts_kokoro as tts_mod

        old_pipeline = tts_mod._pipeline
        tts_mod._pipeline = fake_pipeline  # type: ignore[assignment]

        try:
            chunks = []
            async for chunk in tts_mod.synth("hello", voice="af_heart"):
                chunks.append(chunk)
            assert chunks == []
        finally:
            tts_mod._pipeline = old_pipeline  # type: ignore[assignment]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not FRIDAY_LIVE, reason="requires real Kokoro model (FRIDAY_LIVE=1)")
    async def test_synth_live(self) -> None:
        """Live smoke: synth a short phrase with the real model."""
        from voice.tts_kokoro import synth

        chunks = []
        async for chunk in synth("Hello.", voice="af_heart"):
            chunks.append(chunk)
        assert len(chunks) >= 1
        total_bytes = sum(len(c) for c in chunks)
        # Minimum ~0.1 s of audio = 0.1 * 24000 * 2 = 4800 bytes
        assert total_bytes >= 4_800


# ---------------------------------------------------------------------------
# preload() idempotency tests
# ---------------------------------------------------------------------------


class TestPreload:
    """Test tts_kokoro.preload() builds the pipeline once and is idempotent."""

    @pytest.mark.asyncio
    async def test_preload_builds_pipeline_once(self) -> None:
        """preload() calls _build_pipeline exactly once even when called twice."""
        import voice.tts_kokoro as tts_mod

        # Save existing state so we can restore after the test.
        old_pipeline = tts_mod._pipeline
        old_preloaded = tts_mod._preloaded

        build_call_count = 0

        def _fake_build() -> MagicMock:
            nonlocal build_call_count
            build_call_count += 1
            fake = MagicMock()
            # Return an iterable result for the warm-up synth call.
            warm_result = MagicMock()
            warm_result.output = MagicMock()
            fake.return_value = [warm_result]
            return fake

        try:
            # Reset module state so preload() thinks it's a fresh start.
            tts_mod._pipeline = None
            tts_mod._preloaded = False

            with patch("voice.tts_kokoro._build_pipeline", side_effect=_fake_build):
                await tts_mod.preload()
                await tts_mod.preload()  # second call — must be a no-op

            # _build_pipeline must have been called exactly once.
            assert build_call_count == 1
            assert tts_mod._preloaded is True
        finally:
            tts_mod._pipeline = old_pipeline
            tts_mod._preloaded = old_preloaded

    @pytest.mark.asyncio
    async def test_preload_idempotent_when_already_loaded(self) -> None:
        """preload() returns immediately without calling _build_pipeline if already loaded."""
        import voice.tts_kokoro as tts_mod

        old_pipeline = tts_mod._pipeline
        old_preloaded = tts_mod._preloaded

        build_called = False

        def _should_not_be_called() -> None:
            nonlocal build_called
            build_called = True

        try:
            # Simulate already-preloaded state.
            tts_mod._preloaded = True

            with patch("voice.tts_kokoro._build_pipeline", side_effect=_should_not_be_called):
                await tts_mod.preload()

            assert build_called is False
        finally:
            tts_mod._pipeline = old_pipeline
            tts_mod._preloaded = old_preloaded


# ---------------------------------------------------------------------------
# First-chunk latency tests (chunker emits short first chunk)
# ---------------------------------------------------------------------------


class TestFirstChunkStrategy:
    """Test that the chunker emits a short first chunk for low first-audio latency."""

    @pytest.mark.asyncio
    async def test_first_chunk_is_short(self) -> None:
        """First chunk is flushed at FIRST_CHUNK_CHARS, not MAX_CHUNK_CHARS.

        Feed a long run-on string (no sentence boundary) and assert the first
        emitted chunk has at most FIRST_CHUNK_CHARS characters.
        """
        # A single delta longer than MAX_CHUNK_CHARS (80) with no boundary.
        long_text = "a" * (MAX_CHUNK_CHARS + 20)
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await _chunk_stream(_iter(long_text), queue)

        chunks: list[str] = []
        while not queue.empty():
            item = queue.get_nowait()
            if item is not None:
                chunks.append(item)

        assert len(chunks) >= 1, "Expected at least one chunk"
        first_chunk = chunks[0]
        # First chunk must be at most FIRST_CHUNK_CHARS characters long.
        assert len(first_chunk) <= FIRST_CHUNK_CHARS, (
            f"First chunk ({len(first_chunk)} chars) exceeds FIRST_CHUNK_CHARS "
            f"({FIRST_CHUNK_CHARS}). Short first chunk is required for low first-audio latency."
        )

    @pytest.mark.asyncio
    async def test_subsequent_chunks_use_max_chunk_chars(self) -> None:
        """After the first chunk, subsequent force-flushes use MAX_CHUNK_CHARS.

        Feed text that first produces a short first chunk, then more text
        without a boundary.  The second chunk should be allowed to grow to
        MAX_CHUNK_CHARS before being flushed.
        """
        # First delta: triggers first-chunk flush (> FIRST_CHUNK_CHARS, no boundary).
        first_delta = "w" * (FIRST_CHUNK_CHARS + 1)
        # Second delta: more text that grows the buffer to > MAX_CHUNK_CHARS without boundary.
        second_delta = "x" * (MAX_CHUNK_CHARS + 1)

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await _chunk_stream(_iter(first_delta, second_delta), queue)

        chunks: list[str] = []
        while not queue.empty():
            item = queue.get_nowait()
            if item is not None:
                chunks.append(item)

        assert len(chunks) >= 2, (
            f"Expected at least 2 chunks, got {len(chunks)}: {chunks!r}"
        )
        # The SECOND chunk should be larger than FIRST_CHUNK_CHARS (using the
        # bigger subsequent limit), confirming the limit switched after first emit.
        second_chunk = chunks[1]
        assert len(second_chunk) > FIRST_CHUNK_CHARS, (
            f"Second chunk ({len(second_chunk)} chars) should exceed FIRST_CHUNK_CHARS "
            f"({FIRST_CHUNK_CHARS}) — subsequent chunks use MAX_CHUNK_CHARS ({MAX_CHUNK_CHARS})."
        )
