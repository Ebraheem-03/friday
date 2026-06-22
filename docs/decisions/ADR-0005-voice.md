# ADR-0005: Voice pipeline & latency

**Status:** Accepted

## Context
Conversational pauses kill the experience; full-response TTS is too slow.

## Decision
Stream the Gemini token output into a continuous synthesis queue, chunked to 24 kHz PCM so speech starts before the reply completes. Barge-in interrupts TTS when the user speaks.

## Consequences
Sub-second first-audio target; more complex buffering, documented per module.
