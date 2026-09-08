# Roadmap

Lectern is moving toward a local-first recorded-knowledge workbench. The public
tracker organizes a planning horizon from M5 through M13, followed by an
explicit Version 1.0 decision gate. Milestone placement is not a release
schedule, a delivery date, or a promise that a proposed interface will become a
stable API. Work can be deferred, split, reordered, or rescoped as evidence and
constraints change.

A milestone named `Gate` records a decision or an explicit deferral. Passing an
earlier milestone does not authorize acquisition, remote processing,
publication, or a later release automatically.

## Current Preview At M5a Closure

At the M5a closure point, the public preview remains a CLI-first local workflow.
Its bounded surface includes:

- Bundle manifests and strict JSON artifact schemas, with a documented migration
  from manifest `0.1.0` to `1.0.0`.
- The CLI entry point, `doctor`, and the synthetic local ingest fixture.
- An optional local JSON transcriber command for media without a transcript
  sidecar.
- Local folder source registration, discovery and review queues, a local SQLite
  state store, and library `list`/`show` commands with status information.
- Literal local transcript search, content-bound citation anchors, and
  reconciliation against changed or missing bundle evidence.
- Metadata-only public YouTube playlist discovery through the local source
  registry and review queue.
- Public safety checks and CI verification.

This summary does not expand the support boundary. The current package is still
pre-release; the README and support policy remain authoritative for supported
commands, compatibility, and known limits.

## Public Tracker Horizon

The milestone names below are the public planning vocabulary. Their descriptions
state intended boundaries, not guaranteed future behavior.

### M5 — Local Evidence And Review Surfaces

- **M5a — Local Evidence Retrieval** — carry local search, citations, schema,
  status, migration, and evidence-sampler work.
- **M5b — Experimental Read Surface** — explore privacy-preserving,
  read-oriented MCP/API access over the M5a record shape. Any interface remains
  experimental unless a later compatibility decision says otherwise.
- **M5c — Policy-Gated Local Enqueue** — add explicitly authorized local enqueue
  without bypassing source policy or legal queue transitions.
- **M5d — Companion Workbench Alpha** — build an experimental local review,
  player, and annotation surface over local evidence.
- **M5e — Repair While Reading** — add provenance-preserving, user-authored
  transcript corrections without silently changing shared contracts.
- **Gate — Caption and Media Acquisition** — record separate caption and media
  acquisition decisions or explicit deferrals. The gate itself adds no
  acquisition implementation and does not imply that acquisition will follow.

### M6 — Visual Evidence

- **M6a — Deterministic Local Visual Evidence** — produce reproducible frames,
  OCR, provenance, and companion evidence locally.
- **M6b — Consented Remote Visual Description** — consider opt-in remote visual
  description only with per-item consent and budget controls.

### M7 — Situated Synthesis And References

- **M7a — Citation-Gated Local Synthesis** — produce anchored synthesis that
  marks unsupported claims and preserves supported disagreement.
- **M7b — External Reference Calibration** — calibrate live reference resolution
  as separately labelled, non-blocking evidence rather than silently treating
  it as local bundle evidence.

### M8 — Preview Hardening

- **M8 — Bounded Preview Release Engineering** — harden packaging,
  compatibility, migration, accessibility, support, and preview evidence. This
  milestone does not authorize package publication or a wider release.

### M9 — Continuity, Recovery, And Explicit Capture

- **M9a — Session and Revision Continuity** — add ordered events and
  non-destructive revision history with an explicit state-migration path.
- **M9b — Fragmented-Note Recovery** — import fragmented notes with provenance,
  conflict visibility, idempotence, and local export.
- **M9c — Explicit Local Capture** — add policy-gated drop-folder or import
  capture with retention and recorded-subject safeguards. It does not imply an
  always-on capture service or watch daemon.

### M10–M13 — Inquiry And Ecosystem Horizons

- **M10 — Live Thinking Companion** — link bounded live context, marks,
  questions, provisional answers, and later offline refinement.
- **M11 — Domain Calibration** — add scoped vocabulary and entity hints without
  making a general transcription-quality guarantee.
- **M12 — Corpus Inquiry** — provide auditable discovery, review, processing,
  comparison, and coverage accounting over a provider seam. Provider access and
  use remain conditional on separately documented capabilities and constraints.
- **M13 — Participatory Ecosystem** — document extension surfaces and
  appropriation/export paths with compatibility and deprecation boundaries.

### Version Decision

- **Gate — Version 1.0** — assemble evidence for wider reliance and record an
  explicit decision after the post-M13 contract is represented. The gate has no
  date and does not make publication, compatibility, or a Version 1.0 release
  automatic.

## Conditions That Remain Separate

- Provider-backed work depends on available access, terms, privacy posture,
  consent, and budget. It remains optional or non-blocking unless a milestone
  explicitly establishes a different boundary.
- Caption and media acquisition require separate decisions; metadata discovery
  is not permission or implementation for acquisition.
- Capture work requires explicit policy, retention, and recorded-subject
  safeguards. The roadmap does not promise ambient or always-on capture.
- Compatibility claims require represented contracts, versioning, migration
  evidence, and explicit support decisions. A future interface is not stable
  merely because it appears in the tracker.
- Packaging and release hardening do not publish anything. Publication or a
  wider release requires a separate, explicit decision.

## Non-Goals For The Current Preview

- No package-registry release.
- No launch-style public promotion.
- No GUI.
- No watch daemon or always-on capture.
- No YouTube media, caption, or transcript download workflow.
- No claim that transcripts are faithful for arbitrary media.
- No guarantee that user-supplied transcriber commands are network-free.
