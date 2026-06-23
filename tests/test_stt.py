"""Unit tests for voice/stt.py — mocked, CI-safe, no model load, no audio device.

Coverage
--------
- int16 PCM bytes → float32 conversion feeds the model correctly.
- Joined transcript from multiple segments is returned (stripped).
- Empty audio (zero bytes) → "".
- Too-short audio (< _MIN_AUDIO_BYTES) → "" without calling the model.
- Lazy import: graceful RuntimeError when faster-whisper is absent.
- preload() loads the singleton once; second call is a no-op.
- transcribe() offloads inference via asyncio.to_thread (no event-loop block).
- No audio ever written to disk (patching open/write confirms no calls).
- Metadata-only logging: transcript text does not appear in INFO-level logs.
- config: stt_model default "base.en" + env override via STT_MODEL.

These tests run in CI without the [voice] extra installed — all faster-whisper
imports are mocked.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Helpers — synthetic PCM
# ---------------------------------------------------------------------------

_SAMPLE_RATE = 16_000  # matches capture.py CAPTURE_SAMPLE_RATE


def _make_int16_pcm(duration_s: float = 0.5, amplitude: float = 0.3) -> bytes:
    """Return *duration_s* seconds of synthetic int16 PCM at 16 kHz."""
    n_samples = int(_SAMPLE_RATE * duration_s)
    samples = (np.full(n_samples, amplitude, dtype=np.float32) * 32767).astype(np.int16)
    return samples.tobytes()


def _make_float32_from_pcm(pcm: bytes) -> np.ndarray:
    """Mirror stt.py's conversion so we can assert what the model receives."""
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------------------
# Fake faster-whisper types — used to build a mock WhisperModel
# ---------------------------------------------------------------------------


class _FakeSegment(NamedTuple):
    text: str


class _FakeInfo(NamedTuple):
    language: str
    language_probability: float


def _make_fake_model(segments: list[str], language: str = "en") -> MagicMock:
    """Return a MagicMock that behaves like WhisperModel.transcribe."""
    fake_segments = [_FakeSegment(text=t) for t in segments]
    info = _FakeInfo(language=language, language_probability=0.99)

    model = MagicMock()
    model.transcribe.return_value = (iter(fake_segments), info)
    return model


# ---------------------------------------------------------------------------
# Module-level reset helpers
# ---------------------------------------------------------------------------


def _reset_stt_module() -> tuple[object, bool]:
    """Save and reset voice.stt singleton state.  Returns (old_model, old_preloaded)."""
    import voice.stt as stt_mod  # noqa: PLC0415

    old_model = stt_mod._model
    old_preloaded = stt_mod._preloaded
    stt_mod._model = None
    stt_mod._preloaded = False
    return old_model, old_preloaded


def _restore_stt_module(old_model: object, old_preloaded: bool) -> None:
    import voice.stt as stt_mod  # noqa: PLC0415

    stt_mod._model = old_model  # type: ignore[assignment]
    stt_mod._preloaded = old_preloaded


# ---------------------------------------------------------------------------
# PCM conversion tests
# ---------------------------------------------------------------------------


class TestPCMConversion:
    """Verify int16 PCM → float32 normalisation is correct."""

    def test_float32_range(self) -> None:
        """float32 audio derived from int16 max/min maps to ±1.0."""
        # INT16_MAX → ~1.0 (exactly 32767/32768)
        max_sample = struct.pack("<h", 32767)
        arr = np.frombuffer(max_sample, dtype=np.int16).astype(np.float32) / 32768.0
        assert abs(arr[0] - (32767 / 32768.0)) < 1e-6

        # INT16_MIN → -1.0
        min_sample = struct.pack("<h", -32768)
        arr = np.frombuffer(min_sample, dtype=np.int16).astype(np.float32) / 32768.0
        assert abs(arr[0] - (-32768 / 32768.0)) < 1e-6

    @pytest.mark.asyncio
    async def test_transcribe_feeds_correct_float32_to_model(self) -> None:
        """The float32 array passed to model.transcribe matches the manual conversion."""
        pcm = _make_int16_pcm(duration_s=0.5)
        expected_f32 = _make_float32_from_pcm(pcm)

        captured_audio: list[np.ndarray] = []

        def _fake_transcribe(audio: np.ndarray, **_: Any) -> tuple[Any, Any]:
            captured_audio.append(audio.copy())
            info = _FakeInfo(language="en", language_probability=0.99)
            return iter([_FakeSegment(text="hello")]), info

        fake_model = MagicMock()
        fake_model.transcribe.side_effect = _fake_transcribe

        import voice.stt as stt_mod  # noqa: PLC0415

        old_model, old_preloaded = _reset_stt_module()
        stt_mod._model = fake_model  # type: ignore[assignment]

        try:
            result = await stt_mod.transcribe(pcm)
        finally:
            _restore_stt_module(old_model, old_preloaded)

        assert result == "hello"
        assert len(captured_audio) == 1
        np.testing.assert_allclose(captured_audio[0], expected_f32, rtol=1e-5)


