"""Unit tests for voice.tts_gemini (mocked — no live API key / network)."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from voice import tts_gemini


def _fake_response(data: bytes) -> SimpleNamespace:
    """Build a generate_content-shaped response carrying *data* as audio PCM."""
    inline = SimpleNamespace(data=data, mime_type="audio/L16;codec=pcm;rate=24000")
    part = SimpleNamespace(inline_data=inline)
    content = SimpleNamespace(parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, data: bytes) -> dict:
    """Patch tts_gemini's client singleton to return _fake_response(data).

    Returns a dict that records the kwargs passed to generate_content.
    """
    captured: dict = {}

    def _generate_content(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return _fake_response(data)

    fake = SimpleNamespace(models=SimpleNamespace(generate_content=_generate_content))
    monkeypatch.setattr(tts_gemini, "_client", fake)
    return captured


@pytest.mark.asyncio
async def test_synth_yields_pcm_at_native_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    pcm = np.arange(100, dtype=np.int16).tobytes()
    _install_fake_client(monkeypatch, pcm)

    out = b"".join([chunk async for chunk in tts_gemini.synth("hello", sample_rate=24_000)])
    assert out == pcm  # no resample at native 24 kHz


@pytest.mark.asyncio
async def test_synth_resamples_when_rate_differs(monkeypatch: pytest.MonkeyPatch) -> None:
    pcm = np.zeros(1000, dtype=np.int16).tobytes()
    _install_fake_client(monkeypatch, pcm)

    out = b"".join([chunk async for chunk in tts_gemini.synth("hi", sample_rate=12_000)])
    # 24 kHz -> 12 kHz halves the sample count (±rounding).
    assert abs(len(np.frombuffer(out, dtype=np.int16)) - 500) <= 1


@pytest.mark.asyncio
async def test_synth_forwards_voice_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_fake_client(monkeypatch, np.zeros(10, dtype=np.int16).tobytes())

    async for _ in tts_gemini.synth("hi", voice="Puck", model="custom-tts-model"):
        pass
    assert captured["model"] == "custom-tts-model"
    # voice_name is nested in the speech config object passed to the SDK.
    cfg = captured["config"]
    voice_name = cfg.speech_config.voice_config.prebuilt_voice_config.voice_name
    assert voice_name == "Puck"


@pytest.mark.asyncio
async def test_synth_to_numpy(monkeypatch: pytest.MonkeyPatch) -> None:
    pcm = (np.ones(50, dtype=np.int16) * 16384).tobytes()
    _install_fake_client(monkeypatch, pcm)

    arrs = await tts_gemini.synth_to_numpy("hi")
    assert len(arrs) == 1
    assert arrs[0].dtype == np.float32
    assert np.allclose(arrs[0], 0.5, atol=1e-3)


def test_extract_pcm_malformed_raises() -> None:
    with pytest.raises(RuntimeError):
        tts_gemini._extract_pcm(SimpleNamespace(candidates=[]))


def test_extract_pcm_empty_audio_raises() -> None:
    with pytest.raises(RuntimeError):
        tts_gemini._extract_pcm(_fake_response(b""))


def test_resolve_synth_binds_model() -> None:
    """stream._resolve_synth must bind the model for the gemini engine (M-1)."""
    import functools

    from voice.stream import _resolve_synth

    fn = _resolve_synth("gemini", "my-model")
    assert isinstance(fn, functools.partial)
    assert fn.keywords["model"] == "my-model"

    # kokoro engine returns the kokoro synth unchanged (no model binding).
    assert not isinstance(_resolve_synth("kokoro", "my-model"), functools.partial)
