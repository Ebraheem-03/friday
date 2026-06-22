/**
 * Electron preload — context bridge.
 *
 * Security posture: this preload exposes NOTHING privileged to the renderer.
 * The renderer only needs to open a WebSocket to localhost, which it can do
 * natively via the browser WebSocket API without any Node.js access.
 *
 * If future features require IPC (e.g. drag-to-move the frameless window),
 * add a minimal contextBridge.exposeInMainWorld entry here — never grant
 * full Node/Electron access.
 *
 * contextIsolation: true and sandbox: true are set in main.ts — this file
 * runs in the isolated context where `require` is available but NOT exposed
 * to the page's JS.
 */

// Intentionally empty: no privileged surface exposed.
// The renderer communicates only via its native WebSocket API.
