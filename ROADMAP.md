# Roadmap

Lectern is moving toward a local-first recorded-knowledge workbench. The near
term priority is to make one useful local workflow reliable before adding
external source discovery and richer analysis.

## Current Status

Completed public surface:

- Bundle manifest and schema scaffold.
- CLI entry point and `doctor` command.
- Synthetic local ingest fixture.
- Local folder source registry, discovery queue, SQLite state store, and minimal
  library list/show commands.
- Optional local JSON transcriber command for media without a transcript
  sidecar.
- Timestamped transcript artifacts, method metadata, and summary anchors.
- Metadata-only public YouTube playlist discovery through the local source
  registry and review queue.
- Public safety checks and CI verification.

## Next Work

1. Preview readiness
   - Keep the README, privacy docs, roadmap, changelog, support policy, and
     quickstart aligned with the current local workflow.
   - Verify clean install from the public repository.

2. External source follow-through
   - Keep YouTube discovery metadata-only until media acquisition is explicitly
     scoped.
   - Keep public-playlist, no-OAuth discovery as the default posture.
   - Keep network-dependent checks marked as integration tests.
   - Decide separately whether external media or caption acquisition belongs in
     a later milestone.

3. Library retrieval and agent surface
   - Improve library list/show/search.
   - Add read-oriented MCP/API access over local bundle evidence.
   - Add export and citation commands over bundle-local artifacts.

4. Visual evidence
   - Add synthetic slide fixtures.
   - Preserve representative frames and OCR references.
   - Keep optional visual-model use behind explicit consent and budget caps.

5. Situated synthesis and references
   - Gate support-requiring claims on anchors or resolvable references.
   - Mark or omit unsupported claims.
   - Add scoped faithfulness sampling.

6. Release hardening
   - Verify clean install paths.
   - Document supported and unsupported workflows.
   - Decide whether package-registry publishing is justified.
   - Keep optional extras graceful when dependencies are absent.

## Non-Goals For The Current Preview

- No package-registry release.
- No launch-style public promotion.
- No GUI.
- No watch daemon.
- No YouTube media, caption, or transcript download workflow.
- No claim that transcripts are faithful for arbitrary media.
- No guarantee that user-supplied transcriber commands are network-free.
