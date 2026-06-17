# ADR-0003: The bundle is the contract (typed, versioned, file-centric)

Status: accepted · 2026-06-10

## Context

Every consumer — CLI, MCP server, future GUI, arbitrary agents with file tools, Obsidian — needs a stable way to read pipeline output. Pipelines that communicate through in-memory objects or databases couple consumers to the implementation and defeat resumability.

## Decision

All pipeline state and output lives in a **bundle**: a self-describing directory with a `manifest.json` carrying `schema_version`, source provenance, per-stage status, and artifact content hashes. Stages communicate *only* through bundle artifacts. Schema is defined once in `src/lectern/bundle.py` (strict pydantic models, JSON Schema export committed to `schemas/`); migrations are explicit (`lectern migrate`), and `schema_version` bumps follow semver discipline: additive = minor, breaking = major + migration.

## Consequences

- Agents need no SDK — file tools suffice; the MCP server is convenience, not gatekeeper.
- Idempotence, resume behavior, and crash safety fall out of hash-checked, file-centric stage state.
- Every analysis assertion carries a `[t=hh:mm:ss]` anchor; faithfulness is checkable (DESIGN §7).
- Accepted cost: schema discipline is ongoing work; treated as the project's most protected surface, with breaking changes requiring explicit human review.
