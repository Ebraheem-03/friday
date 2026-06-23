/**
 * stateVisual — pure, exported, unit-testable function.
 *
 * Maps an AssistantState to all visual parameters needed by the HUD scene.
 * No side-effects; no imports from React or Three.js (keeps test bundle small).
 */

import type { AssistantState } from "../types";

export interface StateVisual {
  /** Primary ring/core colour as a CSS hex string */
  primaryColor: string;
  /** Secondary/accent colour */
  accentColor: string;
  /**
   * Base opacity for the core sphere, 0–1.
   * IDLE is dim; LISTENING/SPEAKING are bright.
   */
  coreOpacity: number;
  /**
   * Ring rotation speed multiplier (arbitrary units; the renderer scales this
   * into radians/frame).  0 = no rotation.
   */
  ringSpeed: number;
  /**
   * Whether the core should pulse (uniform scale oscillation).
   */
  pulsing: boolean;
  /**
   * Whether outer segment arcs should rotate visibly (THINKING spinner).
   */
  spinningSegments: boolean;
  /**
   * Multiplier on audio_level's influence on ring radius.
   * SPEAKING = 1.0 (full reactivity); others = 0 or low.
   */
  audioReactivity: number;
  /**
   * Label shown in the HUD status bar.
   */
  label: string;
}

const VISUALS: Record<AssistantState, StateVisual> = {
  idle: {
    primaryColor: "#00c8ff", // dim cyan
    accentColor: "#004466",
    coreOpacity: 0.25,
    ringSpeed: 0.15,
    pulsing: false,
    spinningSegments: false,
    audioReactivity: 0.0,
    label: "IDLE",
  },
  listening: {
    primaryColor: "#00ffee", // bright cyan-teal
    accentColor: "#00aaaa",
    coreOpacity: 0.75,
    ringSpeed: 0.4,
    pulsing: true,
    spinningSegments: false,
    audioReactivity: 0.3,
    label: "LISTENING",
  },
  thinking: {
    primaryColor: "#3388ff", // cool blue
    accentColor: "#0044cc",
    coreOpacity: 0.55,
    ringSpeed: 0.6,
    pulsing: false,
    spinningSegments: true,
    audioReactivity: 0.0,
    label: "THINKING",
  },
  speaking: {
    primaryColor: "#00ffaa", // vivid cyan-green
    accentColor: "#00cc88",
    coreOpacity: 0.85,
    ringSpeed: 0.3,
    pulsing: true,
    spinningSegments: false,
    audioReactivity: 1.0,
    label: "SPEAKING",
  },
  acting: {
    primaryColor: "#ffd700", // gold alert
    accentColor: "#ff8800",
    coreOpacity: 0.9,
    ringSpeed: 0.8,
    pulsing: true,
    spinningSegments: true,
    audioReactivity: 0.2,
    label: "ACTING",
  },
};

/**
 * stateVisual(state) → StateVisual
 *
 * Pure mapping: AssistantState → visual parameters.
 * Exported for unit tests and for Three.js scene components.
 */
export function stateVisual(state: AssistantState): StateVisual {
  return VISUALS[state];
}
