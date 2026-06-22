# ADR-0006: Persistent memory

**Status:** Accepted

## Context
FRIDAY must remember preferences/paths/projects across restarts with zero-latency local recall.

## Decision
Local SQLite (WAL) knowledge-graph: entities, observations, relations, exposed via a synchronous recall API the brain calls. The `server-memory` MCP is a shape reference only, not a runtime dependency.

## Consequences
Fast, offline, portable. Schema migrations owned by mnemo.
