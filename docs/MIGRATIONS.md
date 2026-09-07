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
versions, and unrelated occupied backup/staging roles before overwriting them. Its
JSON and error output do not echo filesystem paths. Re-running it on a valid
`1.0.0` bundle is a checked no-op.

Legacy JSON extension fields that are not declared by Lectern's artifact
models are refused rather than silently discarded. The original `0.1.0`
bundle remains unchanged so an operator can inspect or export that extension
data before retrying with a supported artifact shape.

Every transcript segment must contain at least one non-whitespace character,
using Python's Unicode whitespace definition. Migration refuses empty or
whitespace-only segments and preserves the original bundle and unrelated sibling
directories on refusal. Recovery may discard and rebuild obsolete staging that
Lectern identifies as its own, even if the retry is refused. It does not trim
text, remove or renumber segments, or invent
replacement evidence. Valid segment text and the segment artifact's bytes are
preserved exactly, including surrounding whitespace and internal line breaks.

A successful migration or already-current no-op requires manifest declarations
for both `source.json` and the transcript segments selected by that document.
Every declaration of either artifact must match its actual bytes and SHA-256
digest. Missing declarations are refused; migration does not invent them.
Declared alternate segment paths remain supported. Library readiness, indexing,
and new citations use this same selected-evidence requirement. Existing-anchor
diagnostics can still report changed evidence without minting a new citation.

To roll back while Lectern is stopped, move the `1.0.0` directory aside and
rename `BUNDLE.v0.1.0.bak` to the original bundle name. Do not merge files from
the two schema versions.
