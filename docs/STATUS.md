# FRIDAY — STATUS

_Single source of cross-agent truth. Atlas keeps this current._

## Now
- Step 2 complete (forge). Ready for Step 3 (Gemini brain + orchestrator).

## Environment (confirmed 2026-06-22)
- **Display server: Wayland / GNOME (ubuntu:GNOME)** → vector (Step 6) is Wayland-first: AT-SPI + ydotool; xdotool is X11-only, treat as fallback/no-op on Wayland.
- GEMINI_API_KEY: present in .env (Step 3 smoke test unblocked).
- MCPs live: codebase-memory ✓, context7 ✓, magic/21st.dev ✓. **Gap: @playwright/mcp NOT configured** — scout needs it at build-time for Step 7.

## Next
- Step 3: core/brain.py (Gemini streaming client + tool-call routing) + core/orchestrator.py (async supervisor with backpressure queues). Use context7 for current google-genai SDK surface.

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
- _(append: date — agent — what changed)_
