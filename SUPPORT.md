# Support And Compatibility

Lectern is pre-release software. Support currently targets the public `main`
branch and the current documented preview workflow.

## Supported Environment

- Python 3.12 or newer.
- `uv` for environment and command execution.
- `ffmpeg` for media normalization and `lectern doctor`.
- macOS and Linux are expected development targets. CI currently runs the
  verification matrix on `ubuntu-latest` and `macos-latest` with Python 3.12.

## Version Policy

The package version and bundle manifest schema version are separate.

- The package version describes the installed Lectern software.
- The manifest `schema_version` describes the bundle manifest contract.

The current package version is `0.0.1`. The current manifest schema version is
`0.1.0`.

Before a stable release, CLI flags and command output may change. Bundle schema
changes are treated more carefully: additive bundle manifest changes should
raise the manifest schema minor version, and breaking changes require a major
schema version plus migration notes.

## Security Fixes

Until versioned releases exist, security fixes target the current public `main`
branch. Do not include private media, transcripts, credentials, generated
bundles, or local state databases in public reports.

## Unsupported Workflows

The current preview does not support:

- package-registry installation;
- external source discovery;
- external media acquisition;
- MCP/API access;
- OCR or visual evidence extraction;
- remote model stages;
- a GUI or watch daemon;
- transcript faithfulness guarantees for arbitrary media.
