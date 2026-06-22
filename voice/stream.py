"""Continuous streaming: chunk Gemini token stream → TTS → audio playback.

ADR-0005 target: first-audio < 1 s from first text delta.

Architecture
------------
The pipeline has three concurrent stages running via asyncio tasks:

    [Brain.stream] ──tokens──► [Chunker] ──sentences──► [TTS] ──PCM──► [Player]

Stage 1 — Chunker (``_chunk_stream``)
    Accumulates incoming text deltas into a rolling buffer.  A chunk is emitted
    when the buffer contains a sentence-ending boundary (``. ! ? \\n``).

    FIRST-CHUNK STRATEGY: The first chunk is flushed early — after
    FIRST_CHUNK_CHARS (20) chars — even without a sentence boundary.  This is
    critical for first-audio latency: Kokoro synthesises a whole chunk
    atomically, so shorter first chunk = lower first-audio (~1.0–1.3 s steady-
    state on CPU).  Subsequent chunks use MAX_CHUNK_CHARS (80) for naturalness.

    Force-flush fires at the active limit (FIRST_CHUNK_CHARS before first emit,
    MAX_CHUNK_CHARS thereafter) to keep latency bounded on long Gemini replies.

Stage 2 — TTS (``_tts_worker``)
    Consumes sentence chunks from the chunk queue, calls ``tts_kokoro.synth()``
    per chunk, and pushes the resulting PCM bytes onto the playback queue.
    Because ``synth()`` runs Kokoro in a thread-pool executor, the asyncio loop
    is never blocked.

Stage 3 — Player (``_play_worker``)
    Reads PCM chunks from the playback queue and writes them to a
    ``sounddevice.OutputStream``.  Monitors ``barge_in_event``; if set, drains
    the queue and returns immediately, aborting the current turn's audio.

Latency notes (MEASURED 2026-06-22)
------------------------------------
- Model cold-start: ~14 s (one-time, paid at boot via ``tts_kokoro.preload()``).
  This MUST NOT land on the first user utterance — call preload() at startup.
- Steady-state first-audio: ~1.0–1.3 s for a short first chunk (~3–6 words).
  Strict <1 s is marginal on CPU; keeping FIRST_CHUNK_CHARS small (≤20) is the
  primary knob. Do not raise it without re-measuring.
- Gemini first token → first boundary:             ~200–500 ms
- Boundary → Kokoro synth (CPU, short chunk):      ~500–800 ms (first emission)
- PortAudio buffer start:                           ~5–20 ms
- Total worst-case (short first chunk):             ~700–1320 ms

Buffer sizes
------------
- ``_CHUNK_QUEUE_MAXSIZE = 4``:   max 4 pending sentence chunks queued to TTS.
  Small so backpressure stalls chunker rather than building unbounded latency.
- ``_PCM_QUEUE_MAXSIZE = 8``:     max 8 PCM chunks queued to player (~2–4 s of
  speech at typical sentence length).  OutputStream.write() is the consumer.
- ``FIRST_CHUNK_CHARS = 20``:     force-flush the FIRST chunk after ≤20 chars
  (roughly 3–6 words).  Short first chunk minimises first-audio latency on CPU.
- ``MAX_CHUNK_CHARS = 80``:       force-flush subsequent chunks after 80 chars.
  Larger than FIRST_CHUNK_CHARS to allow natural sentence-length audio later.

sounddevice API used (sounddevice 0.5.5)
    ``sd.OutputStream(samplerate, channels=1, dtype="int16", latency="low")``
    ``stream.write(pcm_array)``  — blocking write; called from asyncio executor.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator

import numpy as np

from voice.tts_kokoro import synth as tts_synth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants — documented above.
# ---------------------------------------------------------------------------

_CHUNK_QUEUE_MAXSIZE: int = 4
_PCM_QUEUE_MAXSIZE: int = 8

# First-chunk limit: flush early (3–6 words) to minimise first-audio latency.
# Kokoro synthesises a whole chunk atomically; shorter first chunk = lower
# first-audio.  Measured steady-state: ~1.0–1.3 s at 20 chars on this CPU.
FIRST_CHUNK_CHARS: int = 20

# Subsequent chunks: larger for natural sentence-length audio.
MAX_CHUNK_CHARS: int = 80       # force-flush after this many chars without boundary

# Sentence/clause boundary: sentence-ending punctuation followed by whitespace.
# NOTE: the `$` end-anchor is intentionally absent.  The original regex included
# `|$` so that tail text without a boundary would still be emitted; but that
# causes the `$` to match any partial buffer and emit it immediately, defeating
# the force-flush size limits.  Tail text is handled by the explicit "Flush any
# remainder" block after the async-for loop.
_BOUNDARY_RE = re.compile(r"(?<=[.!?\n])\s")


# ---------------------------------------------------------------------------
# Stage 1: Chunker
# ---------------------------------------------------------------------------


async def _chunk_stream(
    text_stream: AsyncIterator[str],
    chunk_queue: "asyncio.Queue[str | None]",
) -> None:
    """Read text deltas, emit sentence-boundary chunks onto *chunk_queue*.

    First-chunk strategy: the very first chunk is force-flushed after
    FIRST_CHUNK_CHARS (20) chars so that Kokoro starts synthesising a short
    phrase as early as possible.  Subsequent chunks use MAX_CHUNK_CHARS (80)
    to allow natural sentence-length audio.

    Sends ``None`` as a sentinel when the text stream is exhausted.
    """
    buf = ""
    first_chunk_emitted = False
    async for delta in text_stream:
        buf += delta
        # Drain the buffer: emit chunks until we need more text.
        while True:
            flush_limit = FIRST_CHUNK_CHARS if not first_chunk_emitted else MAX_CHUNK_CHARS

            # Force-flush takes priority over boundary detection when the buffer
            # exceeds the active limit.  This ensures the first chunk is always
            # capped at FIRST_CHUNK_CHARS regardless of whether a sentence boundary
            # (including the `$` end-anchor) exists later in the buffer.
            if len(buf) >= flush_limit:
                # Flush exactly flush_limit chars to respect the size cap,
                # then continue the inner loop to drain the remainder.
                chunk = buf[:flush_limit].strip()
                buf = buf[flush_limit:]
                if chunk:
                    logger.debug(
                        "Chunk (force-flush/%s): %r",
                        "first" if not first_chunk_emitted else "subsequent",
                        chunk[:60],
                    )
                    await chunk_queue.put(chunk)
                    first_chunk_emitted = True
                continue  # check remaining buf against new (larger) limit

            # Buffer is below the active limit — look for a sentence boundary.
            match = _BOUNDARY_RE.search(buf)
            if match and match.start() > 0:
                # Emit everything up to (and including) the boundary.
                boundary_end = match.start() + len(match.group(0))
                chunk = buf[:boundary_end].strip()
                buf = buf[boundary_end:]
                if chunk:
                    logger.debug("Chunk (boundary): %r", chunk[:60])
                    await chunk_queue.put(chunk)
                    first_chunk_emitted = True
            else:
                break  # buffer is small and has no boundary — wait for more text

    # Flush any remainder.
    remainder = buf.strip()
    if remainder:
        logger.debug("Chunk (tail): %r", remainder[:60])
        await chunk_queue.put(remainder)

    # Sentinel: no more chunks.
    await chunk_queue.put(None)


# ---------------------------------------------------------------------------
# Stage 2: TTS worker
# ---------------------------------------------------------------------------


async def _tts_worker(
    chunk_queue: "asyncio.Queue[str | None]",
    pcm_queue: "asyncio.Queue[bytes | None]",
    voice: str,
    sample_rate: int,
) -> None:
    """Consume sentence chunks, synthesise, push PCM onto *pcm_queue*."""
    while True:
        chunk = await chunk_queue.get()
        if chunk is None:
            break  # text stream exhausted
        logger.debug("TTS synthesising %d chars", len(chunk))
        async for pcm_bytes in tts_synth(chunk, voice=voice, sample_rate=sample_rate):
            await pcm_queue.put(pcm_bytes)

    # Sentinel: no more audio.
    await pcm_queue.put(None)


# ---------------------------------------------------------------------------
# Stage 3: Player
# ---------------------------------------------------------------------------


async def _play_worker(
    pcm_queue: "asyncio.Queue[bytes | None]",
    barge_in_event: asyncio.Event,
    tts_active: asyncio.Event,
    sample_rate: int,
) -> None:
    """Write PCM chunks to sounddevice OutputStream; stop on barge-in."""
    try:
        import sounddevice as sd  # type: ignore[import]
    except (ImportError, OSError):
        logger.warning(
            "sounddevice / PortAudio not available — audio playback disabled."
        )
        # Drain queue silently so the pipeline doesn't deadlock.
        while True:
            item = await pcm_queue.get()
            if item is None:
                return

    loop = asyncio.get_running_loop()

    # Open a low-latency output stream for the entire turn.
    try:
        stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
            latency="low",
        )
        stream.start()
    except Exception as exc:
        logger.warning("Failed to open audio output stream: %s", exc)
        while True:
            item = await pcm_queue.get()
            if item is None:
                return
        return

    tts_active.set()
    logger.info("Playback started (sample_rate=%d Hz)", sample_rate)

    try:
        while True:
            if barge_in_event.is_set():
                logger.info("Barge-in: stopping playback early.")
                break

            pcm_bytes = await pcm_queue.get()
            if pcm_bytes is None:
                break  # synthesis complete

            # Write to PortAudio in executor so event-loop stays responsive.
            pcm_arr = np.frombuffer(pcm_bytes, dtype=np.int16)
            await loop.run_in_executor(None, stream.write, pcm_arr)
    finally:
        stream.stop()
        stream.close()
        tts_active.clear()
        logger.info("Playback finished.")


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def speak_stream(
    text_stream: AsyncIterator[str],
    *,
    voice: str = "af_heart",
    sample_rate: int = 24_000,
    tts_active: asyncio.Event | None = None,
    barge_in_event: asyncio.Event | None = None,
) -> None:
    """Drive the full voice pipeline for one Brain response turn.

    Consumes *text_stream* (an async generator of text deltas from
    ``Brain.stream()``), chunks on sentence boundaries, synthesises each chunk
    with Kokoro-82M, and plays the resulting 24 kHz PCM via sounddevice.

    Returns when the response is fully spoken, or immediately after a barge-in.

    Parameters
    ----------
    text_stream:
        Async generator of text deltas (e.g. from ``Brain.stream()``).
    voice:
        Kokoro voice ID.  Default matches ``Settings.tts_voice``.
    sample_rate:
        Output sample rate in Hz.  Must match the sounddevice stream and Kokoro
        native rate (24 000 Hz).
    tts_active:
        External event set while TTS is playing (monitored by capture.py for
        barge-in detection).  If not provided, a local event is used.
    barge_in_event:
        External event set by capture.py on barge-in.  The player monitors this
        to abort playback.  If not provided, a local event is used (meaning
        barge-in cannot be triggered externally).

    Latency (first-audio)
    ----------------------
    < 1 s from first text delta is the ADR-0005 target.  MEASURED 2026-06-22:
      - Model cold-start (one-time):     ~14 s (paid at boot via preload()).
      - Sentence boundary / first-chunk: 200–500 ms  (Gemini streaming speed)
      - Kokoro synthesis (CPU, ≤20 chars, ~3–6 words): ~500–800 ms
      - PortAudio buffer start:           5–20 ms
    Total steady-state first-audio:      ~700–1320 ms
    Strict <1 s is marginal on CPU; FIRST_CHUNK_CHARS=20 is the primary tuning
    knob.  Ensure ``tts_kokoro.preload()`` is called at startup.
    """
    if tts_active is None:
        tts_active = asyncio.Event()
    if barge_in_event is None:
        barge_in_event = asyncio.Event()

    chunk_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_CHUNK_QUEUE_MAXSIZE)
    pcm_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_PCM_QUEUE_MAXSIZE)

    # Clear barge-in before starting a new turn.
    barge_in_event.clear()

    chunker_task = asyncio.create_task(
        _chunk_stream(text_stream, chunk_queue),
        name="voice.chunker",
    )
    tts_task = asyncio.create_task(
        _tts_worker(chunk_queue, pcm_queue, voice, sample_rate),
        name="voice.tts",
    )
    player_task = asyncio.create_task(
        _play_worker(pcm_queue, barge_in_event, tts_active, sample_rate),
        name="voice.player",
    )

    try:
        await asyncio.gather(chunker_task, tts_task, player_task)
    except asyncio.CancelledError:
        # Propagate cancellation; cancel sub-tasks.
        chunker_task.cancel()
        tts_task.cancel()
        player_task.cancel()
        await asyncio.gather(chunker_task, tts_task, player_task, return_exceptions=True)
        raise
    except Exception:
        logger.exception("speak_stream pipeline error")
        chunker_task.cancel()
        tts_task.cancel()
        player_task.cancel()
        await asyncio.gather(chunker_task, tts_task, player_task, return_exceptions=True)
        raise
