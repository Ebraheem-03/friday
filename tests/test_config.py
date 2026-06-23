"""Unit tests for core/config.py.

Coverage
--------
- Required var missing -> ConfigError (clear message, no secret value in message)
- All defaults applied when optional vars absent
- .env file parsed correctly
- os.environ takes precedence over .env file
- Invalid int -> ConfigError
- Port out of range -> ConfigError
- Positive sample-rate validation
- Settings is frozen (immutable)
- __repr__ never leaks the key value
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import ConfigError, Settings, load_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_env_file(tmp_path: Path, content: str) -> Path:
    """Write *content* to a temporary .env file and return its path."""
    p = tmp_path / ".env"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Required var
# ---------------------------------------------------------------------------

class TestRequiredVar:
    def test_missing_key_raises_config_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """ConfigError when GEMINI_API_KEY absent from env and .env."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        dotenv = _make_env_file(tmp_path, "# empty\n")
        with pytest.raises(ConfigError) as exc_info:
            Settings.from_env(dotenv_path=dotenv)
        assert "GEMINI_API_KEY" in str(exc_info.value)

    def test_empty_key_raises_config_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """ConfigError when GEMINI_API_KEY is set but empty."""
        monkeypatch.setenv("GEMINI_API_KEY", "")
        dotenv = _make_env_file(tmp_path, "")
        with pytest.raises(ConfigError) as exc_info:
            Settings.from_env(dotenv_path=dotenv)
        assert "GEMINI_API_KEY" in str(exc_info.value)

    def test_whitespace_only_key_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "   ")
        dotenv = _make_env_file(tmp_path, "")
        with pytest.raises(ConfigError):
            Settings.from_env(dotenv_path=dotenv)

    def test_error_message_does_not_contain_key_value(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The exception message must name the var but not print its value.

        Triggers ConfigError via an invalid int (TELEMETRY_WS_PORT), so the
        GEMINI_API_KEY is successfully loaded from .env but the port parse
        fails — we verify the error text does not contain the secret key value.
        """
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("TELEMETRY_WS_PORT", "not-a-number")
        # .env supplies the real key; the invalid port triggers ConfigError
        dotenv = _make_env_file(tmp_path, "GEMINI_API_KEY=real-key\n")
        with pytest.raises(ConfigError) as exc_info:
            Settings.from_env(dotenv_path=dotenv)
        # The error message should not contain the actual key value
        assert "real-key" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_all_defaults_applied(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Optional vars use their defaults when absent."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        for var in ("SAMPLE_RATE", "TTS_VOICE", "TELEMETRY_WS_PORT", "LOG_LEVEL"):
            monkeypatch.delenv(var, raising=False)
        dotenv = _make_env_file(tmp_path, "")  # empty .env

        cfg = Settings.from_env(dotenv_path=dotenv)

        assert cfg.sample_rate == 24000
        assert cfg.tts_voice == "af_heart"
        assert cfg.telemetry_ws_port == 8765
        assert cfg.log_level == "INFO"

    def test_explicit_values_override_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("SAMPLE_RATE", "16000")
        monkeypatch.setenv("TTS_VOICE", "en_us")
        monkeypatch.setenv("TELEMETRY_WS_PORT", "9000")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        dotenv = _make_env_file(tmp_path, "")

        cfg = Settings.from_env(dotenv_path=dotenv)

        assert cfg.sample_rate == 16000
        assert cfg.tts_voice == "en_us"
        assert cfg.telemetry_ws_port == 9000
        assert cfg.log_level == "DEBUG"


# ---------------------------------------------------------------------------
# .env parsing
# ---------------------------------------------------------------------------

class TestDotenvParsing:
    def test_dotenv_values_loaded(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Settings are sourced from .env when os.environ lacks the vars."""
        for var in ("GEMINI_API_KEY", "SAMPLE_RATE", "TTS_VOICE", "TELEMETRY_WS_PORT", "LOG_LEVEL"):
            monkeypatch.delenv(var, raising=False)

        dotenv = _make_env_file(
            tmp_path,
            "GEMINI_API_KEY=dotenv-key\nSAMPLE_RATE=48000\nTTS_VOICE=en_gb\n"
            "TELEMETRY_WS_PORT=9876\nLOG_LEVEL=WARNING\n",
        )

        cfg = Settings.from_env(dotenv_path=dotenv)

        assert cfg.gemini_api_key == "dotenv-key"
        assert cfg.sample_rate == 48000
        assert cfg.tts_voice == "en_gb"
        assert cfg.telemetry_ws_port == 9876
        assert cfg.log_level == "WARNING"

    def test_blank_lines_and_comments_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        for var in ("GEMINI_API_KEY", "SAMPLE_RATE"):
            monkeypatch.delenv(var, raising=False)

        dotenv = _make_env_file(
            tmp_path,
            "# This is a comment\n\n   \nGEMINI_API_KEY=from-dotenv\n# another comment\n",
        )
        cfg = Settings.from_env(dotenv_path=dotenv)
        assert cfg.gemini_api_key == "from-dotenv"

    def test_quoted_values_stripped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        for var in ("GEMINI_API_KEY", "TTS_VOICE"):
            monkeypatch.delenv(var, raising=False)

        dotenv = _make_env_file(tmp_path, 'GEMINI_API_KEY="quoted-key"\nTTS_VOICE=\'single-quoted\'\n')
        cfg = Settings.from_env(dotenv_path=dotenv)
        assert cfg.gemini_api_key == "quoted-key"
        assert cfg.tts_voice == "single-quoted"

    def test_missing_dotenv_file_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Absent .env file is fine; env vars alone are sufficient."""
        monkeypatch.setenv("GEMINI_API_KEY", "env-only-key")
        non_existent = tmp_path / "no_such_file.env"
        cfg = Settings.from_env(dotenv_path=non_existent)
        assert cfg.gemini_api_key == "env-only-key"


# ---------------------------------------------------------------------------
# os.environ precedence over .env
# ---------------------------------------------------------------------------

class TestEnvPrecedence:
    def test_os_environ_wins_over_dotenv(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """os.environ values must override identical keys in .env."""
        monkeypatch.setenv("GEMINI_API_KEY", "from-env")
        monkeypatch.setenv("TTS_VOICE", "env-voice")

        dotenv = _make_env_file(tmp_path, "GEMINI_API_KEY=from-dotenv\nTTS_VOICE=dotenv-voice\n")

        cfg = Settings.from_env(dotenv_path=dotenv)

        assert cfg.gemini_api_key == "from-env"
        assert cfg.tts_voice == "env-voice"

    def test_dotenv_fills_gap_not_in_environ(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """If key is only in .env, that value is used."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("TTS_VOICE", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "env-key")  # only this one in env

        dotenv = _make_env_file(tmp_path, "TTS_VOICE=dotenv-voice\n")
        cfg = Settings.from_env(dotenv_path=dotenv)
        assert cfg.tts_voice == "dotenv-voice"


# ---------------------------------------------------------------------------
# Type coercion / validation
# ---------------------------------------------------------------------------

class TestTypeValidation:
    def test_invalid_sample_rate_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("SAMPLE_RATE", "not-an-int")
        dotenv = _make_env_file(tmp_path, "")
        with pytest.raises(ConfigError) as exc_info:
            Settings.from_env(dotenv_path=dotenv)
        assert "SAMPLE_RATE" in str(exc_info.value)

    def test_invalid_port_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("TELEMETRY_WS_PORT", "banana")
        dotenv = _make_env_file(tmp_path, "")
        with pytest.raises(ConfigError) as exc_info:
            Settings.from_env(dotenv_path=dotenv)
        assert "TELEMETRY_WS_PORT" in str(exc_info.value)


class TestPortRangeValidation:
    def test_port_zero_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("TELEMETRY_WS_PORT", "0")
        dotenv = _make_env_file(tmp_path, "")
        with pytest.raises(ConfigError):
            Settings.from_env(dotenv_path=dotenv)

    def test_port_65536_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("TELEMETRY_WS_PORT", "65536")
        dotenv = _make_env_file(tmp_path, "")
        with pytest.raises(ConfigError):
            Settings.from_env(dotenv_path=dotenv)

    def test_port_1_is_valid(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("TELEMETRY_WS_PORT", "1")
        dotenv = _make_env_file(tmp_path, "")
        cfg = Settings.from_env(dotenv_path=dotenv)
        assert cfg.telemetry_ws_port == 1

    def test_port_65535_is_valid(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("TELEMETRY_WS_PORT", "65535")
        dotenv = _make_env_file(tmp_path, "")
        cfg = Settings.from_env(dotenv_path=dotenv)
        assert cfg.telemetry_ws_port == 65535

    def test_negative_sample_rate_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("SAMPLE_RATE", "-1")
        dotenv = _make_env_file(tmp_path, "")
        with pytest.raises(ConfigError):
            Settings.from_env(dotenv_path=dotenv)

    def test_zero_sample_rate_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("SAMPLE_RATE", "0")
        dotenv = _make_env_file(tmp_path, "")
        with pytest.raises(ConfigError):
            Settings.from_env(dotenv_path=dotenv)


# ---------------------------------------------------------------------------
# Immutability and repr safety
# ---------------------------------------------------------------------------

class TestSettingsBehaviour:
    def test_settings_is_frozen(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Frozen dataclass: attribute assignment must raise."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        dotenv = _make_env_file(tmp_path, "")
        cfg = Settings.from_env(dotenv_path=dotenv)
        with pytest.raises((AttributeError, TypeError)):
            cfg.sample_rate = 999  # type: ignore[misc]

    def test_repr_does_not_leak_key(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """__repr__ must never contain the actual API key value."""
        secret = "do-not-leak-me-abc123"
        monkeypatch.setenv("GEMINI_API_KEY", secret)
        dotenv = _make_env_file(tmp_path, "")
        cfg = Settings.from_env(dotenv_path=dotenv)
        assert secret not in repr(cfg)

    def test_load_settings_convenience_wrapper(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """load_settings() returns a valid Settings object."""
        monkeypatch.setenv("GEMINI_API_KEY", "wrapper-key")
        dotenv = _make_env_file(tmp_path, "")
        cfg = load_settings(dotenv_path=dotenv)
        assert isinstance(cfg, Settings)
        assert cfg.gemini_api_key == "wrapper-key"


# ---------------------------------------------------------------------------
# TTS engine + wake-word settings
# ---------------------------------------------------------------------------


class TestTtsAndWakeWordSettings:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        for v in ("TTS_ENGINE", "TTS_GEMINI_VOICE", "WAKE_WORD", "WAKE_WORD_ENABLED"):
            monkeypatch.delenv(v, raising=False)
        cfg = Settings.from_env(dotenv_path=_make_env_file(tmp_path, ""))
        assert cfg.tts_engine == "gemini"
        assert cfg.tts_gemini_voice == "Kore"
        assert cfg.wake_word == "friday"
        assert cfg.wake_word_enabled is True

    def test_tts_engine_normalised_and_validated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setenv("TTS_ENGINE", "  Kokoro  ")
        cfg = Settings.from_env(dotenv_path=_make_env_file(tmp_path, ""))
        assert cfg.tts_engine == "kokoro"

    def test_tts_engine_invalid_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setenv("TTS_ENGINE", "espeak")
        with pytest.raises(ConfigError):
            Settings.from_env(dotenv_path=_make_env_file(tmp_path, ""))

    @pytest.mark.parametrize(
        "raw,expected",
        [("1", True), ("true", True), ("yes", True), ("on", True),
         ("0", False), ("false", False), ("no", False), ("off", False),
         ("TRUE", True), ("Off", False)],
    )
    def test_wake_word_enabled_bool_parsing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str, expected: bool
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setenv("WAKE_WORD_ENABLED", raw)
        cfg = Settings.from_env(dotenv_path=_make_env_file(tmp_path, ""))
        assert cfg.wake_word_enabled is expected

    def test_wake_word_enabled_invalid_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setenv("WAKE_WORD_ENABLED", "maybe")
        with pytest.raises(ConfigError):
            Settings.from_env(dotenv_path=_make_env_file(tmp_path, ""))

    def test_repr_redacts_key_with_new_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value")
        cfg = Settings.from_env(dotenv_path=_make_env_file(tmp_path, ""))
        r = repr(cfg)
        assert "super-secret-value" not in r
        assert "wake_word=" in r and "tts_engine=" in r
