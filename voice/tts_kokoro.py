"""Kokoro-82M text-to-speech → 24 kHz mono PCM.

Real Kokoro 0.9.4 API (verified by source inspection 2026-06-22)
-----------------------------------------------------------------
Entry-point: ``kokoro.KPipeline``

    pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device="cpu")
    for result in pipeline(text, voice="af_heart", speed=1.0, split_pattern=r"\\n+"):
        audio: torch.FloatTensor = result.output.audio   # 1-D, float32, CPU
        # (result.graphemes, result.phonemes also available)

- ``KPipeline.__init__`` params: lang_code, repo_id, model (KModel|True|False), device.
  Pass ``device="cpu"`` to skip CUDA auto-detect (project rule: no CUDA).
- ``KPipeline.__call__`` params: text, voice, speed, split_pattern, model.
  Returns a *synchronous* ``Generator[KPipeline.Result]``.
- ``KPipeline.Result`` is a dataclass with fields:
    graphemes (str), phonemes (str), tokens, output (KModel.Output), text_index.
- ``KModel.Output.audio`` — 1-D ``torch.FloatTensor``, values in [-1, 1], CPU.
- Native sample rate: **24 000 Hz** (confirmed from ``istftnet.py``
  ``sampling_rate=24000`` and pipeline comment "pred_dur frames to sample_rate 24000").
  No resampling needed; Settings.sample_rate defaults to 24000.

Latency notes (MEASURED 2026-06-22 on this CPU)
------------------------------------------------
- First call (lazy load):  ~14 s — KPipeline construction, model weight download,
  spaCy/misaki G2P init.  This cost is now paid at STARTUP via ``preload()`` so
  it does NOT land on the first user utterance.
- Steady-state first-audio: ~1.0–1.3 s for a short clause (~3–6 words) on CPU.
  Strict <1 s is marginal on CPU; it depends on the first-chunk size delivered
  by stream.py.  A 3–6 word first chunk keeps us closest to the budget.
- ``synth()`` is an async generator that runs KPipeline in a thread-pool executor
  so the asyncio event-loop is never blocked.
- Each ``yield`` delivers a chunk of raw 16-bit little-endian PCM bytes
  corresponding to one synthesis segment (KPipeline chunks on ``\\n+`` by default,
  so caller should pre-split on sentence boundaries for lowest latency).

Startup contract
----------------
The orchestrator / voice entrypoint MUST call ``await preload()`` during boot
(before the first user turn) to pay the ~14 s model-load cost up-front.
``preload()`` is idempotent: subsequent calls return immediately.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy singleton — model loaded once per process lifetime.
# ---------------------------------------------------------------------------

_pipeline: "object | None" = None  # type: ignore[assignment]  — KPipeline at runtime
_pipeline_lock = asyncio.Lock()

# Kokoro English lang_code: 'a' = American English, 'b' = British English.
_LANG_CODE = "a"
_REPO_ID = "hexgrad/Kokoro-82M"


def _build_pipeline() -> "object":  # returns KPipeline
    """Synchronous model load — call inside executor only."""
    try:
        from kokoro import KPipeline  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "kokoro is not installed. Install the [voice] extra: "
            "pip install '.[voice]' -c constraints/cpu.txt"
        ) from exc

    logger.info("Loading Kokoro-82M pipeline (CPU, lang='%s') ...", _LANG_CODE)
    pipeline = KPipeline(
        lang_code=_LANG_CODE,
        repo_id=_REPO_ID,
        device="cpu",  # PROJECT RULE: no CUDA
    )
    logger.info("Kokoro pipeline ready.")
    return pipeline


async def _get_pipeline() -> "object":  # returns KPipeline
    """Return the shared singleton, building it on first call (thread-safe)."""
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    async with _pipeline_lock:
        # Double-check after acquiring the lock.
        if _pipeline is None:
            loop = asyncio.get_running_loop()
            _pipeline = await loop.run_in_executor(None, _build_pipeline)
    return _pipeline


# ---------------------------------------------------------------------------
# Startup preload — call this at boot to pay the ~14 s model-load cost
# ---------------------------------------------------------------------------

_preloaded: bool = False


async def preload(voice: str = "af_heart") -> None:
    """Load the Kokoro pipeline and run one throwaway synthesis at startup.

    This pays the ~14 s model-load + G2P-init cost at boot time so that the
    first real user utterance gets steady-state latency (~1.0–1.3 s) rather
    than the cold-start penalty.

    Idempotent: safe to call multiple times; the pipeline is built only once.

    The orchestrator / voice entrypoint MUST call::

        await tts_kokoro.preload()

    during boot, before the first user turn.

    Parameters
    ----------
    voice:
        Kokoro voice ID used for the throwaway warm-up synth.
        Matches the voice that will be used at runtime so the voice weights
        are also resident in memory.
    """
    global _preloaded
    if _preloaded:
        logger.debug("preload(): already loaded — skipping.")
        return

    logger.info("preload(): warming up Kokoro pipeline (pays ~14 s startup cost) ...")
    pipeline = await _get_pipeline()

    # Run one tiny throwaway synth so the voice weights and G2P graph are warm.
    # Using a single short word to minimise the warm-up time while still
    # exercising the full synthesis path (G2P → model → vocoder).
    loop = asyncio.get_running_loop()

    def _warm_up_synth() -> None:
        for result in pipeline("Hi.", voice=voice, speed=1.0, split_pattern=r"\n+"):  # type: ignore[call-arg]
            # We discard the audio — this is purely a warm-up.
            _ = result.output

    await loop.run_in_executor(None, _warm_up_synth)
    _preloaded = True
    logger.info("preload(): Kokoro pipeline warm and ready.")


# ---------------------------------------------------------------------------
# Public synthesis API
# ---------------------------------------------------------------------------


async def synth(
    text: str,
    *,
    voice: str = "af_heart",
    speed: float = 1.0,
    sample_rate: int = 24_000,
) -> AsyncIterator[bytes]:
    """Synthesise *text* and yield raw PCM chunks as they are ready.

    Parameters
    ----------
    text:
        Text to synthesise.  The KPipeline splits on ``\\n+`` internally;
        for lowest latency the caller (stream.py) should deliver short
        sentence-boundary chunks rather than full paragraphs.
    voice:
        Kokoro voice id.  Default matches ``Settings.tts_voice = "af_heart"``.
    speed:
        Playback speed multiplier (1.0 = normal).
    sample_rate:
        Target sample rate.  Kokoro natively outputs 24 000 Hz; if a different
        rate is requested the output is resampled with linear interpolation.
        Under normal operation this is always 24 000 → 24 000 (no-op).

    Yields
    ------
    bytes
        Raw 16-bit signed little-endian PCM audio at *sample_rate*.
        One chunk per KPipeline synthesis segment.

    Raises
    ------
    RuntimeError
        If kokoro is not installed or the model fails to load.
    """
    pipeline = await _get_pipeline()
    loop = asyncio.get_running_loop()

    def _run_synth() -> list[np.ndarray]:
        """Run KPipeline synchronously in the executor thread.

        Returns a list of float32 numpy arrays, one per segment.
        Uses duck-typing: anything callable with KPipeline's interface works,
        including MagicMock in tests.
        """
        chunks: list[np.ndarray] = []
        for result in pipeline(text, voice=voice, speed=speed, split_pattern=r"\n+"):  # type: ignore[call-arg]
            if result.output is None:
                continue  # quiet pipeline or empty segment
            # audio is a 1-D torch.FloatTensor on CPU
            audio_np: np.ndarray = result.output.audio.numpy()
            chunks.append(audio_np)
        return chunks

    # Run synthesis in a thread so the event-loop stays free.
    audio_chunks: list[np.ndarray] = await loop.run_in_executor(None, _run_synth)

    for audio_f32 in audio_chunks:
        # Resample only if the target rate differs from Kokoro's native 24 kHz.
        if sample_rate != 24_000:
            ratio = sample_rate / 24_000
            n_out = int(len(audio_f32) * ratio)
            audio_f32 = np.interp(
                np.linspace(0, len(audio_f32) - 1, n_out),
                np.arange(len(audio_f32)),
                audio_f32,
            ).astype(np.float32)

        # Convert float32 [-1, 1] → int16 PCM little-endian.
        clipped = np.clip(audio_f32, -1.0, 1.0)
        pcm_i16 = (clipped * 32767).astype(np.int16)
        yield pcm_i16.tobytes()


async def synth_to_numpy(
    text: str,
    *,
    voice: str = "af_heart",
    speed: float = 1.0,
) -> list[np.ndarray]:
    """Synthesise *text* and return a list of float32 numpy arrays (24 kHz).

    Convenience wrapper used by the smoke script and tests.
    """
    pipeline = await _get_pipeline()
    loop = asyncio.get_running_loop()

    def _run() -> list[np.ndarray]:
        chunks: list[np.ndarray] = []
        for result in pipeline(text, voice=voice, speed=speed, split_pattern=r"\n+"):  # type: ignore[call-arg]
            if result.output is None:
                continue
            chunks.append(result.output.audio.numpy())
        return chunks

    return await loop.run_in_executor(None, _run)
