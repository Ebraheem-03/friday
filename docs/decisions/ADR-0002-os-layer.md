# ADR-0002: OS-control layer on Linux

**Status:** Accepted

## Context
The source blueprint assumed Windows-MCP. We are on Linux, where windows-mcp/macos-mcp do not run.

## Decision
Build-time: use `@agent-sh/computer-use-linux` (AT-SPI + ydotool, Wayland-first) to prototype. Runtime: native Python `os_bridge.py` using AT-SPI + ydotool/xdotool with display-server auto-detection. (KDE alternative: kwin-mcp.)

## Consequences
Window targeting depends on desktop environment; must confirm X11/Wayland + DE. All destructive actions gated behind confirm/dry-run.
