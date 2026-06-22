#!/usr/bin/env python3
"""Live smoke test for core/brain.py — NOT collected by default pytest.

Run from the repo root after creating a real .env:

    python3 scripts/smoke_brain.py

Requires:
    - GEMINI_API_KEY in .env or environment.
    - google-genai==2.9.0 installed.
    - Network access to Gemini API.

Exit codes:
    0 — stream completed successfully.
    1 — configuration error (missing/invalid API key or model).
    2 — API error (auth failure, model not found, quota, network).

Security note: the API key is never printed; only the model id is shown.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so imports work when run directly.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


PROMPT = "Say hello in 5 words."


async def main() -> int:
    from core.config import ConfigError, Settings
    from core.brain import Brain

    # --- Load settings (reads .env from repo root) --------------------------
    dotenv_path = _REPO_ROOT / ".env"
    try:
        settings = Settings.from_env(dotenv_path=dotenv_path)
    except ConfigError as exc:
        print(f"[smoke_brain] Configuration error: {exc}", file=sys.stderr)
        return 1

    print(f"[smoke_brain] Model: {settings.gemini_model}")
    print(f"[smoke_brain] Prompt: {PROMPT!r}")
    print("[smoke_brain] Streaming response:")

    brain = Brain(settings)

    try:
        async for delta in brain.stream(PROMPT, history=[]):
            print(delta, end="", flush=True)
        print()  # newline after streaming finishes
    except Exception as exc:
        # Catch auth/quota/model errors and print a clear message.
        err_str = str(exc)
        if "API_KEY" in err_str.upper() or "PERMISSION" in err_str.upper() or "UNAUTHENTICATED" in err_str.upper():
            print(
                f"\n[smoke_brain] Auth error — check GEMINI_API_KEY: {exc}",
                file=sys.stderr,
            )
        elif "NOT_FOUND" in err_str.upper() or "model" in err_str.lower():
            print(
                f"\n[smoke_brain] Model not found or unavailable: {exc}",
                file=sys.stderr,
            )
        else:
            print(
                f"\n[smoke_brain] API error: {exc}",
                file=sys.stderr,
            )
        return 2

    print("[smoke_brain] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
