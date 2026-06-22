# ADR-0007: HUD / interface

**Status:** Accepted

## Context
Want an Iron-Man-style telemetry HUD without hand-tweaking fragile CSS from scratch.

## Decision
Electron + React + Three.js. Optional design acceleration via Stitch (OAuth) and 21st.dev, plus frontend-design and an impeccable-style audit pass. HUD state is driven by the runtime state machine over a telemetry WebSocket.

## Consequences
Optional design MCPs need keys/billing and stay off until the HUD phase. No browser storage assumptions.