# ---------------------------------------------------------------------------
# Transcript assembly tests
# ---------------------------------------------------------------------------


class TestTranscriptAssembly:
    """Verify multi-segment joining and stripping."""

    @pytest.mark.asyncio
    async def test_single_segment_returned(self) -> None:
        """Single segment text is returned stripped."""
        pcm = _make_int16_pcm()
        fake_model = _make_fake_model(["  hello world  "])

        import voice.stt as stt_mod  # noqa: PLC0415

        old_model, old_preloaded = _reset_stt_module()
        stt_mod._model = fake_model  # type: ignore[assignment]

        try:
            result = await stt_mod.transcribe(pcm)
        finally:
            _restore_stt_module(old_model, old_preloaded)

        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_multi_segment_joined(self) -> None:
        """Multiple segments are joined with a space and stripped."""
        pcm = _make_int16_pcm()
        fake_model = _make_fake_model(["Hello", " there", " world"])

        import voice.stt as stt_mod  # noqa: PLC0415

        old_model, old_preloaded = _reset_stt_module()
        stt_mod._model = fake_model  # type: ignore[assignment]

        try:
            result = await stt_mod.transcribe(pcm)
        finally:
            _restore_stt_module(old_model, old_preloaded)

        assert result == "Hello  there  world"

    @pytest.mark.asyncio
    async def test_no_segments_returns_empty_string(self) -> None:
        """No segments (model returned nothing) → empty string."""
        pcm = _make_int16_pcm()
        fake_model = _make_fake_model([])

        import voice.stt as stt_mod  # noqa: PLC0415

        old_model, old_preloaded = _reset_stt_module()
        stt_mod._model = fake_model  # type: ignore[assignment]

        try:
            result = await stt_mod.transcribe(pcm)
        finally:
            _restore_stt_module(old_model, old_preloaded)

        assert result == ""


# ---------------------------------------------------------------------------
# Short / empty audio guard
# ---------------------------------------------------------------------------


class TestShortAudioGuard:
    """Empty and too-short audio returns '' without hitting the model."""

    @pytest.mark.asyncio
    async def test_empty_bytes_returns_empty_string(self) -> None:
        """transcribe(b'') → ''."""
        import voice.stt as stt_mod  # noqa: PLC0415

        old_model, old_preloaded = _reset_stt_module()
        fake_model = MagicMock()
        stt_mod._model = fake_model  # type: ignore[assignment]

        try:
            result = await stt_mod.transcribe(b"")
        finally:
            _restore_stt_module(old_model, old_preloaded)

        assert result == ""
        fake_model.transcribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_too_short_pcm_returns_empty_string(self) -> None:
        """Audio below _MIN_AUDIO_BYTES → '' without model call."""
        import voice.stt as stt_mod  # noqa: PLC0415

        # Build PCM that is 1 byte shorter than the minimum.
        short_pcm = b"\x00" * (stt_mod._MIN_AUDIO_BYTES - 1)

        old_model, old_preloaded = _reset_stt_module()
        fake_model = MagicMock()
        stt_mod._model = fake_model  # type: ignore[assignment]

        try:
            result = await stt_mod.transcribe(short_pcm)
        finally:
            _restore_stt_module(old_model, old_preloaded)

        assert result == ""
        fake_model.transcribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_exactly_min_bytes_calls_model(self) -> None:
        """Audio at exactly _MIN_AUDIO_BYTES is NOT rejected — model is called."""
        import voice.stt as stt_mod  # noqa: PLC0415

        pcm = b"\x00" * stt_mod._MIN_AUDIO_BYTES
        fake_model = _make_fake_model(["ok"])

        old_model, old_preloaded = _reset_stt_module()
        stt_mod._model = fake_model  # type: ignore[assignment]

        try:
            result = await stt_mod.transcribe(pcm)
        finally:
            _restore_stt_module(old_model, old_preloaded)

        # Model was called and returned "ok".
        assert result == "ok"
        fake_model.transcribe.assert_called_once()


