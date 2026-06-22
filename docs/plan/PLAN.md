# FRIDAY — Build Plan

Execute strictly in order. After **each step**: summarise files created/modified/deleted, then say "Ready for Step N+1 — proceed?" and wait. Never merge or skip steps. Read before editing. On conflict, propose two options.

> Tooling note: keep only the lean core MCPs on (codebase-memory, playwright, context7). Enable an optional server only inside its phase (`docs/mcp/OPTIONAL-SERVERS.md`), then remove it.

## Step 0 — Orient
- Read `CLAUDE.md` and all ADRs. Index this repo: tell `codebase-memory` to index the project, then prefer graph queries for structural questions.
- Output a findings summary of the skeleton. **[REVIEW gate]**

## Step 1 — Repo skeleton + CI (forge)
- `pyproject.toml`, `.gitignore`, `README.md`, `.env.example` already stubbed — fill `pyproject` deps (pinned).
- Add a CI workflow stub (lint + pytest). Initialise git; create empty `dev`/`main`.

## Step 2 — Core config + state machine (forge)
- Implement `core/config.py` (typed env settings) and `core/state.py` (idle/listening/thinking/speaking/acting).
- Unit tests for state transitions.

## Step 3 — Gemini brain (forge)
- `core/brain.py`: streaming Gemini client + tool-call routing to runtime modules (memory/os/web). Use `context7` for current SDK.
- `core/orchestrator.py`: async supervisor wiring the stages with backpressure queues.
- Smoke test: text-in → streamed text-out.

## Step 4 — Voice I/O (echo)
- `voice/capture.py` (sounddevice + VAD + barge-in), `voice/tts_kokoro.py`, `voice/stream.py` (continuous 24 kHz chunking off the token stream).
- Target sub-second first-audio. Latency note in each file.

## Step 5 — Memory (mnemo)
- `memory/store.py` (SQLite WAL entity/relation schema + migrations), `memory/recall.py` (write/query API).
- Wire `brain.py` to call recall. Test: "remember X" → restart → recalls X.

## Step 6 — Linux OS bridge (vector)  **[REVIEW gate: confirm X11/Wayland + DE first]**
- Enable `computer-use-linux` MCP; prototype find/click/type/window flows.
- Implement `core/os_bridge.py` natively (AT-SPI + ydotool/xdotool, display-server auto-detect). **Confirm/dry-run gate on all destructive actions.**
- Remove the MCP when done. `sentinel` reviews before merge.

## Step 7 — Web agent (scout)
- Prototype routines with the `playwright` MCP; ship `web/agent.py` (browser-use) + `web/scrape.py` (Scrapling).
- Return clean structured data to the brain.

## Step 8 — HUD (halo)
- Electron shell + React/Three.js telemetry HUD + audio-reactive visuals.
- Runtime telemetry WebSocket (CPU/RAM + audio frequency) from Python → HUD.
- Drive HUD states from `state.py`. Optional: stitch/21st.dev/frontend-design + an impeccable-style audit pass.

## Step 9 — Integration + hardening (atlas + sentinel)
- End-to-end: wake → listen → think → speak → act, with HUD reacting.
- `sentinel` full pass: dependency provenance/pinning, security gates, CI green. `dev`→`main`.
