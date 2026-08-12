# Bundle migrations

## Manifest `0.1.0` to `1.0.0`

Run `lectern migrate BUNDLE` while no other Lectern process is writing that
bundle. Lectern validates the complete source bundle, builds and validates a
sibling target, keeps the migrated bundle at `BUNDLE`, and retains the original
as `BUNDLE.v0.1.0.bak`. It does not delete the backup.

The major version changes because a local manifest's `source.ref` now denotes
`sha256:<digest>` rather than a filesystem path. The source digest and byte size
come from the bundle's existing `source.json`; media and transcript content are
not sent anywhere and are not regenerated.

The command refuses malformed or hash-invalid bundles, symlinks, unknown
versions, and occupied backup/staging roles before overwriting anything. Its
JSON and error output do not echo filesystem paths. Re-running it on a valid
`1.0.0` bundle is a checked no-op.

To roll back while Lectern is stopped, move the `1.0.0` directory aside and
rename `BUNDLE.v0.1.0.bak` to the original bundle name. Do not merge files from
the two schema versions.
