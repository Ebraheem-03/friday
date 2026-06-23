"""Guarded live end-to-end smoke for Step 9 (NOT collected by CI).

Exercises the real, load-bearing pieces of the assembled app WITHOUT requiring
audio hardware (this host has no PortAudio, so the interactive mic→speaker turn
is verified separately by a human on a machine with audio):

  1. build_app() — full graph construction + wiring (real tools, exec posture).
  2. TelemetryServer — real 127.0.0.1 socket; a WS client receives a valid frame.
  3. Brain — a real Gemini streaming call (needs GEMINI_API_KEY).
  4. TTS→STT round-trip — Kokoro synthesises a sentence to 16 kHz PCM, then
     faster-whisper transcribes it back (proves STT works on real audio).

Run:  python scripts/smoke_step9.py
Exit code 0 = all passed; nonzero = at least one failure.
"""

from __future__ import annotations

import asyncio
import json
import logging

logging.basicConfig(level=logging.WARNING)

EXPECTED = "the quick brown fox jumps over the lazy dog"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def smoke_build_app() -> None:
    from core.app import build_app
    from core.brain import _NoopMemory, _NoopOS, _NoopWeb
    from core.config import Settings

    app = build_app(Settings.from_env())
    assert app.brain._memory is app.memory, "brain memory not wired"
    assert app.brain._os_tool is app.os_bridge, "brain os_tool not wired"
    assert app.brain._web_tool is app.web_agent, "brain web_tool not wired"
    assert not isinstance(app.brain._os_tool, _NoopOS), "os_tool is a no-op stub"
    assert not isinstance(app.brain._web_tool, _NoopWeb), "web_tool is a no-op stub"
    assert not isinstance(app.brain._memory, _NoopMemory), "memory is a no-op stub"
    # locked exec posture
    assert app.os_bridge._dry_run is False, "os_bridge should be live (dry_run=False)"
    assert app.os_bridge._trusted is True, "os_bridge should be trusted"
    assert app.os_bridge._confirm_callback is not None, "run_command confirm missing"
    record("build_app wiring + exec posture", True,
           "real tools wired; dry_run=False, trusted=True, confirm set")


async def smoke_telemetry() -> None:
    import websockets

    from core.config import Settings
    from core.state import State
    from core.telemetry import TelemetryServer

    settings = Settings.from_env()
    srv = TelemetryServer(settings, interval=0.05)
    await srv.start()
    try:
        srv.set_state(State.THINKING)
        srv.set_audio_level(0.5)
        uri = f"ws://127.0.0.1:{settings.telemetry_ws_port}"
        async with websockets.connect(uri) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
        frame = json.loads(raw)
        for k in ("ts", "state", "cpu_pct", "ram_pct", "audio_level"):
            assert k in frame, f"frame missing {k}"
        assert frame["state"] == "thinking", f"state={frame['state']!r}"
        assert 0.0 <= frame["cpu_pct"] <= 100.0, "cpu out of range"
        assert 0.0 <= frame["audio_level"] <= 1.0, "audio out of range"
        record("telemetry WS (127.0.0.1) live frame", True,
               f"state={frame['state']} cpu={frame['cpu_pct']:.0f}% audio={frame['audio_level']}")
    finally:
        await srv.stop()


async def smoke_brain() -> None:
    from core.brain import Brain
    from core.config import Settings

    brain = Brain(Settings.from_env())
    out = []
    async for delta in brain.stream(
        "Reply with exactly one word: PONG", history=[]
    ):
        out.append(delta)
    text = "".join(out).strip()
    assert text, "empty brain response"
    record("brain live Gemini stream", True, f"reply={text[:40]!r}")


async def smoke_tts_stt_roundtrip() -> None:
    from voice import stt, tts_kokoro

    await tts_kokoro.preload()
    await stt.preload()

    pcm = bytearray()
    async for chunk in tts_kokoro.synth(EXPECTED, sample_rate=16_000):
        pcm.extend(chunk)
    assert len(pcm) > 0, "TTS produced no audio"

    transcript = (await stt.transcribe(bytes(pcm), sample_rate=16_000)).lower()
    exp_words = set(EXPECTED.split())
    got_words = set(transcript.replace(".", "").replace(",", "").split())
    overlap = exp_words & got_words
    frac = len(overlap) / len(exp_words)
    ok = frac >= 0.6  # allow minor ASR slips on a CPU base.en model
    record("TTS→STT round-trip", ok,
           f"{len(pcm)} PCM bytes; transcript={transcript!r}; word-overlap={frac:.0%}")
    if not ok:
        raise AssertionError(f"transcript overlap too low: {transcript!r}")


async def main() -> int:
    smokes = [
        ("build_app", smoke_build_app),
        ("telemetry", smoke_telemetry),
        ("brain", smoke_brain),
        ("tts_stt", smoke_tts_stt_roundtrip),
    ]
    for name, fn in smokes:
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001
            record(name, False, f"{type(exc).__name__}: {exc}")

    print("\n=== SUMMARY ===")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  {'✓' if ok else '✗'} {name}")
    print(f"{passed}/{len(results)} live smokes passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
