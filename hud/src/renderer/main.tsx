/**
 * Renderer entry point — React root mount.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error("Root element #root not found in HTML");
}

createRoot(rootEl).render(
  <StrictMode>
    <App />
  </StrictMode>
);
