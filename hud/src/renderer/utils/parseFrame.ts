/**
 * parseFrame — pure utility extracted from useTelemetry for testability.
 *
 * Validates and parses a raw JSON string into a TelemetryFrame.
 * Returns null for any malformed, missing-field, or out-of-range input.
 * Never throws.
 */

import type { TelemetryFrame, AssistantState } from "../types";

const VALID_STATES = new Set<string>([
  "idle",
  "listening",
  "thinking",
  "speaking",
  "acting",
]);

export function parseFrame(raw: string): TelemetryFrame | null {
  try {
    const obj: unknown = JSON.parse(raw);
    if (typeof obj !== "object" || obj === null) return null;

    const r = obj as Record<string, unknown>;

    const ts = r["ts"];
    const state = r["state"];
    const cpu_pct = r["cpu_pct"];
    const ram_pct = r["ram_pct"];
    const audio_level = r["audio_level"];

    if (
      typeof ts !== "number" ||
      typeof state !== "string" ||
      !VALID_STATES.has(state) ||
      typeof cpu_pct !== "number" ||
      typeof ram_pct !== "number" ||
      typeof audio_level !== "number"
    ) {
      return null;
    }

    return {
      ts,
      state: state as AssistantState,
      cpu_pct: Math.max(0, Math.min(100, cpu_pct)),
      ram_pct: Math.max(0, Math.min(100, ram_pct)),
      audio_level: Math.max(0, Math.min(1, audio_level)),
    };
  } catch {
    return null;
  }
}
