# ADR-0008: Security posture

**Status:** Accepted

## Context
FRIDAY controls the OS, drives a browser, and streams context to a cloud brain — a real attack surface.

## Decision
Pin and verify every dependency against its official source; ban look-alikes (e.g. the malicious unscoped `playwright-mcp`). Confirm/dry-run gate on destructive OS actions. No secrets or full screen captures logged by default. `sentinel` reviews shell/filesystem/network code before merge.

## Consequences
Slower to add dependencies; materially safer. Gates are mandatory, not optional.
