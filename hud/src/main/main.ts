/**
 * Electron main process — FRIDAY HUD window.
 *
 * Security hardening (CLAUDE.md §3 / ADR-0007):
 *   - contextIsolation: true
 *   - nodeIntegration: false
 *   - sandbox: true
 *   - No @electron/remote
 *   - Preload exposes nothing privileged
 *   - No remote/CDN resources — everything is bundled locally
 *   - Renderer's only network access is the localhost telemetry WS
 *   - Restrictive CSP set in index.html and enforced via webRequest
 */

import { app, BrowserWindow, session } from "electron";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// The renderer build output directory (after `vite build`)
const RENDERER_DIST = path.join(__dirname, "../../dist");
// In dev mode (vite dev server not used in our workflow; we build first)
const RENDERER_INDEX = path.join(RENDERER_DIST, "index.html");

function createWindow(): void {
  const win = new BrowserWindow({
    width: 480,
    height: 480,
    // Borderless transparent always-on-top
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: true,
    hasShadow: false,
    // Position: top-right corner (adjust to taste)
    x: 10,
    y: 10,
    webPreferences: {
      // Security hardening
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      // Preload bridge (currently exposes nothing privileged)
      preload: path.join(__dirname, "../preload/preload.js"),
      // Disable navigation to external URLs
      navigateOnDragDrop: false,
      // No devtools in production
      devTools: !app.isPackaged ? true : false,
    },
    show: false, // avoid flash of unstyled content
    skipTaskbar: true,
    title: "FRIDAY HUD",
    backgroundColor: "#00000000",
  });

  // Apply restrictive CSP at the webRequest level as a belt-and-suspenders
  // measure on top of the meta CSP in index.html.
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        "Content-Security-Policy": [
          "default-src 'self'; " +
            "script-src 'self'; " +
            "style-src 'self' 'unsafe-inline'; " +
            "connect-src 'self' ws://127.0.0.1:*; " +
            "img-src 'self' data:; " +
            "font-src 'self';",
        ],
      },
    });
  });

  // Prevent navigation away from the local renderer
  win.webContents.on("will-navigate", (event, url) => {
    const parsedUrl = new URL(url);
    // Allow only file:// protocol
    if (parsedUrl.protocol !== "file:") {
      event.preventDefault();
    }
  });

  // Block new window creation (no pop-ups, no external links)
  win.webContents.setWindowOpenHandler(() => {
    return { action: "deny" };
  });

  // Show window gracefully once content is ready
  win.once("ready-to-show", () => {
    win.show();
  });

  void win.loadFile(RENDERER_INDEX);
}

app.whenReady().then(() => {
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
