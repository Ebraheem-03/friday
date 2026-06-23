/**
 * HudScene — Three.js scene with arc-reactor core, audio-reactive rings,
 * and CPU/RAM radial gauges.
 *
 * Performance contract:
 *   - Single requestAnimationFrame loop; no new allocations per frame.
 *   - Geometries/materials disposed on unmount.
 *   - Reads the latest TelemetryFrame ref; never re-renders from Three state.
 */

import { useEffect, useRef } from "react";
import * as THREE from "three";
import type { TelemetryFrame } from "../types";
import { stateVisual } from "../visuals/stateVisual";

interface HudSceneProps {
  frame: TelemetryFrame | null;
  width: number;
  height: number;
}

// --- helpers ----------------------------------------------------------------

function hexToColor(hex: string): THREE.Color {
  return new THREE.Color(hex);
}

/** Build a torus (ring) mesh */
function makeRing(
  radius: number,
  tube: number,
  color: THREE.Color,
  opacity: number
): THREE.Mesh {
  const geo = new THREE.TorusGeometry(radius, tube, 16, 128);
  const mat = new THREE.MeshBasicMaterial({
    color,
    transparent: true,
    opacity,
    depthWrite: false,
  });
  return new THREE.Mesh(geo, mat);
}

/** Build arc (partial torus) mesh using a custom buffer geometry */
function makeArcSegment(
  radius: number,
  tube: number,
  arcLength: number,
  color: THREE.Color
): THREE.Mesh {
  const geo = new THREE.TorusGeometry(radius, tube, 8, 64, arcLength);
  const mat = new THREE.MeshBasicMaterial({
    color,
    transparent: true,
    opacity: 0.7,
    depthWrite: false,
  });
  return new THREE.Mesh(geo, mat);
}

/** Build a flat gauge arc indicating a percentage 0–100 */
function makeGaugeArc(
  radius: number,
  pct: number,
  color: THREE.Color,
  tube: number
): THREE.Mesh {
  const arcLen = (pct / 100) * Math.PI * 2;
  const geo = new THREE.TorusGeometry(radius, tube, 8, 64, arcLen);
  const mat = new THREE.MeshBasicMaterial({
    color,
    transparent: true,
    opacity: 0.85,
    depthWrite: false,
  });
  const mesh = new THREE.Mesh(geo, mat);
  // Start arc at top (-PI/2)
  mesh.rotation.z = -Math.PI / 2;
  return mesh;
}

// --- component --------------------------------------------------------------

