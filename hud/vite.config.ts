/**
 * Vite configuration for the FRIDAY HUD renderer.
 *
 * This config builds ONLY the renderer (React + Three.js).
 * The Electron main process is NOT bundled here — it is compiled separately
 * by tsc (tsconfig.node.json) when packaging the app.
 *
 * In CI: ELECTRON_SKIP_BINARY_DOWNLOAD=1 means `npm ci` skips the ~100 MB
 * Electron binary.  `npm run build` here only invokes `vite build` of the
 * renderer, which does not require the Electron binary.
 */

/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  root: ".",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, "index.html"),
    },
    // Target modern Chromium (Electron uses a pinned Chromium — ES2022 is safe)
    target: "chrome120",
    sourcemap: false,
    minify: true,
  },
  resolve: {
    alias: {
      "@renderer": path.resolve(__dirname, "src/renderer"),
    },
  },
  // Dev server (only used locally, not in CI)
  server: {
    port: 5173,
    strictPort: true,
  },
  // Vitest config — inline so we don't need a separate vitest.config.ts
  test: {
    // Pure-function tests (stateVisual, parseFrame) have no DOM dependencies.
    // Using "node" environment avoids the optional jsdom peer dependency.
    environment: "node",
    globals: true,
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
  },
});
