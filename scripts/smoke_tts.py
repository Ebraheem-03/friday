"""Live TTS smoke test — atlas runs this; it is NOT collected by pytest.

Usage:
    python3 scripts/smoke_tts.py

What it does:
    1. Loads the real Kokoro-82M pipeline (downloads weights on first run).
    2. Synthesises the fixed phrase "FRIDAY online. All systems nominal."
    3. Prints:
         - time-to-first-audio-chunk (ms)
         - total synthesis duration (ms)
         - output sample rate
         - total PCM bytes + audio duration in seconds
    4. Writes the result to a temporary WAV file and prints the path.
    5. Does NOT auto-play audio (this machine may be headless).

Exit codes:
    0 — success
    1 — model load failure or synthesis error (message printed to stderr)

Security:
    The WAV is written to /tmp (not the repo) and is NOT committed.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import wave

# Ensure the friday package root is importable even when run from scripts/.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

PHRASE = "FRIDAY online. All systems nominal."
VOICE = "af_heart"
SAMPLE_RATE = 24_000  # Kokoro native; confirmed from istftnet.py


async def main() -> None:
    try:
        from voice.tts_kokoro import synth
    except ImportError as exc:
        print(f"ERROR: Could not import voice.tts_kokoro: {exc}", file=sys.stderr)
        print(
            "Ensure the [voice] extra is installed: "
            "pip install '.[voice]' -c constraints/cpu.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Synthesising: {PHRASE!r}")
    print(f"Voice: {VOICE!r}, Sample rate: {SAMPLE_RATE} Hz")
    print()

    t_start = time.perf_counter()
    t_first_chunk: float | None = None
    pcm_chunks: list[bytes] = []

    try:
        async for chunk in synth(PHRASE, voice=VOICE, sample_rate=SAMPLE_RATE):
            if t_first_chunk is None:
                t_first_chunk = time.perf_counter()
                elapsed_ms = (t_first_chunk - t_start) * 1000
                print(f"[TIMING] Time to first audio chunk: {elapsed_ms:.1f} ms")
            pcm_chunks.append(chunk)
    except Exception as exc:
        print(f"ERROR: Synthesis failed: {exc}", file=sys.stderr)
        sys.exit(1)

    t_end = time.perf_counter()

    if not pcm_chunks:
        print("ERROR: No audio was synthesised — KPipeline returned no results.", file=sys.stderr)
        sys.exit(1)

    total_pcm_bytes = sum(len(c) for c in pcm_chunks)
    total_samples = total_pcm_bytes // 2  # int16 = 2 bytes per sample
    audio_duration_s = total_samples / SAMPLE_RATE
    synthesis_duration_ms = (t_end - t_start) * 1000

    print(f"[TIMING] Total synthesis time:         {synthesis_duration_ms:.1f} ms")
    print(f"[AUDIO]  Output sample rate:           {SAMPLE_RATE} Hz")
    print(f"[AUDIO]  Total PCM bytes:              {total_pcm_bytes}")
    print(f"[AUDIO]  Audio duration:               {audio_duration_s:.2f} s")
    print(f"[AUDIO]  Chunks synthesised:           {len(pcm_chunks)}")
    print()

    # Write WAV to /tmp — do NOT write to repo.
    tmp_fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="friday_smoke_tts_")
    os.close(tmp_fd)

    try:
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(SAMPLE_RATE)
            for chunk in pcm_chunks:
                wf.writeframes(chunk)
        print(f"[OUTPUT] WAV written to: {wav_path}")
        print("         (not auto-played; use aplay, ffplay, or a media player)")
    except OSError as exc:
        print(f"WARNING: Could not write WAV file: {exc}", file=sys.stderr)

    print()
    print("Smoke test PASSED.")


if __name__ == "__main__":
    asyncio.run(main())
