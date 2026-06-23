# FRIDAY HUD (Electron + React + Three.js)

Iron-Man style translucent telemetry HUD. Owned by the `halo` agent.

## Stack

- Electron 39.8.10 — frameless transparent always-on-top window
- React 19.2.7 + TypeScript 6.0.3 — renderer
- Three.js 0.184.0 — GPU-accelerated HUD scene (arc-reactor core, audio-reactive rings, CPU/RAM gauges)
- Vite 8.0.16 — renderer bundler
- Vitest 4.1.9 — unit tests (pure functions only; no browser/Electron)

## Quick start (local)

Requires Node 20+. The Python telemetry server is NOT required to start the HUD — it shows "WAITING FOR FRIDAY" and auto-reconnects when the server comes up.

```sh
cd hud
npm install
npm run build        # bundle the renderer
npm start            # launch Electron window
```

### Linux notes (one-time)

1. **Electron binary missing?** If `npm start` fails with *"Electron failed to install
   correctly"*, the platform binary was skipped at install time (e.g. CI sets
   `ELECTRON_SKIP_BINARY_DOWNLOAD=1`). Fetch it without that flag:

   ```sh
   ( unset ELECTRON_SKIP_BINARY_DOWNLOAD; node node_modules/electron/install.js )
   ```

2. **`chrome-sandbox` SUID permission.** Electron's setuid sandbox helper must be
   root-owned, mode 4755, or Electron aborts (it refuses to run unsandboxed). We keep
   the renderer sandbox ON, so set the permission rather than passing `--no-sandbox`:

   ```sh
   sudo chown root:root node_modules/electron/dist/chrome-sandbox
   sudo chmod 4755 node_modules/electron/dist/chrome-sandbox
   ```

## Development workflow

```sh
npm run dev          # Vite dev server at http://localhost:5173 (renderer only)
npm run typecheck    # tsc --noEmit (strict)
npm run test         # vitest run (pure-function unit tests, ~1s)
npm run build        # typecheck node files + vite build
```

## Telemetry WebSocket

The HUD connects to `ws://127.0.0.1:8765` by default (from Python `core/telemetry.py`, Step 8a).

Override via env at build time:

```sh
VITE_TELEMETRY_WS_URL=ws://127.0.0.1:9000 npm run build
```

Frame schema (mirror of Python `TelemetryFrame`):

```ts
{
  ts: number;          // epoch seconds
  state: "idle" | "listening" | "thinking" | "speaking" | "acting";
  cpu_pct: number;     // 0–100
  ram_pct: number;     // 0–100
  audio_level: number; // 0.0–1.0
}
```

## State visuals

| State | Colour | Behaviour |
|-------|--------|-----------|
| idle | Dim cyan `#00c8ff` | Slow ring rotation, no pulse |
| listening | Bright cyan `#00ffee` | Pulsing core, mild audio reactivity |
| thinking | Blue `#3388ff` | Spinning arc segments |
| speaking | Vivid green-cyan `#00ffaa` | Full audio-reactive ring expansion |
| acting | Gold `#ffd700` | Gold accent, spinning + pulsing alert |

The `stateVisual(state)` function in `src/renderer/visuals/stateVisual.ts` is the single source of truth for all visual parameters and is fully unit-tested.

## Security

- `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`
- No `@electron/remote`
- Preload exposes nothing privileged
- Restrictive CSP: `default-src 'self'; connect-src 'self' ws://127.0.0.1:*`
- No CDN resources — everything bundled locally
- No `localStorage` / `sessionStorage` assumptions (per ADR-0007)

## CI

The `hud` job in `.github/workflows/ci.yml` runs `npm ci` → `typecheck` → `test` → `build` with `ELECTRON_SKIP_BINARY_DOWNLOAD=1` (no Electron binary needed in CI).
