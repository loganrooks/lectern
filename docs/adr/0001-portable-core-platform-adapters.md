# ADR-0001: Portable Python core with platform-optimized adapters

Status: accepted · 2026-06-10

## Context

Lectern must eventually run well on macOS, Linux, and Windows, but v1 targets macOS/Apple Silicon. The temptation is either (a) a generic lowest-common-denominator pipeline, or (b) a native Apple build (Swift/MLX-first) that forecloses portability.

## Decision

One portable core — pipeline orchestration, bundle schema, CLI, MCP server — in Python, identical on all platforms. All hardware/OS-specific capability (transcription, OCR, VLM) lives behind small interfaces (`Transcriber`, `Ocr`, `Vlm`) with runtime backend selection (`lectern doctor` reports the resolution). macOS gets first-class backends (mlx-whisper, Vision OCR); other platforms get faster-whisper/whisper.cpp/tesseract.

## Consequences

- Per-platform optimization without per-platform forks; adding a platform = implementing interfaces.
- Python chosen because the inference ecosystem is Python-wrapped native code and the heavy lifting (ffmpeg, whisper.cpp, MLX) is already native; orchestration cost is negligible. Strict pyright + ruff make it tractable for agentic implementation.
- Risk accepted: Python packaging friction for end users — mitigated via `uv tool install` and (later) brew formula.
- Rejected: Swift core (forecloses Linux; splits contributor base), Rust core (premature; no measured orchestration bottleneck), Electron (no GUI in v1 at all — see ADR-0004).