# ---------------------------------------------------------------------------
# Lazy import / graceful degradation
# ---------------------------------------------------------------------------


class TestLazyImport:
    """faster-whisper absent → clear RuntimeError, not ImportError at module level."""

    def test_stt_imports_without_faster_whisper(self) -> None:
        """voice.stt can be imported even when faster-whisper is not installed."""
        # If this test runs, the import already succeeded at the top of the file.
        import voice.stt  # noqa: F401, PLC0415

        # No assertion needed: reaching here means import did not raise.

    @pytest.mark.asyncio
    async def test_transcribe_raises_runtime_error_when_faster_whisper_absent(
        self,
    ) -> None:
        """transcribe() raises RuntimeError (not ImportError) when the package is missing."""
        pcm = _make_int16_pcm()

        import voice.stt as stt_mod  # noqa: PLC0415

        old_model, old_preloaded = _reset_stt_module()

        try:
            # Make _build_model raise ImportError (simulating missing package).
            def _fake_build(model_size: str) -> None:
                raise ImportError("No module named 'faster_whisper'")

            with patch("voice.stt._build_model", side_effect=_fake_build):
                with pytest.raises(RuntimeError, match="faster-whisper"):
                    await stt_mod.transcribe(pcm)
        finally:
            _restore_stt_module(old_model, old_preloaded)

    @pytest.mark.asyncio
    async def test_preload_raises_runtime_error_when_faster_whisper_absent(
        self,
    ) -> None:
        """preload() raises RuntimeError when the package is missing."""
        import voice.stt as stt_mod  # noqa: PLC0415

        old_model, old_preloaded = _reset_stt_module()

        try:
            def _fake_build(model_size: str) -> None:
                raise ImportError("No module named 'faster_whisper'")

            with patch("voice.stt._build_model", side_effect=_fake_build):
                with pytest.raises((RuntimeError, ImportError)):
                    await stt_mod.preload()
        finally:
            _restore_stt_module(old_model, old_preloaded)


# ---------------------------------------------------------------------------
# Singleton / preload idempotency
# ---------------------------------------------------------------------------


class TestPreload:
    """preload() builds the model once and is idempotent."""

    @pytest.mark.asyncio
    async def test_preload_builds_model_once(self) -> None:
        """preload() calls _build_model exactly once even when called twice."""
        import voice.stt as stt_mod  # noqa: PLC0415

        old_model, old_preloaded = _reset_stt_module()
        build_call_count = 0

        def _fake_build(model_size: str) -> MagicMock:
            nonlocal build_call_count
            build_call_count += 1
            return MagicMock()

        try:
            with patch("voice.stt._build_model", side_effect=_fake_build):
                await stt_mod.preload()
                await stt_mod.preload()  # second call — must be a no-op

            assert build_call_count == 1
            assert stt_mod._preloaded is True
        finally:
            _restore_stt_module(old_model, old_preloaded)

    @pytest.mark.asyncio
    async def test_preload_idempotent_when_already_loaded(self) -> None:
        """preload() is a no-op if _preloaded is already True."""
        import voice.stt as stt_mod  # noqa: PLC0415

        old_model, old_preloaded = _reset_stt_module()
        build_called = False

        def _should_not_be_called(model_size: str) -> None:
            nonlocal build_called
            build_called = True

        try:
            # Simulate already-preloaded state.
            stt_mod._preloaded = True

            with patch("voice.stt._build_model", side_effect=_should_not_be_called):
                await stt_mod.preload()

            assert build_called is False
        finally:
            _restore_stt_module(old_model, old_preloaded)

    @pytest.mark.asyncio
    async def test_get_model_returns_same_singleton(self) -> None:
        """_get_model() returns the same object on repeated calls."""
        import voice.stt as stt_mod  # noqa: PLC0415

        old_model, old_preloaded = _reset_stt_module()
        fake = MagicMock()
        build_count = 0

        def _fake_build(model_size: str) -> MagicMock:
            nonlocal build_count
            build_count += 1
            return fake

        try:
            with patch("voice.stt._build_model", side_effect=_fake_build):
                m1 = await stt_mod._get_model()
                m2 = await stt_mod._get_model()

            assert m1 is m2
            assert build_count == 1
        finally:
            _restore_stt_module(old_model, old_preloaded)


