# Project FRIDAY — Engineering Constitution

A local, always-on voice assistant: **Gemini brain + voice I/O + Linux OS control + web automation + persistent memory + Electron HUD.** Read this file fully before doing anything.

## 0. The one distinction that governs everything: build-time vs runtime

- **Build-time MCP servers** (codebase-memory, playwright, context7, and the opt-in ones) are tools *you, the agent, use to construct FRIDAY*. They do **not** ship inside the product.
- **Runtime stack** is plain Python the shipped app calls directly: Gemini (brain), Kokoro-82M (TTS), `browser-use`/`Scrapling` (web), `sounddevice` (audio), SQLite (memory), and the Linux OS bridge (AT-SPI + ydotool/xdotool).

Never put an MCP server in the runtime path. Never reach for a runtime library to do a build-time exploration task. If a change blurs this line, stop and flag it.

## 1. Plan economics (Claude Pro — non-negotiable)

Chat + Claude Code share **one** Pro usage pool (rolling 5h window + weekly cap). Discipline:

- **Prefer `codebase-memory` graph tools over grep/glob/Read for structural questions.** A graph query is ~hundreds of tokens; file-by-file exploration is tens of thousands.
- **Keep optional MCP servers OFF** until you're in the phase that needs them (see `docs/mcp/OPTIONAL-SERVERS.md`). Each loaded server adds tool-schema tokens to every turn.
- **Subagent-first.** Delegate isolated tasks to specialists (cheap, single-session context). **Agent Teams / parallel agents stay gated** — only on explicit human go for a burst, never by default.
- Default model is **Sonnet**. Use Opus only for genuinely hard planning/integration, on request.

## 2. Architecture (hub-and-spoke)

The **main thread** is the only hub with the `Task` tool; it delegates to specialists. Specialists never talk to each other — all shared state flows through `docs/STATUS.md`, ADRs, and PR descriptions. Specialists:

| Agent | Owns |
|---|---|
| **atlas** | Planning, integration, cross-cutting decisions |
| **forge** | Core runtime: async loop, Gemini brain, state machine, config |
| **echo** | Voice I/O: capture, STT, Kokoro TTS, 24 kHz streaming, barge-in |
| **vector** | Linux OS bridge: AT-SPI / ydotool, subprocess, file ops |
| **scout** | Web agent: browser-use + Scrapling (prototyped via Playwright MCP) |
| **mnemo** | Memory: SQLite entity store + recall API |
| **halo** | HUD: Electron + React + Three.js + telemetry WebSocket |
| **sentinel** | QA + security review, dependency pinning, tests |

## 3. Security posture (this app controls your machine)

FRIDAY combines OS control + browser automation + a **cloud** brain that can see screen/audio context. Treat it as a real attack surface:

- **Pin every dependency.** Verify package names against the official source before adding — do not trust look-alikes (e.g. the malicious `playwright-mcp` typosquat; the only correct one is `@playwright/mcp`).
- The OS bridge must support a **dry-run / confirm mode** for destructive actions (file deletes, app kills, sends).
- Never log secrets or full screen captures to disk by default. `.env` is git-ignored; there is an `.env.example`.
- `sentinel` reviews any code that executes shell, touches the filesystem broadly, or sends data off-device.

## 4. Workflow rules

1. After each PLAN.md step: summarise files created/modified/deleted, then **pause** and ask to proceed. Never merge or skip steps.
2. Read a file before editing it.
3. If the plan conflicts with reality, describe the conflict and propose two options — don't guess.
4. Delete nothing unless PLAN.md says so.
5. Branching: `feature/<agent>-<slug>` → `dev` (CI + sentinel) → `main`. Commit daily.
6. Items needing a human (secrets, billing, OS confirmation) go to the `[REVIEW]` list in `docs/STATUS.md`.
