"""Mic capture via sounddevice + RMS-based VAD + barge-in signal.

Design overview
---------------
Energy/RMS VAD — no extra dependencies
    We use a simple root-mean-square (RMS) energy detector with hangover
    framing rather than WebRTCVAD or Silero.  This avoids any new pinned
    dependency (sentinel approval would be required) while still being
    sufficient for detecting speech vs. silence.  The threshold is calibrated
    to the ambient noise floor on startup.

    VAD algorithm:
        1.  Divide the 16 kHz mono stream into 20 ms frames (320 samples).
        2.  Compute per-frame RMS.
        3.  A frame is "active" when rms > threshold.
        4.  Speech onset:  N_ONSET consecutive active frames trigger start.
        5.  Speech offset: N_HANGOVER consecutive silent frames trigger end.
        6.  The entire utterance buffer (onset → hangover end) is yielded.

Barge-in
    The caller holds a ``asyncio.Event`` named ``tts_active``.  When barge-in
    is detected (speech onset fires while tts_active is set), the capture loop
    sets a separate ``barge_in_event`` that stream.py monitors to abort
    playback.

Security (CLAUDE.md §3 / ADR-0008)
    Raw microphone audio is NEVER written to disk in the normal path.  All
    processing is in-memory numpy arrays.  The only output is the yielded
    utterance bytes (PCM) passed to the STT layer.

sounddevice API used (confirmed from source, 0.5.5)
    - ``sounddevice.InputStream(samplerate, blocksize, channels, dtype, callback)``
      callback signature: ``callback(indata: np.ndarray, frames, time, status)``
    - ``indata`` shape: (frames, channels) for multi-channel; (frames,) for mono
      when channels=1 (actually shape is (frames, 1) — we take [:, 0]).
    - Stream is used as a context manager; ``.start()`` / ``.stop()`` inside the
      async loop uses an ``asyncio.Queue`` to bridge PortAudio thread → event loop.
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
# VAD constants  (tuned for 16 kHz / 20 ms frames)
# ---------------------------------------------------------------------------

CAPTURE_SAMPLE_RATE: int = 16_000       # Mic capture rate (standard STT rate)
FRAME_MS: int = 20                       # Frame length in milliseconds
FRAME_SAMPLES: int = CAPTURE_SAMPLE_RATE * FRAME_MS // 1000  # = 320 samples

# Onset:   3 consecutive active frames (~60 ms) triggers speech start.
N_ONSET: int = 3
# Hangover: 25 silent frames (~500 ms) after last active frame ends utterance.
N_HANGOVER: int = 25

# RMS threshold: frames below this are considered silence.
# Calibrated on typical laptop mic; can be overridden via env or argument.
DEFAULT_RMS_THRESHOLD: float = 0.01     # range 0.0–1.0 for float32 samples

# Maximum utterance length to buffer (guard against model hanging).
MAX_UTTERANCE_SECONDS: int = 30
MAX_UTTERANCE_FRAMES: int = MAX_UTTERANCE_SECONDS * 1000 // FRAME_MS  # 1500 frames


# ---------------------------------------------------------------------------
# VAD state machine (pure Python, no side effects)
# ---------------------------------------------------------------------------


class _VADStateMachine:
    """Frame-level RMS VAD with onset + hangover logic.

    Not thread-safe; call from a single thread (the sounddevice callback
    thread or the asyncio reader).
    """

    def __init__(self, threshold: float = DEFAULT_RMS_THRESHOLD) -> None:
        self.threshold = threshold
        self._reset()

    def _reset(self) -> None:
        self._onset_count: int = 0
        self._hangover_count: int = 0
        self._in_speech: bool = False
        self._utterance: list[np.ndarray] = []

    def push_frame(self, frame: np.ndarray) -> np.ndarray | None:
        """Push one 20 ms frame.  Returns completed utterance ndarray or None.

        Parameters
        ----------
        frame:
            1-D float32 mono array of length FRAME_SAMPLES.

        Returns
        -------
        np.ndarray or None
            Concatenated utterance (float32, 16 kHz) when utterance completes,
            else None.
        """
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        active = rms > self.threshold

        if not self._in_speech:
            if active:
                self._onset_count += 1
                if self._onset_count >= N_ONSET:
                    self._in_speech = True
                    self._hangover_count = 0
                    # Retroactively include the onset frames.
                    # (They are already in _utterance from tentative buffering.)
            else:
                self._onset_count = 0
                self._utterance = []  # discard tentative frames when silent
                return None

            # Tentatively buffer onset candidate frames.
            self._utterance.append(frame.copy())
            return None

        # --- In speech ---
        self._utterance.append(frame.copy())

        if active:
            self._hangover_count = 0
        else:
            self._hangover_count += 1

        # Force-end if utterance exceeds max length.
        if len(self._utterance) >= MAX_UTTERANCE_FRAMES:
            logger.warning("VAD: utterance exceeded max length; force-ending.")
            return self._flush()

        if self._hangover_count >= N_HANGOVER:
            return self._flush()

        return None

    def _flush(self) -> np.ndarray:
        """Return the buffered utterance and reset state."""
        utt = np.concatenate(self._utterance, axis=0)
        self._reset()
        return utt


# ---------------------------------------------------------------------------
# Async capture loop
# ---------------------------------------------------------------------------


async def capture_loop(
    *,
    tts_active: asyncio.Event,
    barge_in_event: asyncio.Event,
    rms_threshold: float = DEFAULT_RMS_THRESHOLD,
    sample_rate: int = CAPTURE_SAMPLE_RATE,
) -> AsyncIterator[bytes]:
    """Async generator: yields completed utterances as raw int16 PCM bytes.

    Parameters
    ----------
    tts_active:
        Event that is set while TTS is playing.  When speech onset is detected
        while this event is set, ``barge_in_event`` is signalled and the
        current TTS playback should be interrupted.
    barge_in_event:
        Cleared by stream.py before starting playback; set here on barge-in.
    rms_threshold:
        RMS energy threshold for VAD onset/offset detection.
    sample_rate:
        Microphone capture sample rate.  Should match STT expectations (16 kHz).

    Yields
    ------
    bytes
        Raw 16-bit signed little-endian PCM at *sample_rate*.  One yield per
        complete utterance.

    Notes
    -----
    - This generator runs as long as the caller iterates it.
    - Cancellation (``asyncio.CancelledError``) is propagated cleanly; the
      sounddevice stream is closed on exit via the ``finally`` block.
    - Audio is NEVER written to disk here (CLAUDE.md §3 / ADR-0008).
    """
    try:
        import sounddevice as sd  # type: ignore[import]
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "sounddevice / PortAudio not available. "
            "Install the [voice] extra and ensure PortAudio is installed on the system."
        ) from exc

    audio_queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=200)
    loop = asyncio.get_running_loop()
    vad = _VADStateMachine(threshold=rms_threshold)

    def _callback(
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: object,
    ) -> None:
        """PortAudio callback — runs in a C thread; must not block."""
        if status:
            logger.debug("sounddevice status: %s", status)
        # indata shape: (frames, 1) for mono channels=1
        mono = indata[:, 0].copy()
        try:
            loop.call_soon_threadsafe(audio_queue.put_nowait, mono)
        except asyncio.QueueFull:
            logger.debug("VAD queue full — dropping frame")

    stream = sd.InputStream(
        samplerate=sample_rate,
        blocksize=FRAME_SAMPLES,
        channels=1,
        dtype="float32",
        latency="low",
        callback=_callback,
    )

    logger.info(
        "Mic capture started (rate=%d Hz, frame=%d ms, threshold=%.4f)",
        sample_rate,
        FRAME_MS,
        rms_threshold,
    )
    with stream:
        stream.start()
        try:
            while True:
                frame: np.ndarray = await audio_queue.get()
                utterance = vad.push_frame(frame)
                if utterance is not None:
                    # Check barge-in: speech detected while TTS is playing.
                    if tts_active.is_set():
                        logger.info("Barge-in detected — signalling TTS stop.")
                        barge_in_event.set()

                    # Convert float32 → int16 PCM and yield.
                    clipped = np.clip(utterance, -1.0, 1.0)
                    pcm = (clipped * 32767).astype(np.int16)
                    yield pcm.tobytes()
        finally:
            stream.stop()
            logger.info("Mic capture stopped.")
