# ADR-0004: CLI First; GUI Deliberately Deferred

Status: accepted · 2026-06-10

Amended by: ADR-0005 (`lectern watch` daemon timing deferred beyond the current M0-M8 roadmap).

## Context

A polished Mac experience suggests a native app; portability suggests Electron/web. Choosing now would anchor architecture around the least-durable layer.

## Decision

v1 ships `lectern` (CLI) first, with `lectern mcp` following once the library and retrieval surfaces are stable. ADR-0005 defers `lectern watch` daemon timing beyond the current M0-M8 roadmap. No GUI. The durable assets are the pipeline and bundle format; any future GUI — Swift menubar app, web UI, or both — is a thin client over the bundle library + MCP server and can be chosen per platform later without architectural cost.

## Consequences

- The Electron-vs-Swift tradeoff is deferred until there's usage data and a stable contract to build on; nothing is foreclosed, including *different* answers per platform.
- v1 audience is necessarily technical (acceptable: that's also the contributor pool for a new public repo).
- Re-open when: M6 complete and a non-CLI audience materializes.
