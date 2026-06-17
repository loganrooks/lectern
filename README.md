# lectern

**Turn talks into thought.** Lectern ingests recorded talks — YouTube videos, streams, and your own live lecture recordings — and renders them into *knowledge bundles*: timestamped transcripts, analyzed visual aids, extracted references, and grounded summaries, structured for both human reading and agentic workflows (summarize, chat-with-talk, trace the surrounding discourse, surface follow-up questions).

> Working name. Candidates under consideration: **lectern** (where the talk happens), **rostrum**, **ruminant** (the digestion metaphor), **colloq**. Rename is trivial pre-publication; see `docs/DESIGN.md` §Open Items.

## Why

AI research talks are dense with expert guidance that never makes it into papers — but there are too many to watch, and "Watch Later" is where they go to die. Lectern replaces that dead-end with an ingestion queue: add a video to a dedicated playlist (or drop a recording in a folder), and it becomes a fully analyzable artifact you can interrogate instead of a 90-minute obligation.

## Status

**Pre-implementation.** Design is complete (`docs/DESIGN.md`); implementation is early. Nothing is usable yet.

## Design at a glance

- **Pipeline of pure stages**: acquire → normalize → transcribe → diarize → visual → enrich → situate → synthesize. Each stage reads/writes typed on-disk artifacts inside a *bundle* — the durable contract (`docs/adr/0003`).
- **Local-first compute**: transcription via mlx-whisper / whisper.cpp / faster-whisper on your machine; cloud APIs are per-stage opt-in (`docs/adr/0002`). Local recordings never leave your machine without explicit per-item consent.
- **Portable core, platform adapters**: one Python core + CLI everywhere; hardware/OS-specific backends (MLX on Apple Silicon, CUDA elsewhere, macOS Vision OCR vs tesseract) behind capability interfaces (`docs/adr/0001`).
- **CLI + watch daemon first, GUI later** (`docs/adr/0004`).
- **Agent-ready output**: deterministic bundle layout, every claim timestamp-anchored, plus an MCP server for chat-with-talk.

## For contributors

Read `docs/DESIGN.md`, then `docs/GREY_AREAS.md` (the honest list of legal/technical grey zones and how we handle them). Verification entrypoint: `make verify`.

## License

MIT (see `LICENSE`). Lectern processes media you have the right to access, on your own machine, and provides no redistribution features — see `docs/GREY_AREAS.md` §1.
