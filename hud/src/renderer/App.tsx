/**
 * App — root React component for the FRIDAY HUD renderer.
 *
 * Connects to the telemetry WS, shows a "waiting" overlay when disconnected,
 * and renders the Three.js HUD scene when connected/connecting.
 */

import { useEffect, useState } from "react";
import { useTelemetry } from "./hooks/useTelemetry";
import HudScene from "./components/HudScene";
import { TELEMETRY_WS_URL } from "./config";

function useWindowSize(): { width: number; height: number } {
  const [size, setSize] = useState({
    width: window.innerWidth,
    height: window.innerHeight,
  });

  useEffect(() => {
    const handler = () =>
      setSize({ width: window.innerWidth, height: window.innerHeight });
    window.addEventListener("resize", handler);
    return () => window.removeEventListener("resize", handler);
  }, []);

  return size;
}

export default function App() {
  const { frame, status } = useTelemetry(TELEMETRY_WS_URL);
  const { width, height } = useWindowSize();

  const isWaiting = status === "disconnected" || (status === "connecting" && frame === null);

  return (
    <div
      style={{
        width: "100vw",
        height: "100vh",
        overflow: "hidden",
        background: "transparent",
        position: "relative",
        fontFamily: "'Courier New', Courier, monospace",
      }}
    >
      {/* Three.js scene — always rendered (uses last good frame if disconnected) */}
      <HudScene frame={frame} width={width} height={height} />

      {/* Waiting overlay */}
      {isWaiting && (
        <div
          style={{
            position: "absolute",
            bottom: 40,
            left: 0,
            right: 0,
            textAlign: "center",
            color: "#00c8ff",
            fontSize: 13,
            opacity: 0.6,
            letterSpacing: "0.15em",
            userSelect: "none",
            pointerEvents: "none",
          }}
        >
          WAITING FOR FRIDAY
          {status === "connecting" && " — CONNECTING…"}
        </div>
      )}

      {/* Gauge labels — CPU / RAM */}
      {frame !== null && (
        <div
          style={{
            position: "absolute",
            bottom: 40,
            left: 0,
            right: 0,
            display: "flex",
            justifyContent: "center",
            gap: 40,
            color: "#00aaff",
            fontSize: 11,
            letterSpacing: "0.1em",
            userSelect: "none",
            pointerEvents: "none",
          }}
        >
          <span style={{ color: "#00aaff" }}>
            CPU {Math.round(frame.cpu_pct)}%
          </span>
          <span style={{ color: "#aa00ff" }}>
            RAM {Math.round(frame.ram_pct)}%
          </span>
        </div>
      )}
    </div>
  );
}
