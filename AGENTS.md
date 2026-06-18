# AGENTS.md

Public guidance for automated contributors and reviewers working in this
repository.

## Scope

- These instructions apply to the whole repository.
- Follow `CONTRIBUTING.md`, `SECURITY.md`, and direct maintainer instructions.
- Keep changes small, scoped, and reviewable.
- Do not overwrite unrelated local or contributor work.

## Project Shape

- Lectern is a Python 3.12+ project managed with `uv`.
- The stable verification entrypoint is `make verify`.
- The project is local-first: media artifacts from local sources must not be
  sent to remote services without explicit per-item consent.
- Test fixtures must be synthetic and redistributable.

## Public Safety

Do not commit:

- generated media bundles or local run output;
- caches, virtual environments, editor files, or machine-local artifacts;
- raw model prompts, model review reports, or local operator notes;
- credentials, tokens, API keys, private recordings, or private media;
- copyrighted media fixtures or third-party transcripts.

Run `make public-check` when touching docs, workflows, fixtures, repository
metadata, or anything that affects publication safety.

`make public-check` scans tracked files, modified tracked worktree files, and
untracked non-ignored files. It does not read ignored local-only content, but it
does check that local-only artifact paths remain ignored and untracked.

## Testing

- Run `make verify` before opening a pull request.
- For workflow changes, also run `actionlint .github/workflows/ci.yml` when
  available.
- Unit tests must not require network access. Mark network-dependent tests as
  `integration`.
- State which checks were run and any known limits in the pull request.

## Review Guidance

- Treat automated review comments as signals to investigate, not as facts to
  accept blindly.
- Prefer fixes that address the demonstrated issue without broad unrelated
  rewrites.
- Watch for second-order effects in safety checks, workflow gates, privacy
  boundaries, fixture policy, and public documentation.
- Do not require access to local-only planning notes to understand or review a
  public pull request.
