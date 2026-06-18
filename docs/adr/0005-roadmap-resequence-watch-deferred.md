# ADR-0005: Roadmap Resequenced; Watch Daemon Deferred

Status: accepted
Date: 2026-06-18

## Context

ADR-0004 accepted a CLI-first v1 surface that included `lectern`, `lectern watch`, and `lectern mcp`, with GUI deferred. Subsequent requirements work and external review found that the old M2 plan, "YouTube adapter + playlist watcher," put source surface area ahead of the binding product constraint: producing one useful local bundle from user-provided media without a transcript sidecar.

The current roadmap now prioritizes:

1. local automation spine;
2. local transcription / first useful non-fixture workflow;
3. public preview gate;
4. external source discovery;
5. retrieval and agent surfaces;
6. visual evidence;
7. situated synthesis;
8. packaging and release hardening.

## Decision

`lectern watch` is deferred beyond the current M0-M8 roadmap. The current automation path should be command-friendly: explicit source scans, queue operations, retry/status commands, and scheduler-compatible CLI behavior.

`lectern mcp` remains in scope, but moves to the retrieval and agent-surface milestone after local automation and useful local transcription exist.

YouTube playlist support starts as metadata discovery through the source registry and queue. Remote media acquisition-to-bundle is not implied by metadata discovery; it must be explicitly scoped in a later milestone.

## Consequences

- ADR-0004 remains correct that GUI work is deferred and the CLI is first.
- ADR-0004 is narrowed for v1 surface timing: `lectern watch` is no longer required before the first useful preview or the current M0-M8 roadmap completes.
- The roadmap favors deterministic local workflows before background daemon semantics.
- A future watch daemon can be reintroduced when source/queue/state behavior is stable and there is evidence that always-on watching is more valuable than explicit scan/poll workflows.
