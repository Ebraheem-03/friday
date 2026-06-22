# ADR-0001: Runtime stack

**Status:** Accepted

## Context
FRIDAY needs a low-latency voice assistant with a capable reasoning brain, on Linux.

## Decision
Brain = Gemini (streaming/Live). Voice = Kokoro-82M TTS + sounddevice capture. Web = browser-use + Scrapling. Memory = local SQLite. Orchestration = async Python. HUD = Electron + React + Three.js.

## Consequences
Cloud brain implies network dependency and a privacy/security surface (see ADR-0008). Everything else runs locally.
