# Optional MCP servers (enable per-phase, then remove)

These are **not** in `.mcp.json` on purpose — each one adds tool-schema tokens to every turn, which matters on Pro. Add with `claude mcp add --scope local` when you enter the phase, and `claude mcp remove` when done.

## OS control — Phase: OS bridge (vector)
```bash
claude mcp add --scope local computer-use-linux -- computer-use-linux mcp
```
Requires the Phase-3 host setup (ydotoold + AT-SPI). Wayland-first, X11 best-effort. Security-sensitive — keep off except while actively building/verifying `os_bridge.py`.

## Knowledge-graph memory (reference) — Phase: Memory (mnemo)
```bash
claude mcp add --scope local server-memory -- npx -y @modelcontextprotocol/server-memory
```
Use only as a shape reference. The runtime memory store is your own SQLite (`friday/memory/`). (Note: verify its on-disk format yourself before relying on it; don't assume.)

## UI generation — Phase: HUD (halo)
```bash
# 21st.dev components  (needs an API key)
claude mcp add --scope local magic -- npx -y @21st-dev/magic@latest   # env: API_KEY=$TWENTY_FIRST_API_KEY
# Google Stitch — robust OAuth path (NOT the fragile API-key path):
gcloud auth application-default login
claude mcp add --scope local stitch -- npx -y @_davideast/stitch-mcp proxy   # env: GOOGLE_CLOUD_PROJECT=...
```
Stitch needs a GCP project with **billing enabled** and the **Stitch API** enabled. The `STITCH_API_KEY` route commonly fails ("API keys are not supported"); use gcloud OAuth.

## ⚠️ Provenance
Only the scoped `@playwright/mcp` is the official Playwright server. Do **not** install the unscoped `playwright-mcp` — it is a known typosquat that injects content and exfiltrates page data.