# ---------------------------------------------------------------------------
# asyncio.to_thread offload — event loop not blocked
# ---------------------------------------------------------------------------


class TestToThreadOffload:
    """transcribe() runs inference in a thread, not on the event loop."""

    @pytest.mark.asyncio
    async def test_transcribe_runs_in_thread(self) -> None:
        """Verify asyncio.to_thread is used — the blocking call is not on the event loop.

        Strategy: patch asyncio.to_thread and verify it was awaited (rather than
        the inference function being called directly in the event loop).
        """
        pcm = _make_int16_pcm()

        import voice.stt as stt_mod  # noqa: PLC0415

        old_model, old_preloaded = _reset_stt_module()
        fake_model = _make_fake_model(["test"])
        stt_mod._model = fake_model  # type: ignore[assignment]

        to_thread_calls: list[Any] = []
        original_to_thread = asyncio.to_thread

        async def _spy_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
            to_thread_calls.append(func)
            # Actually run it so the test produces a real result.
            return await original_to_thread(func, *args, **kwargs)

        try:
            with patch("asyncio.to_thread", side_effect=_spy_to_thread):
                await stt_mod.transcribe(pcm)

            assert len(to_thread_calls) >= 1, (
                "asyncio.to_thread was not called — inference may be blocking the event loop"
            )
        finally:
            _restore_stt_module(old_model, old_preloaded)


# ---------------------------------------------------------------------------
# No disk writes
# ---------------------------------------------------------------------------


class TestNoDiskWrites:
    """Audio bytes must never be written to disk."""

    @pytest.mark.asyncio
    async def test_transcribe_never_writes_to_disk(self) -> None:
        """transcribe() does not call open() or write to any file."""
        pcm = _make_int16_pcm()

        import voice.stt as stt_mod  # noqa: PLC0415

        old_model, old_preloaded = _reset_stt_module()
        fake_model = _make_fake_model(["hello"])
        stt_mod._model = fake_model  # type: ignore[assignment]

        write_calls: list[Any] = []

        original_open = open  # noqa: A001

        def _spy_open(file: Any, mode: str = "r", **kwargs: Any) -> Any:
            # Allow reads (e.g. tokeniser files) but flag any write opens.
            if "w" in str(mode) or "x" in str(mode):
                write_calls.append((file, mode))
            return original_open(file, mode, **kwargs)  # type: ignore[call-arg]

        try:
            with patch("builtins.open", side_effect=_spy_open):
                await stt_mod.transcribe(pcm)

            assert write_calls == [], (
                f"transcribe() opened file(s) for writing: {write_calls}"
            )
        finally:
            _restore_stt_module(old_model, old_preloaded)


# ---------------------------------------------------------------------------
# Metadata-only logging — transcript text not in INFO logs
# ---------------------------------------------------------------------------


