# ADR-0004: Build-time vs runtime separation

**Status:** Accepted

## Context
The blueprint blurred dev-time MCP servers with the shipped app's libraries.

## Decision
MCP servers (codebase-memory, playwright, context7, and opt-ins) are agent tools used only while building. The product calls plain Python libraries directly. No MCP server ships in the runtime.

## Consequences
Clean boundary; smaller runtime footprint; no MCP runtime dependency. Agents must respect the line on every change.
