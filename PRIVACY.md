# Privacy

Lectern is local-first. The default posture is that local-source media and
media-derived artifacts stay on the user's machine.

## Local-Source Rule

Artifacts derived from a local source must not be sent to a remote service
unless the user explicitly opts in for that specific item and stage.

The current local ingest and automation paths do not call remote transcription
providers. Tests also exercise the local-source privacy boundary by failing if
Lectern opens a network socket during the local command transcription path.

## User-Supplied Commands

Lectern can run a user-configured local JSON transcriber command. That command
runs on the user's machine with the user's privileges.

Lectern rejects obvious remote endpoint URLs in the configured command and
records that Lectern itself did not invoke a remote service. It cannot prove that
an arbitrary executable never opens a network connection internally. Use only
transcriber commands you trust for the media being processed.

## Local Artifacts

Generated bundles, local state databases, caches, and media-derived outputs can
contain sensitive information. They are local run output and should not be
committed, attached to issues, or shared unless the user intentionally chooses to
share them.

The public repository uses synthetic redistributable test fixtures. Do not add
private recordings, private transcripts, copyrighted media, or third-party
transcripts as fixtures.

## Future Remote Stages

Future stages may support remote APIs for tasks such as reference enrichment,
visual descriptions, or synthesis. Those stages must be opt-in, scoped to the
specific item and stage, and budget-aware before they are used on local-source
artifacts.
