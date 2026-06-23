/**
 * HUD renderer configuration.
 *
 * WS URL defaults to ws://127.0.0.1:8765.  Override at build time via the
 * VITE_TELEMETRY_WS_URL env var (e.g. in .env.local for development).
 * Never hardcode the URL in multiple places — import this module instead.
 */

export const TELEMETRY_WS_URL: string =
  import.meta.env["VITE_TELEMETRY_WS_URL"] ?? "ws://127.0.0.1:8765";
