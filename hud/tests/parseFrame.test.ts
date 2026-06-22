/**
 * Unit tests for parseFrame — the TelemetryFrame JSON validator.
 *
 * No React/Three.js/DOM dependencies. Runs in a plain node environment.
 */

import { describe, it, expect } from "vitest";
import { parseFrame } from "../src/renderer/utils/parseFrame";

function validRaw(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    ts: 1700000000,
    state: "idle",
    cpu_pct: 42.5,
    ram_pct: 60.0,
    audio_level: 0.3,
    ...overrides,
  });
}

describe("parseFrame", () => {
  // --- happy path ----------------------------------------------------------

  it("parses a well-formed idle frame", () => {
    const frame = parseFrame(validRaw());
    expect(frame).not.toBeNull();
    expect(frame!.state).toBe("idle");
    expect(frame!.cpu_pct).toBe(42.5);
    expect(frame!.ram_pct).toBe(60.0);
    expect(frame!.audio_level).toBe(0.3);
    expect(frame!.ts).toBe(1700000000);
  });

  it("accepts all valid states", () => {
    const states = ["idle", "listening", "thinking", "speaking", "acting"];
    for (const state of states) {
      const frame = parseFrame(validRaw({ state }));
      expect(frame).not.toBeNull();
      expect(frame!.state).toBe(state);
    }
  });

  it("clamps cpu_pct above 100 to 100", () => {
    const frame = parseFrame(validRaw({ cpu_pct: 110 }));
    expect(frame!.cpu_pct).toBe(100);
  });

  it("clamps cpu_pct below 0 to 0", () => {
    const frame = parseFrame(validRaw({ cpu_pct: -5 }));
    expect(frame!.cpu_pct).toBe(0);
  });

  it("clamps ram_pct above 100 to 100", () => {
    const frame = parseFrame(validRaw({ ram_pct: 200 }));
    expect(frame!.ram_pct).toBe(100);
  });

  it("clamps ram_pct below 0 to 0", () => {
    const frame = parseFrame(validRaw({ ram_pct: -1 }));
    expect(frame!.ram_pct).toBe(0);
  });

  it("clamps audio_level above 1 to 1", () => {
    const frame = parseFrame(validRaw({ audio_level: 1.5 }));
    expect(frame!.audio_level).toBe(1);
  });

  it("clamps audio_level below 0 to 0", () => {
    const frame = parseFrame(validRaw({ audio_level: -0.1 }));
    expect(frame!.audio_level).toBe(0);
  });

  it("accepts exact boundary values (0 and 100 / 1.0)", () => {
    const frame = parseFrame(
      validRaw({ cpu_pct: 0, ram_pct: 100, audio_level: 1.0 })
    );
    expect(frame).not.toBeNull();
    expect(frame!.cpu_pct).toBe(0);
    expect(frame!.ram_pct).toBe(100);
    expect(frame!.audio_level).toBe(1);
  });

  it("preserves extra unknown fields gracefully (ignores them)", () => {
    const frame = parseFrame(validRaw({ unknown_field: "surprise" }));
    expect(frame).not.toBeNull();
    expect(frame).not.toHaveProperty("unknown_field");
  });

  // --- null / reject cases -------------------------------------------------

  it("returns null for an empty string", () => {
    expect(parseFrame("")).toBeNull();
  });

  it("returns null for invalid JSON", () => {
    expect(parseFrame("{not json}")).toBeNull();
  });

  it("returns null for JSON null", () => {
    expect(parseFrame("null")).toBeNull();
  });

  it("returns null for a JSON array", () => {
    expect(parseFrame("[1, 2, 3]")).toBeNull();
  });

  it("returns null for a JSON number", () => {
    expect(parseFrame("42")).toBeNull();
  });

  it("returns null when ts is missing", () => {
    const obj = { state: "idle", cpu_pct: 10, ram_pct: 20, audio_level: 0.1 };
    expect(parseFrame(JSON.stringify(obj))).toBeNull();
  });

  it("returns null when state is missing", () => {
    const obj = { ts: 1700000000, cpu_pct: 10, ram_pct: 20, audio_level: 0.1 };
    expect(parseFrame(JSON.stringify(obj))).toBeNull();
  });

  it("returns null when cpu_pct is missing", () => {
    const obj = { ts: 1700000000, state: "idle", ram_pct: 20, audio_level: 0.1 };
    expect(parseFrame(JSON.stringify(obj))).toBeNull();
  });

  it("returns null when ram_pct is missing", () => {
    const obj = { ts: 1700000000, state: "idle", cpu_pct: 10, audio_level: 0.1 };
    expect(parseFrame(JSON.stringify(obj))).toBeNull();
  });

  it("returns null when audio_level is missing", () => {
    const obj = { ts: 1700000000, state: "idle", cpu_pct: 10, ram_pct: 20 };
    expect(parseFrame(JSON.stringify(obj))).toBeNull();
  });

  it("returns null when state is an unknown string", () => {
    expect(parseFrame(validRaw({ state: "flying" }))).toBeNull();
  });

  it("returns null when state is an empty string", () => {
    expect(parseFrame(validRaw({ state: "" }))).toBeNull();
  });

  it("returns null when state is a number", () => {
    expect(parseFrame(validRaw({ state: 0 }))).toBeNull();
  });

  it("returns null when cpu_pct is a string", () => {
    expect(parseFrame(validRaw({ cpu_pct: "50" }))).toBeNull();
  });

  it("returns null when ts is a string", () => {
    expect(parseFrame(validRaw({ ts: "2024-01-01" }))).toBeNull();
  });

  it("is a pure function — same input always gives the same result", () => {
    const raw = validRaw({ state: "speaking", audio_level: 0.8 });
    const a = parseFrame(raw);
    const b = parseFrame(raw);
    expect(a).toEqual(b);
  });

  it("returns null for null input — does not throw", () => {
    // TypeScript callers can't pass null, but JS callers can.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(parseFrame(null as any)).toBeNull();
  });
});