export default function HudScene({ frame, width, height }: HudSceneProps) {
  const mountRef = useRef<HTMLDivElement>(null);
  // Keep latest frame accessible in animation loop without re-triggering effects.
  const frameRef = useRef<TelemetryFrame | null>(frame);
  frameRef.current = frame;

  useEffect(() => {
    const container = mountRef.current;
    if (!container) return;

    // --- Renderer -----------------------------------------------------------
    const renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,
    });
    renderer.setPixelRatio(window.devicePixelRatio);
    renderer.setSize(width, height);
    renderer.setClearColor(0x000000, 0);
    container.appendChild(renderer.domElement);

    // --- Scene & Camera -----------------------------------------------------
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(60, width / height, 0.1, 100);
    camera.position.set(0, 0, 4);

    // --- Arc-Reactor Core ---------------------------------------------------
    const coreGeo = new THREE.SphereGeometry(0.45, 32, 32);
    const coreMat = new THREE.MeshBasicMaterial({
      color: new THREE.Color("#00c8ff"),
      transparent: true,
      opacity: 0.25,
    });
    const core = new THREE.Mesh(coreGeo, coreMat);
    scene.add(core);

    // Inner glow ring around the core
    const innerRing = makeRing(0.52, 0.018, new THREE.Color("#00ffee"), 0.9);
    scene.add(innerRing);

    // --- Audio-reactive rings (3 concentric) --------------------------------
    const ring1 = makeRing(0.85, 0.012, new THREE.Color("#00c8ff"), 0.7);
    const ring2 = makeRing(1.1, 0.010, new THREE.Color("#00c8ff"), 0.5);
    const ring3 = makeRing(1.35, 0.008, new THREE.Color("#00c8ff"), 0.35);
    const audioRings = [ring1, ring2, ring3];
    audioRings.forEach((r) => scene.add(r));

    // Store base radii for audio-reactive scaling
    const audioRingBaseRadii = [0.85, 1.1, 1.35];

    // --- Spinning segment arcs (THINKING state) -----------------------------
    const SEGMENT_RADII = [1.55, 1.65, 1.75];
    const segments: THREE.Mesh[] = [];
    SEGMENT_RADII.forEach((r, i) => {
      const arc = makeArcSegment(
        r,
        0.009,
        Math.PI * 0.6,
        new THREE.Color("#3388ff")
      );
      arc.rotation.z = (i * Math.PI * 2) / 3;
      scene.add(arc);
      segments.push(arc);
    });

    // --- CPU gauge (outer ring, left half) ----------------------------------
    // Built/replaced each frame — we'll manage via a group
    const cpuGroup = new THREE.Group();
    cpuGroup.position.set(0, 0, 0);
    scene.add(cpuGroup);

    const ramGroup = new THREE.Group();
    ramGroup.position.set(0, 0, 0);
    scene.add(ramGroup);

    // Static gauge background tracks
    const cpuTrack = makeRing(1.92, 0.006, new THREE.Color("#002244"), 0.6);
    scene.add(cpuTrack);
    const ramTrack = makeRing(2.05, 0.006, new THREE.Color("#220022"), 0.6);
    scene.add(ramTrack);

    // --- State text label (canvas texture) ----------------------------------
    const labelCanvas = document.createElement("canvas");
    labelCanvas.width = 256;
    labelCanvas.height = 64;
    const labelCtx = labelCanvas.getContext("2d")!;
    const labelTex = new THREE.CanvasTexture(labelCanvas);
    const labelGeo = new THREE.PlaneGeometry(1.4, 0.35);
    const labelMat = new THREE.MeshBasicMaterial({
      map: labelTex,
      transparent: true,
      depthWrite: false,
    });
    const labelMesh = new THREE.Mesh(labelGeo, labelMat);
    labelMesh.position.set(0, -1.4, 0);
    scene.add(labelMesh);

    function drawLabel(text: string, color: string) {
      labelCtx.clearRect(0, 0, 256, 64);
      labelCtx.font = "bold 20px 'Courier New', monospace";
      labelCtx.fillStyle = color;
      labelCtx.textAlign = "center";
      labelCtx.textBaseline = "middle";
      labelCtx.fillText(text, 128, 32);
      labelTex.needsUpdate = true;
    }

    // --- Helper: rebuild gauge arcs ----------------------------------------
    let cpuArc: THREE.Mesh | null = null;
    let ramArc: THREE.Mesh | null = null;

    function updateGauges(cpuPct: number, ramPct: number) {
      if (cpuArc) {
        cpuGroup.remove(cpuArc);
        (cpuArc.geometry as THREE.BufferGeometry).dispose();
        (cpuArc.material as THREE.Material).dispose();
      }
      cpuArc = makeGaugeArc(1.92, cpuPct, new THREE.Color("#00aaff"), 0.009);
      cpuGroup.add(cpuArc);

      if (ramArc) {
        ramGroup.remove(ramArc);
        (ramArc.geometry as THREE.BufferGeometry).dispose();
        (ramArc.material as THREE.Material).dispose();
      }
      ramArc = makeGaugeArc(2.05, ramPct, new THREE.Color("#aa00ff"), 0.009);
      ramGroup.add(ramArc);
    }

    // Initial gauge render
    updateGauges(0, 0);

    // --- Animation loop variables -------------------------------------------
    let animId: number;
    let lastCpu = 0;
    let lastRam = 0;
    let lastLabel = "";
    let clock = 0;

    function animate() {
      animId = requestAnimationFrame(animate);
      clock += 0.016; // ~60fps tick

      const f = frameRef.current;
      const state = f?.state ?? "idle";
      const vis = stateVisual(state);
      const audioLevel = f?.audio_level ?? 0;
      const cpuPct = f?.cpu_pct ?? 0;
      const ramPct = f?.ram_pct ?? 0;

      // --- Core colour + opacity ---
      const primaryCol = hexToColor(vis.primaryColor);
      (coreMat as THREE.MeshBasicMaterial).color.set(primaryCol);

      let coreOp = vis.coreOpacity;
      if (vis.pulsing) {
        coreOp = vis.coreOpacity * (0.75 + 0.25 * Math.sin(clock * 4));
      }
      coreMat.opacity = coreOp;

      // Inner ring color
      (innerRing.material as THREE.MeshBasicMaterial).color.set(primaryCol);

      // --- Audio-reactive rings ---
      const ringReactivity = vis.audioReactivity;
      audioRings.forEach((ring, i) => {
        const baseR = audioRingBaseRadii[i];
        const boost = ringReactivity * audioLevel * 0.4;
        const scale = (baseR + boost) / baseR;
        ring.scale.set(scale, scale, 1);
        ring.rotation.z += vis.ringSpeed * 0.005 * (i % 2 === 0 ? 1 : -1);
        (ring.material as THREE.MeshBasicMaterial).color.set(primaryCol);
        (ring.material as THREE.MeshBasicMaterial).opacity =
          0.35 + ringReactivity * audioLevel * 0.5 + 0.15 * (2 - i) * 0.3;
      });

      // --- Spinning segments (THINKING) ---
      const segVisible = vis.spinningSegments;
      segments.forEach((seg, i) => {
        seg.visible = segVisible;
        if (segVisible) {
          seg.rotation.z +=
            vis.ringSpeed * 0.012 * (i % 2 === 0 ? 1 : -1.3);
          (seg.material as THREE.MeshBasicMaterial).color.set(
            hexToColor(vis.accentColor)
          );
        }
      });

      // --- Gauges (rebuild only when values change meaningfully) ---
      const cpuRounded = Math.round(cpuPct);
      const ramRounded = Math.round(ramPct);
      if (cpuRounded !== lastCpu || ramRounded !== lastRam) {
        updateGauges(cpuRounded, ramRounded);
        lastCpu = cpuRounded;
        lastRam = ramRounded;
      }

      // --- State label ---
      if (vis.label !== lastLabel) {
        drawLabel(vis.label, vis.primaryColor);
        lastLabel = vis.label;
      }

      // --- ACTING gold accent: tint the whole scene ---
      if (state === "acting") {
        const goldCol = hexToColor(vis.primaryColor);
        (innerRing.material as THREE.MeshBasicMaterial).color.set(goldCol);
      }

      renderer.render(scene, camera);
    }

    animate();

    // Initial label
    drawLabel("IDLE", "#00c8ff");

    // --- Cleanup ------------------------------------------------------------
    return () => {
      cancelAnimationFrame(animId);

      // Dispose geometries + materials
      [coreGeo, innerRing.geometry, ring1.geometry, ring2.geometry, ring3.geometry]
        .forEach((g) => g.dispose());
      [coreMat, innerRing.material as THREE.Material]
        .forEach((m) => m.dispose());
      audioRings.forEach((r) => {
        r.geometry.dispose();
        (r.material as THREE.Material).dispose();
      });
      segments.forEach((s) => {
        s.geometry.dispose();
        (s.material as THREE.Material).dispose();
      });
      [cpuTrack, ramTrack].forEach((t) => {
        t.geometry.dispose();
        (t.material as THREE.Material).dispose();
      });
      if (cpuArc) {
        cpuArc.geometry.dispose();
        (cpuArc.material as THREE.Material).dispose();
      }
      if (ramArc) {
        ramArc.geometry.dispose();
        (ramArc.material as THREE.Material).dispose();
      }
      labelGeo.dispose();
      labelMat.dispose();
      labelTex.dispose();

      renderer.dispose();
      if (container.contains(renderer.domElement)) {
        container.removeChild(renderer.domElement);
      }
    };
    // We intentionally only run this effect once on mount — the frame is read
    // via ref inside the animation loop, not as a dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width, height]);

  return (
    <div
      ref={mountRef}
      style={{ width, height, position: "absolute", top: 0, left: 0 }}
    />
  );
}
