# lectern

[![CI](https://github.com/loganrooks/lectern/actions/workflows/ci.yml/badge.svg)](https://github.com/loganrooks/lectern/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

**Turn talks into thought.** Lectern ingests recorded talks and produces local,
inspectable knowledge bundles for humans and agents.

Lectern is currently pre-release. The design is documented, the core bundle
schema and CLI scaffold exist, and the project is building toward a local-first
media pipeline.

## Design Shape

- Pipeline of pure stages: acquire, normalize, transcribe, diarize, visual,
  enrich, situate, synthesize.
- Durable bundle artifacts on disk, with explicit manifests and stage records.
- Local-first media processing with per-stage opt-in for remote APIs.
- Synthetic fixtures only; no copyrighted media fixtures in the repository.

## Development

Prerequisites:

- Python 3.12 or newer
- `uv`
- `ffmpeg` for media work beyond the scaffold

```bash
make sync
make verify
```

`make verify` is the local and CI verification entrypoint. It runs linting,
format checks, type checks, tests, and the public repository safety check.

## Current local support

The current `lectern ingest` path supports the synthetic fixture workflow:

```bash
uv run lectern ingest tests/fixtures/synthetic_talk.wav
```

That fixture uses a committed `.transcript.txt` sidecar so CI can check bundle
behavior and transcript passthrough without downloading ASR models or sending
media to remote services.

Lectern can also use an optional local JSON transcriber command for media
without a sidecar:

```bash
uv run lectern ingest local-talk.wav --transcriber-command "my-local-asr --json {input}"
```

The command is executed locally with the normalized audio path and must emit JSON
segments or text. Lectern records transcript method metadata and timestamp
anchors, but it does not bundle an ASR model, does not call remote transcription
providers, and does not claim transcript faithfulness. A user-supplied command
runs with the user's privileges; Lectern cannot prove that command never opens a
network connection.

Lectern also has an early local automation spine for folder sources. It records
source and queue state in local SQLite, scans local folders without network
access, requires explicit queue approval before ingesting a discovered item, and
indexes completed bundles:

```bash
uv run lectern sources add-folder talks ~/Talks
uv run lectern sources scan talks
uv run lectern queue list
uv run lectern queue approve <queue-item-id>
uv run lectern queue ingest <queue-item-id>
uv run lectern library list
```

Use `--json` on source, queue, and library commands for machine-readable output.
The state database is local run state under `.lectern/` by default and should not
be committed.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Security
and privacy reporting guidance is in [SECURITY.md](SECURITY.md).

## Documentation

- [Design](docs/DESIGN.md)
- [Grey Areas](docs/GREY_AREAS.md)
- [Architecture decisions](docs/adr/)
- [Automated contributor guidance](AGENTS.md)

## License

MIT. Lectern processes media you have the right to access, on your own machine,
and provides no redistribution features.
