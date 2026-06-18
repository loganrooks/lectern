"""Local ingest pipeline for early Lectern milestones.

M1 intentionally avoids remote services and model downloads. The transcriber
implemented here accepts synthetic fixtures with a committed transcript sidecar;
general-purpose ASR lands in a later milestone once the dependency/model policy
is explicit.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lectern.bundle import (
    ArtifactRef,
    Manifest,
    Source,
    SourceKind,
    StageName,
    StageRecord,
    StageState,
)

CANONICAL_SAMPLE_RATE = 16_000
CANONICAL_CHANNELS = 1
CANONICAL_SAMPLE_WIDTH = 2


class IngestError(RuntimeError):
    """Raised when a local ingest cannot complete under current constraints."""


@dataclass(frozen=True)
class IngestResult:
    """Result of a local ingest run."""

    bundle_dir: Path
    manifest: Manifest


def ingest_local(source_path: Path, output_root: Path = Path("bundles")) -> IngestResult:
    """Ingest a local media file into a Lectern bundle."""

    source = source_path.expanduser()
    if not source.is_file():
        raise IngestError(f"source file does not exist: {source}")

    source_digest = _sha256(source)
    source_duration = _wav_duration_seconds(source)
    bundle_id = f"{_slug(source.stem)}-{source_digest[:12]}"
    bundle_dir = output_root / bundle_id

    manifest = Manifest(
        bundle_id=bundle_id,
        source=Source(
            kind=SourceKind.LOCAL,
            ref=str(source),
            title=source.stem.replace("_", " ").title(),
            duration_s=source_duration,
        ),
    )

    _ensure_bundle_dirs(bundle_dir)

    source_json = bundle_dir / "source.json"
    _write_json(
        source_json,
        {
            "source": manifest.source.model_dump(mode="json"),
            "sha256": source_digest,
            "bytes": source.stat().st_size,
        },
    )
    manifest.stages[StageName.ACQUIRE] = _done_stage(bundle_dir, [source_json])

    audio_path = bundle_dir / "media" / "audio.wav"
    _normalize_to_canonical_wav(source, audio_path)
    manifest.stages[StageName.NORMALIZE] = _done_stage(bundle_dir, [audio_path])

    transcript = _read_fixture_transcript(source)
    segments_path = bundle_dir / "transcript" / "segments.json"
    transcript_path = bundle_dir / "transcript" / "transcript.md"
    _write_json(segments_path, _segments_for(transcript, source_duration))
    transcript_path.write_text(transcript.rstrip() + "\n", encoding="utf-8")
    manifest.stages[StageName.TRANSCRIBE] = _done_stage(
        bundle_dir,
        [segments_path, transcript_path],
    )

    summary_path = bundle_dir / "analysis" / "summary.md"
    summary_path.write_text(_summary_lite(transcript), encoding="utf-8")
    manifest.stages[StageName.SYNTHESIZE] = _done_stage(bundle_dir, [summary_path])

    manifest.save(bundle_dir)
    return IngestResult(bundle_dir=bundle_dir, manifest=manifest)


def _ensure_bundle_dirs(bundle_dir: Path) -> None:
    for relative in ("media", "transcript", "analysis", "log"):
        (bundle_dir / relative).mkdir(parents=True, exist_ok=True)


def _read_fixture_transcript(source: Path) -> str:
    transcript_path = source.with_suffix(".transcript.txt")
    if transcript_path.is_file():
        transcript = transcript_path.read_text(encoding="utf-8").strip()
        if transcript:
            return transcript
        raise IngestError(f"fixture transcript is empty: {transcript_path}")

    raise IngestError(
        "no local transcription backend is configured for this file; "
        "synthetic fixtures must provide a .transcript.txt sidecar"
    )


def _normalize_to_canonical_wav(source: Path, output: Path) -> None:
    if _is_canonical_wav(source):
        shutil.copyfile(source, output)
        return

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise IngestError("ffmpeg is required to normalize non-canonical audio")

    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ac",
            str(CANONICAL_CHANNELS),
            "-ar",
            str(CANONICAL_SAMPLE_RATE),
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown ffmpeg failure"
        raise IngestError(f"audio normalization failed: {message}")


def _is_canonical_wav(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as audio:
            return (
                audio.getnchannels() == CANONICAL_CHANNELS
                and audio.getframerate() == CANONICAL_SAMPLE_RATE
                and audio.getsampwidth() == CANONICAL_SAMPLE_WIDTH
            )
    except wave.Error:
        return False


def _wav_duration_seconds(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as audio:
            frame_rate = audio.getframerate()
            if frame_rate <= 0:
                return None
            return audio.getnframes() / frame_rate
    except wave.Error:
        return None


def _segments_for(transcript: str, duration_s: float | None) -> list[dict[str, Any]]:
    return [
        {
            "id": 0,
            "start_s": 0.0,
            "end_s": duration_s,
            "text": transcript,
            "source": "fixture_transcript",
        }
    ]


def _summary_lite(transcript: str) -> str:
    normalized = " ".join(transcript.split())
    first_sentence = normalized.split(". ", maxsplit=1)[0].rstrip(".") + "."
    word_count = len(normalized.split())
    return f"# Summary\n\n{first_sentence}\n\nWords: {word_count}\n"


def _done_stage(bundle_dir: Path, outputs: list[Path]) -> StageRecord:
    now = datetime.now(UTC)
    return StageRecord(
        state=StageState.DONE,
        started=now,
        finished=now,
        outputs=[_artifact_ref(bundle_dir, output) for output in outputs],
    )


def _artifact_ref(bundle_dir: Path, path: Path) -> ArtifactRef:
    data = path.read_bytes()
    return ArtifactRef(
        path=path.relative_to(bundle_dir).as_posix(),
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _slug(value: str) -> str:
    chars = [char.lower() if char.isalnum() else "-" for char in value]
    slug = "-".join(part for part in "".join(chars).split("-") if part)
    return slug or "bundle"
