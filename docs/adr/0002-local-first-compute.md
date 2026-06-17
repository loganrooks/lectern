# ADR-0002: Local-first compute, per-stage API opt-in, privacy hard rule

Status: accepted - 2026-06-10

## Context

Lectern processes both public media and private local recordings. Cloud ASR,
LLM, and VLM services can improve quality for some stages, but they add cost and
move media-derived artifacts off the user's machine.

## Decision

1. Deterministic media stages, transcription, scene detection, OCR, and
   deduplication run locally by default.
2. Intelligence stages such as visual descriptions, reference enrichment, and
   synthesis may use remote APIs only when the stage is explicitly configured to
   do so.
3. Remote API stages must honor per-item budget caps.
4. Artifacts derived from local-source media must not reach a remote service
   without explicit per-item consent.

## Consequences

- Private recordings remain safe to process by default.
- Local models and deterministic tools are first-class dependencies, not
  fallbacks.
- Some high-quality descriptions or summaries may require explicit opt-in to
  paid remote services.
- The test suite must preserve the local-source privacy rule as a load-bearing
  invariant.
