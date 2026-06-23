"""Speech-to-text via faster-whisper (CPU-only, int8 quantised).

Design overview
---------------
Model
    ``faster-whisper`` wraps CTranslate2's Whisper implementation.
    We use ``WhisperModel(model_size, device="cpu", compute_type="int8")``:
    - ``device="cpu"`` — project rule: no CUDA (CLAUDE.md §3, cpu.txt).
    - ``compute_type="int8"`` — the correct CPU default; halves memory vs
      float32 with negligible WER impact on base.en.

Latency on CPU (base.en, measured rough estimate)
    Real-time factor ~0.3–0.5 × on a modern laptop CPU.  A 5-second utterance
    transcribes in ~1.5–2.5 s.  This sits in the STT slot of the pipeline —
    after VAD closes the utterance and before the brain receives text.

Preload contract (mirrors tts_kokoro.preload)
    The orchestrator / voice entrypoint MUST call::

        await stt.preload()

    at boot to pay the model-load cost (~2–5 s for base.en) before the first
    user utterance.  ``preload()`` is idempotent.

Security (CLAUDE.md §3)
    - Audio is NEVER written to disk — numpy array stays in memory only.
    - Transcript text is NEVER logged at INFO or above.  Only metadata
      (audio byte length, segment count, detected language) is logged at DEBUG.

CI safety
    faster-whisper is imported lazily (inside functions), so this module
    imports cleanly in CI without the [voice] extra installed.
    Absence of faster-whisper raises a clear RuntimeError at call time,
    not at import time.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy singleton — model loaded once per process lifetime.
# ---------------------------------------------------------------------------

_model: "object | None" = None  # WhisperModel at runtime
_model_lock = asyncio.Lock()

# Default model — good latency/accuracy balance on CPU.
# Configurable via Settings.stt_model (env: STT_MODEL).
DEFAULT_MODEL_SIZE: str = "base.en"


def _build_model(model_size: str) -> "object":  # returns WhisperModel
    """Synchronous model load — must be called inside an executor only.

    Parameters
    ----------
    model_size:
        faster-whisper model identifier, e.g. "base.en", "small", "medium.en".
        "base.en" is the default — good CPU latency with solid English accuracy.
    """
    try:
        from faster_whisper import WhisperModel  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed.  Install the [voice] extra:\n"
            "  pip install '.[voice]' -c constraints/cpu.txt\n"
            "faster-whisper requires Python >=3.8 and a CPU or CUDA device.  "
            "This project always uses CPU (device='cpu')."
        ) from exc

    logger.info(
        "Loading faster-whisper model '%s' (device=cpu, compute_type=int8) ...",
        model_size,
    )
    model = WhisperModel(
        model_size,
        device="cpu",          # PROJECT RULE: no CUDA
        compute_type="int8",   # Correct CPU default; halves memory vs float32
    )
    logger.info("faster-whisper model '%s' ready.", model_size)
    return model


async def _get_model(model_size: str = DEFAULT_MODEL_SIZE) -> "object":  # returns WhisperModel
    """Return the shared singleton, building it on first call (thread-safe).

    Parameters
    ----------
    model_size:
        Passed through to ``_build_model`` on first call; ignored on subsequent
        calls (the singleton is already built).
    """
    global _model
    if _model is not None:
        return _model
    async with _model_lock:
        # Double-check after acquiring the lock.
        if _model is None:
            loop = asyncio.get_running_loop()
            try:
                _model = await loop.run_in_executor(
                    None, _build_model, model_size
                )
            except ImportError as exc:
                # _build_model raises ImportError when faster-whisper is absent.
                # Re-raise as RuntimeError so callers always see a clear message.
                raise RuntimeError(
                    "faster-whisper is not installed.  Install the [voice] extra:\n"
                    "  pip install '.[voice]' -c constraints/cpu.txt"
                ) from exc
    return _model


# ---------------------------------------------------------------------------
# Startup preload — call this at boot to pay the model-load cost up-front.
# ---------------------------------------------------------------------------

_preloaded: bool = False


async def preload(model_size: str = DEFAULT_MODEL_SIZE) -> None:
    """Load the faster-whisper model at startup so the first utterance is fast.

    Idempotent: safe to call multiple times; the model is built only once.

    The orchestrator / voice entrypoint MUST call::

        await stt.preload()

    during boot, before the first user turn.  Loading base.en takes ~2–5 s on
    a modern CPU (network download on first run, then cached by HuggingFace).

    Parameters
    ----------
    model_size:
        faster-whisper model size to load.  Default: ``"base.en"``.
        Override via ``Settings.stt_model`` (env: ``STT_MODEL``).
    """
    global _preloaded
    if _preloaded:
        logger.debug("stt.preload(): already loaded — skipping.")
        return

    logger.info(
        "stt.preload(): loading faster-whisper '%s' (pays startup cost) ...",
        model_size,
    )
    await _get_model(model_size)
    _preloaded = True
    logger.info("stt.preload(): faster-whisper model ready.")


# ---------------------------------------------------------------------------
# Public transcription API
# ---------------------------------------------------------------------------

# Minimum audio length to bother transcribing.  Very short captures are
# almost certainly noise; returning "" immediately avoids a wasted model call.
# 0.1 s at 16 kHz int16 = 16000 * 0.1 * 2 = 3200 bytes.
_MIN_AUDIO_BYTES: int = 3_200


async def transcribe(
    pcm: bytes,
    *,
    sample_rate: int = 16_000,
    model_size: str = DEFAULT_MODEL_SIZE,
) -> str:
    """Transcribe raw int16 PCM bytes to text.

    Parameters
    ----------
    pcm:
        Raw 16-bit signed little-endian PCM at *sample_rate*.  This is the
        format yielded by ``voice.capture.capture_loop``.
    sample_rate:
        Sample rate of *pcm* in Hz.  Must match the rate used by the capture
        loop (default 16 000 Hz — the standard STT rate).
    model_size:
        faster-whisper model size.  Used only on the first call (to load the
        singleton); subsequent calls use the already-loaded model regardless of
        this value.

    Returns
    -------
    str
        Stripped transcript text, or ``""`` for empty / too-short audio.

    Notes
    -----
    - The blocking faster-whisper inference runs in a thread via
      ``asyncio.to_thread`` so the event loop is never blocked.
    - Audio never touches disk (CLAUDE.md §3).
    - Transcript text is never logged at INFO — only DEBUG metadata.
    """
    # Guard: very short audio → almost certainly noise, skip the model call.
    if not pcm or len(pcm) < _MIN_AUDIO_BYTES:
        logger.debug(
            "stt.transcribe(): audio too short (%d bytes < %d) — returning ''",
            len(pcm) if pcm else 0,
            _MIN_AUDIO_BYTES,
        )
        return ""

    # Convert int16 little-endian PCM → float32 normalised to [-1, 1].
    audio_f32: np.ndarray = (
        np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    )

    model = await _get_model(model_size)

    def _run_transcribe() -> tuple[list[str], str, float]:
        """Run faster-whisper in a thread (CPU-bound).

        Returns
        -------
        tuple of (segment_texts, language, language_probability)
        """
        # WhisperModel.transcribe returns (segments_generator, TranscriptionInfo).
        # segments_generator is lazy; we must consume it inside this thread.
        segments_gen, info = model.transcribe(  # type: ignore[attr-defined]
            audio_f32,
            beam_size=5,
            language=None,   # auto-detect; for English-only, caller can pass "en"
        )
        texts = [seg.text for seg in segments_gen]
        return texts, info.language, info.language_probability

    texts, language, lang_prob = await asyncio.to_thread(_run_transcribe)

    # Metadata only at DEBUG — transcript text is private speech (CLAUDE.md §3).
    logger.debug(
        "stt.transcribe(): %d bytes → %d segment(s), lang=%s (%.2f)",
        len(pcm),
        len(texts),
        language,
        lang_prob,
    )

    transcript = " ".join(texts).strip()
    return transcript
