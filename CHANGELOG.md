# Changelog

All notable public changes are recorded here. Lectern is pre-release software;
versioned release notes will become stricter once preview support stabilizes.

## Unreleased

- Added local command transcription support for media without a transcript
  sidecar.
- Added transcript method metadata, timestamped transcript artifacts, and
  timestamp-derived summary anchors.
- Added local automation spine commands for folder sources, queue operations,
  and minimal library inspection.
- Added public preview documentation for privacy, roadmap, support, and
  quickstart verification.

## 0.0.1

- Initial public package metadata, CLI scaffold, manifest schema, synthetic
  fixture ingest, public safety checks, and CI verification.

## Versioning Notes

The package version and bundle manifest schema version are separate.

- Package version: reports the installed Lectern software version.
- Manifest schema version: records the bundle manifest contract read by tools
  and agents.

The current package version is `0.0.1`. The current manifest schema version is
`0.1.0`.
