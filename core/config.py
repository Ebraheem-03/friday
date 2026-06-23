"""Typed settings loaded from environment. No secrets in code.

Latency note: Settings.from_env() is called once at startup (or during tests). It
does a single pass over the .env file and os.environ — O(n) on the number of env
vars, negligible vs. network or audio I/O.

Usage:
    from core.config import Settings, load_settings
    cfg = load_settings()  # raises ConfigError fast if anything is wrong
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Raised when required configuration is missing or invalid.

    The message names the offending variable but never reveals its value so that
    secrets are never leaked into logs or tracebacks.
    """


# ---------------------------------------------------------------------------
# Internal .env parser (stdlib-only; no python-dotenv)
# ---------------------------------------------------------------------------

def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict of KEY -> VALUE pairs.

    Rules:
    - Blank lines and lines starting with '#' are ignored.
    - Each line must be KEY=VALUE; anything else is silently skipped.
    - Values may optionally be quoted with single or double quotes (stripped).
    - Real os.environ values ALWAYS take precedence over .env values (callers
      must enforce this — see Settings.from_env()).
    - Does NOT mutate os.environ; returns a plain dict.
    """
    result: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return result  # .env is optional; CI uses real env vars
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return result

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, raw_value = stripped.partition("=")
        key = key.strip()
        value = raw_value.strip()
        # Strip matching outer quotes (single or double) if present
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            result[key] = value

    return result


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

def _require_str(env: dict[str, str], key: str) -> str:
    """Return env[key] as a non-empty string or raise ConfigError."""
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(
            f"Required environment variable '{key}' is missing or empty. "
            "Set it in your shell or .env file."
        )
    return value


def _opt_str(env: dict[str, str], key: str, default: str) -> str:
    value = env.get(key, "").strip()
    return value if value else default


def _opt_int(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(
            f"Environment variable '{key}' must be an integer; got a non-numeric value."
        ) from None


def _opt_bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(
        f"Environment variable '{key}' must be a boolean (1/0/true/false); got {raw!r}."
    )


def _validate_port(value: int, key: str) -> int:
    if not (1 <= value <= 65535):
        raise ConfigError(
            f"Environment variable '{key}' must be in the range 1–65535; got {value}."
        )
    return value


# ---------------------------------------------------------------------------
# Settings dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable, fully-validated runtime configuration.

    Fields
    ------
    gemini_api_key : str
        Required. Gemini API key — never logged or printed.
    gemini_model : str
        Gemini model ID to use for inference. Default: "gemini-2.5-flash".
        Override with GEMINI_MODEL env var (e.g. "gemini-2.0-flash").
    sample_rate : int
        Audio sample rate in Hz. Default: 24000 (matches Kokoro TTS output).
    tts_voice : str
        Kokoro TTS voice identifier. Default: "af_heart".
    tts_engine : str
        TTS backend: "gemini" (near-human cloud, ~3-5 s/reply, default) or
        "kokoro" (local, instant, robotic). Override with TTS_ENGINE.
    tts_gemini_voice : str
        Gemini prebuilt voice name when tts_engine="gemini". Default: "Kore"
        (others: Puck, Charon, Aoede, Fenrir, Leda, ...). Override TTS_GEMINI_VOICE.
    tts_gemini_model : str
        Gemini TTS model id. Default: "gemini-2.5-flash-preview-tts".
    stt_model : str
        faster-whisper model size for speech-to-text. Default: "base.en".
        Override with STT_MODEL env var (e.g. "small", "medium.en").
        "base.en" gives good latency/accuracy on CPU for English speech.
    telemetry_ws_port : int
        WebSocket port for the HUD telemetry server. Default: 8765. Range: 1–65535.
    log_level : str
        Python logging level name. Default: "INFO".
    wake_word : str
        Wake word that must prefix a command for FRIDAY to respond. Default:
        "friday". Matching is case-insensitive on the Whisper transcript.
        Override with WAKE_WORD.
    wake_word_enabled : bool
        When True (default), FRIDAY ignores any utterance that does not contain
        the wake word — the always-on posture. Set WAKE_WORD_ENABLED=0 to make
        it respond to every utterance (push-to-talk / quiet-room mode).
    """

    gemini_api_key: str
    gemini_model: str
    sample_rate: int
    tts_voice: str
    tts_engine: str
    tts_gemini_voice: str
    tts_gemini_model: str
    stt_model: str
    telemetry_ws_port: int
    log_level: str
    wake_word: str
    wake_word_enabled: bool

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, dotenv_path: Path | None = None) -> "Settings":
        """Load and validate settings from the environment.

        Resolution order (highest wins):
        1. Real os.environ values.
        2. Values parsed from the .env file (if present).

        The .env file is looked up at ``dotenv_path`` when provided, otherwise
        at ``<repo-root>/.env`` (i.e. the parent of the directory containing
        this file).

        Raises
        ------
        ConfigError
            If a required variable is missing/empty or a typed variable is invalid.
        """
        # Locate .env relative to this file: friday/core/config.py -> friday/.env
        if dotenv_path is None:
            dotenv_path = Path(__file__).parent.parent / ".env"

        file_vars = _parse_dotenv(dotenv_path)

        # Merge: os.environ takes precedence
        env: dict[str, str] = {**file_vars, **os.environ}

        # --- Required ---
        gemini_api_key = _require_str(env, "GEMINI_API_KEY")

        # --- Optional with defaults ---
        gemini_model = _opt_str(env, "GEMINI_MODEL", "gemini-2.5-flash")
        sample_rate = _opt_int(env, "SAMPLE_RATE", 24000)
        tts_voice = _opt_str(env, "TTS_VOICE", "af_heart")
        tts_engine = _opt_str(env, "TTS_ENGINE", "gemini").strip().lower()
        tts_gemini_voice = _opt_str(env, "TTS_GEMINI_VOICE", "Kore")
        tts_gemini_model = _opt_str(env, "TTS_GEMINI_MODEL", "gemini-2.5-flash-preview-tts")
        stt_model = _opt_str(env, "STT_MODEL", "base.en")
        telemetry_ws_port = _opt_int(env, "TELEMETRY_WS_PORT", 8765)
        log_level = _opt_str(env, "LOG_LEVEL", "INFO")
        wake_word = _opt_str(env, "WAKE_WORD", "friday")
        wake_word_enabled = _opt_bool(env, "WAKE_WORD_ENABLED", True)

        # --- Validation ---
        _validate_port(telemetry_ws_port, "TELEMETRY_WS_PORT")
        # sample_rate must be positive; guard against 0 or negative
        if sample_rate <= 0:
            raise ConfigError(
                f"Environment variable 'SAMPLE_RATE' must be a positive integer; got {sample_rate}."
            )
        if tts_engine not in ("gemini", "kokoro"):
            raise ConfigError(
                f"Environment variable 'TTS_ENGINE' must be 'gemini' or 'kokoro'; got {tts_engine!r}."
            )

        return cls(
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
            sample_rate=sample_rate,
            tts_voice=tts_voice,
            tts_engine=tts_engine,
            tts_gemini_voice=tts_gemini_voice,
            tts_gemini_model=tts_gemini_model,
            stt_model=stt_model,
            telemetry_ws_port=telemetry_ws_port,
            log_level=log_level,
            wake_word=wake_word,
            wake_word_enabled=wake_word_enabled,
        )

    def __repr__(self) -> str:
        """Never include the API key value in repr output."""
        key_hint = "***" if self.gemini_api_key else "(empty)"
        return (
            f"Settings("
            f"gemini_api_key={key_hint}, "
            f"gemini_model={self.gemini_model!r}, "
            f"sample_rate={self.sample_rate}, "
            f"tts_voice={self.tts_voice!r}, "
            f"tts_engine={self.tts_engine!r}, "
            f"tts_gemini_voice={self.tts_gemini_voice!r}, "
            f"stt_model={self.stt_model!r}, "
            f"telemetry_ws_port={self.telemetry_ws_port}, "
            f"wake_word={self.wake_word!r}, "
            f"wake_word_enabled={self.wake_word_enabled}, "
            f"log_level={self.log_level!r})"
        )


# ---------------------------------------------------------------------------
# Module-level convenience loader
# ---------------------------------------------------------------------------

def load_settings(dotenv_path: Path | None = None) -> Settings:
    """Convenience wrapper around Settings.from_env().

    Prefer this in application entry-points; use Settings.from_env() directly
    in tests where you need to control the dotenv_path.
    """
    return Settings.from_env(dotenv_path=dotenv_path)
