/**
 * Unit tests for stateVisual — the pure state→visual mapping function.
 *
 * These tests have zero React/Three.js dependencies and run in a plain
 * node environment via Vitest.
 */

import { describe, it, expect } from "vitest";
import { stateVisual } from "../src/renderer/visuals/stateVisual";
import type { AssistantState } from "../src/renderer/types";

const ALL_STATES: AssistantState[] = [
  "idle",
  "listening",
  "thinking",
  "speaking",
  "acting",
];

describe("stateVisual", () => {
  it("returns a result for every valid state", () => {
    for (const state of ALL_STATES) {
      const vis = stateVisual(state);
      expect(vis).toBeDefined();
    }
  });

  it("returns an object with all required keys", () => {
    const REQUIRED_KEYS = [
      "primaryColor",
      "accentColor",
      "coreOpacity",
      "ringSpeed",
      "pulsing",
      "spinningSegments",
      "audioReactivity",
      "label",
    ] as const;

    for (const state of ALL_STATES) {
      const vis = stateVisual(state);
      for (const key of REQUIRED_KEYS) {
        expect(vis).toHaveProperty(key);
      }
    }
  });

  it("primaryColor is a valid CSS hex string for every state", () => {
    const HEX_RE = /^#[0-9a-fA-F]{6}$/;
    for (const state of ALL_STATES) {
      const { primaryColor } = stateVisual(state);
      expect(primaryColor).toMatch(HEX_RE);
    }
  });

  it("accentColor is a valid CSS hex string for every state", () => {
    const HEX_RE = /^#[0-9a-fA-F]{6}$/;
    for (const state of ALL_STATES) {
      const { accentColor } = stateVisual(state);
      expect(accentColor).toMatch(HEX_RE);
    }
  });

  it("coreOpacity is between 0 and 1 for every state", () => {
    for (const state of ALL_STATES) {
      const { coreOpacity } = stateVisual(state);
      expect(coreOpacity).toBeGreaterThanOrEqual(0);
      expect(coreOpacity).toBeLessThanOrEqual(1);
    }
  });

  it("ringSpeed is non-negative for every state", () => {
    for (const state of ALL_STATES) {
      const { ringSpeed } = stateVisual(state);
      expect(ringSpeed).toBeGreaterThanOrEqual(0);
    }
  });

  it("audioReactivity is between 0 and 1 for every state", () => {
    for (const state of ALL_STATES) {
      const { audioReactivity } = stateVisual(state);
      expect(audioReactivity).toBeGreaterThanOrEqual(0);
      expect(audioReactivity).toBeLessThanOrEqual(1);
    }
  });

  it("pulsing is a boolean for every state", () => {
    for (const state of ALL_STATES) {
      const { pulsing } = stateVisual(state);
      expect(typeof pulsing).toBe("boolean");
    }
  });

  it("spinningSegments is a boolean for every state", () => {
    for (const state of ALL_STATES) {
      const { spinningSegments } = stateVisual(state);
      expect(typeof spinningSegments).toBe("boolean");
    }
  });

  it("label is a non-empty string for every state", () => {
    for (const state of ALL_STATES) {
      const { label } = stateVisual(state);
      expect(typeof label).toBe("string");
      expect(label.length).toBeGreaterThan(0);
    }
  });

  // Per-state semantics
  it("IDLE has lower opacity and no pulsing", () => {
    const idle = stateVisual("idle");
    const listening = stateVisual("listening");
    expect(idle.coreOpacity).toBeLessThan(listening.coreOpacity);
    expect(idle.pulsing).toBe(false);
    expect(idle.spinningSegments).toBe(false);
    expect(idle.audioReactivity).toBe(0);
  });

  it("LISTENING pulses and has some audio reactivity", () => {
    const vis = stateVisual("listening");
    expect(vis.pulsing).toBe(true);
    expect(vis.spinningSegments).toBe(false);
    expect(vis.audioReactivity).toBeGreaterThan(0);
  });

  it("THINKING has spinning segments", () => {
    const vis = stateVisual("thinking");
    expect(vis.spinningSegments).toBe(true);
    expect(vis.audioReactivity).toBe(0);
  });

  it("SPEAKING has full audio reactivity", () => {
    const vis = stateVisual("speaking");
    expect(vis.audioReactivity).toBe(1);
    expect(vis.pulsing).toBe(true);
    expect(vis.spinningSegments).toBe(false);
  });

  it("ACTING uses gold/warm accent and both pulsing + spinning", () => {
    const vis = stateVisual("acting");
    expect(vis.pulsing).toBe(true);
    expect(vis.spinningSegments).toBe(true);
    // Gold primary colour has high red component
    const r = parseInt(vis.primaryColor.slice(1, 3), 16);
    expect(r).toBeGreaterThan(200);
  });

  it("is a pure function — same state always returns equivalent result", () => {
    for (const state of ALL_STATES) {
      const a = stateVisual(state);
      const b = stateVisual(state);
      expect(a).toEqual(b);
    }
  });

  it("returns different visuals for different states", () => {
    const colors = ALL_STATES.map((s) => stateVisual(s).primaryColor);
    // At least some should be distinct (not all the same colour)
    const unique = new Set(colors);
    expect(unique.size).toBeGreaterThan(1);
  });

  it("label matches uppercase state name pattern", () => {
    for (const state of ALL_STATES) {
      const { label } = stateVisual(state);
      expect(label).toBe(label.toUpperCase());
    }
  });
});
