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
