# ADR-0003: Web automation

**Status:** Accepted

## Context
FRIDAY needs hands-free browsing, scraping, and form-filling.

## Decision
Build-time prototyping via the official `@playwright/mcp` (accessibility snapshots). Runtime ships `browser-use` + `Scrapling` natively — no MCP in the runtime path.

## Consequences
Accessibility-tree approach is more robust than pixel parsing; the unscoped `playwright-mcp` typosquat is explicitly banned (ADR-0008).