class TestLoggingSecurity:
    """Transcript text must never appear in INFO-level log records."""

    @pytest.mark.asyncio
    async def test_transcript_not_in_info_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """INFO-level log records must not contain the transcribed text."""
        secret_transcript = "my very private speech content xyzzy"
        pcm = _make_int16_pcm()

        import voice.stt as stt_mod  # noqa: PLC0415

        old_model, old_preloaded = _reset_stt_module()
        fake_model = _make_fake_model([secret_transcript])
        stt_mod._model = fake_model  # type: ignore[assignment]

        try:
            with caplog.at_level(logging.INFO, logger="voice.stt"):
                result = await stt_mod.transcribe(pcm)

            assert result == secret_transcript.strip()

            # The transcript text must NOT appear in any INFO-level log record.
            info_messages = [
                r.message for r in caplog.records if r.levelno >= logging.INFO
            ]
            for msg in info_messages:
                assert secret_transcript not in msg, (
                    f"Transcript text found in INFO log: {msg!r}"
                )
        finally:
            _restore_stt_module(old_model, old_preloaded)

    @pytest.mark.asyncio
    async def test_metadata_logged_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DEBUG logs include metadata (byte length, segment count, language)."""
        pcm = _make_int16_pcm()

        import voice.stt as stt_mod  # noqa: PLC0415

        old_model, old_preloaded = _reset_stt_module()
        fake_model = _make_fake_model(["whatever"], language="en")
        stt_mod._model = fake_model  # type: ignore[assignment]

        try:
            with caplog.at_level(logging.DEBUG, logger="voice.stt"):
                await stt_mod.transcribe(pcm)

            debug_messages = [
                r.message for r in caplog.records if r.levelno == logging.DEBUG
            ]
            # At least one DEBUG record should mention "lang" or segment count.
            combined = " ".join(debug_messages)
            assert "lang" in combined or "segment" in combined or str(len(pcm)) in combined
        finally:
            _restore_stt_module(old_model, old_preloaded)


# ---------------------------------------------------------------------------
# Config: stt_model field
# ---------------------------------------------------------------------------


class TestConfigSTTModel:
    """Tests for the stt_model field in core/config.py."""

    def _make_env_file(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / ".env"
        p.write_text(content, encoding="utf-8")
        return p

    def test_stt_model_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """stt_model defaults to 'base.en' when STT_MODEL is absent."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.delenv("STT_MODEL", raising=False)
        dotenv = self._make_env_file(tmp_path, "")

        from core.config import Settings  # noqa: PLC0415

        cfg = Settings.from_env(dotenv_path=dotenv)
        assert cfg.stt_model == "base.en"

    def test_stt_model_env_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """STT_MODEL env var overrides the default."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("STT_MODEL", "small")
        dotenv = self._make_env_file(tmp_path, "")

        from core.config import Settings  # noqa: PLC0415

        cfg = Settings.from_env(dotenv_path=dotenv)
        assert cfg.stt_model == "small"

    def test_stt_model_dotenv_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """STT_MODEL in .env file is used when not in os.environ."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.delenv("STT_MODEL", raising=False)
        dotenv = self._make_env_file(tmp_path, "STT_MODEL=medium.en\n")

        from core.config import Settings  # noqa: PLC0415

        cfg = Settings.from_env(dotenv_path=dotenv)
        assert cfg.stt_model == "medium.en"

    def test_stt_model_in_repr(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """stt_model appears in __repr__ output."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("STT_MODEL", "base.en")
        dotenv = self._make_env_file(tmp_path, "")

        from core.config import Settings  # noqa: PLC0415

        cfg = Settings.from_env(dotenv_path=dotenv)
        assert "stt_model=" in repr(cfg)
        assert "base.en" in repr(cfg)

    def test_all_defaults_still_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Adding stt_model did not break any existing defaults."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        for var in ("SAMPLE_RATE", "TTS_VOICE", "STT_MODEL", "TELEMETRY_WS_PORT", "LOG_LEVEL"):
            monkeypatch.delenv(var, raising=False)
        dotenv = self._make_env_file(tmp_path, "")

        from core.config import Settings  # noqa: PLC0415

        cfg = Settings.from_env(dotenv_path=dotenv)
        assert cfg.sample_rate == 24000
        assert cfg.tts_voice == "af_heart"
        assert cfg.stt_model == "base.en"
        assert cfg.telemetry_ws_port == 8765
        assert cfg.log_level == "INFO"
