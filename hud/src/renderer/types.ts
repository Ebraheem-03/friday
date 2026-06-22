/**
 * Wire types — mirror the Python TelemetryServer frame schema exactly.
 * The HUD is a pure consumer; it never sends frames.
 */

export type AssistantState =
  | "idle"
  | "listening"
  | "thinking"
  | "speaking"
  | "acting";

export interface TelemetryFrame {
  /** Unix epoch seconds */
  ts: number;
  state: AssistantState;
  /** 0–100 */
  cpu_pct: number;
  /** 0–100 */
  ram_pct: number;
  /** 0.0–1.0 */
  audio_level: number;
}

/** Runtime connection status reported by useTelemetry */
export type ConnectionStatus = "connecting" | "connected" | "disconnected";
