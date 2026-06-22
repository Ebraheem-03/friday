# FRIDAY — STATUS

_Single source of cross-agent truth. Atlas keeps this current._

## Now
- Steps 1–3 complete, committed & pushed to origin/dev. **CI green** (0246631). Ready for Step 4 (echo: voice I/O).


## Environment (confirmed 2026-06-22)
- **Display server: Wayland / GNOME (ubuntu:GNOME)** → vector (Step 6) is Wayland-first: AT-SPI + ydotool; xdotool is X11-only, treat as fallback/no-op on Wayland.
- GEMINI_API_KEY: present in .env (Step 3 smoke test unblocked).
- MCPs live: codebase-memory ✓, context7 ✓, magic/21st.dev ✓. **Gap: @playwright/mcp NOT configured** — scout needs it at build-time for Step 7.
- **Dependency layout:** only `google-genai` is always-on. Runtime stack split into pyproject extras — `[voice]` (echo), `[web]` (scout), `[os]` (vector), `[dev]` (CI). Install only your extra in-phase (see docs/SETUP.md §4b).
- **PROJECT RULE — CPU-only, no CUDA:** torch/transformers (via kokoro) must use the PyTorch CPU index. `voice` extra installs with `-c constraints/cpu.txt`. echo (Step 4) must pin exact `torch+cpu`/`transformers` there.

## Next
- Step 4: echo — voice I/O (STT capture, Kokoro TTS, 24 kHz PCM streaming, barge-in).

## [REVIEW] — needs the human
- [x] **OS:** confirm X11 vs Wayland + desktop environment (blocks `vector` window targeting). Wayland otherwise you can check.
- [x] **Secrets:** `GEMINI_API_KEY` in `friday/.env` (blocks `forge`/`brain`). Already placed.
- [x] **codebase-memory:** run install script; confirm `codebase-memory-mcp` on PATH. Installed the script you should check. 
- [x] **OS host setup:** Run echo "$XDG_SESSION_TYPE  /  $XDG_CURRENT_DESKTOP" to understand yourself.
- [x] **(optional) 21st.dev** API key; **(optional) Stitch** GCP project + billing. Configured in claude global mcps you can use claude mcp list to see.
- [x] Create the git repo and empty `dev` + `main` branches.

## Log
- 2026-06-22 — atlas — Step 0 Orient: indexed repo (166 nodes/165 edges), read all ADRs; confirmed stubs consistent with ADRs. Noted git already initialised (dev/main exist) — Step 1 git-init sub-task is a no-op. Delegated Step 1 to forge.
- 2026-06-22 — forge — Step 1: pinned all 8 packages in pyproject.toml (==exact), added [build-system] + [tool.pytest.ini_options]; created .github/workflows/ci.yml (lint+pytest, PyGObject apt deps included). Versions resolved from PyPI JSON API — all confirmed stable.
- 2026-06-22 — sentinel — Step 1 security + correctness review. All 8 package pins verified live against PyPI JSON: canonical names, no typosquats, all pinned versions exist as stable releases. Two bugs found and fixed: (1) invalid build-backend `setuptools.backends.legacy:build` → corrected to `setuptools.build_meta`; (2) missing `tests/` dir would have caused pytest exit-5 (CI red) → added `tests/__init__.py` + `tests/test_smoke.py` placeholder. browser-use telemetry noted (MEDIUM, non-blocking). VERDICT: GO.
- 2026-06-22 — atlas — Step 1 committed (f490be0 on dev, held local). Human cleared all [REVIEW] gates. Env confirmed: Wayland/GNOME, GEMINI_API_KEY set, codebase-memory/context7/magic MCPs live; @playwright/mcp absent (Step 7 risk). Delegated Step 2 to forge.
- 2026-06-22 — forge — Step 2: implemented core/config.py (frozen Settings dataclass, stdlib-only .env parser, ConfigError fail-fast, no new deps) and core/state.py (State enum, StateMachine with guard + observer hook, InvalidTransition). Added tests/test_config.py (28 tests) and tests/test_state.py (41 tests). Full suite: 71 passed.
- 2026-06-22 — forge — Step 3: implemented core/brain.py (Brain class, MemoryTool/OSTool/WebTool Protocols, 4 Gemini function declarations, no-op stubs, async generator stream + _run_stream with manual tool-call routing) and core/orchestrator.py (async supervisor, bounded asyncio.Queue backpressure, StateMachine-driven, clean cancellation). Added gemini_model field to Settings (GEMINI_MODEL env, default "gemini-2.5-flash"). Added tests/test_brain.py (28 tests) and tests/test_orchestrator.py (12 tests). Created scripts/smoke_brain.py (live smoke, NOT collected by CI). Full suite: 111 passed, 1 skipped. Ruff clean.
- 2026-06-22 — sentinel — Step 3 security review. One MUST-FIX found and patched: `_run_stream` recursed without a depth bound — a prompt-injected or misbehaving model could chain tool calls indefinitely (stack overflow / infinite API burn). Fix: added `_MAX_TOOL_ROUNDS = 5` constant and `_depth` parameter; recursion halts at limit and logs a warning. Two new tests added (depth-limit stops infinite chain; normal single tool-call unaffected). Suite: 113 passed, 1 skipped. All other checks clear: GEMINI_API_KEY never logged/printed/repr'd; smoke script exception paths print SDK error body only (key is in HTTP header, not response body); only user text sent to Gemini (no ambient screen/audio context); tool router uses explicit allowlist with unknown-tool error-string return (no crash, no injection); no MCP imports in runtime path; orchestrator queues bounded with maxsize=8 default, CancelledError suppressed in stop(), queues drained on shutdown; `pytest-asyncio` installed but not pinned in pyproject.toml [dev] (LOW, non-blocking for Step 3; atlas to add pin in a housekeeping PR). VERDICT: GO.
- 2026-06-22 — atlas — Step 3 committed (d92cc5e) + live smoke test PASSED (real gemini-2.5-flash, streamed text-out). Fixed a cascade of CI failures the local pytest never caught: (1) setuptools flat-layout discovery → explicit [tool.setuptools] packages; (2) full native/ML stack built in CI → split runtime deps into per-module extras, CI installs only .[dev]; (3) CPU-only project rule → constraints/cpu.txt + SETUP §4b; (4) ruff F401 unused import. CI now GREEN at 0246631. Steps 1–3 on origin/dev.
- _(append: date — agent — what changed)_
