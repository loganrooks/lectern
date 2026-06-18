from __future__ import annotations

import json
import tomllib
import wave
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from lectern import ingest as ingest_module
from lectern.bundle import Manifest, SourceKind, StageName, StageState
from lectern.ingest import IngestError, ingest_local

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC_TALK = FIXTURE_DIR / "synthetic_talk.wav"
SYNTHETIC_TRANSCRIPT = FIXTURE_DIR / "synthetic_talk.transcript.txt"
QUALITY_CONFIG = Path(__file__).resolve().parent / "quality.toml"


def test_local_ingest_acceptance_for_synthetic_talk(tmp_path: Path) -> None:
    result = ingest_local(SYNTHETIC_TALK, tmp_path)
    bundle_dir = result.bundle_dir

    manifest = Manifest.load(bundle_dir)
    assert manifest.bundle_id.startswith("synthetic-talk-")
    assert manifest.source.kind is SourceKind.LOCAL
    assert manifest.source.ref == str(SYNTHETIC_TALK)

    for stage in (
        StageName.ACQUIRE,
        StageName.NORMALIZE,
        StageName.TRANSCRIBE,
        StageName.SYNTHESIZE,
    ):
        record = manifest.stages[stage]
        assert record.state is StageState.DONE
        assert record.outputs

    assert (bundle_dir / "source.json").is_file()
    assert (bundle_dir / "media" / "audio.wav").is_file()
    assert (bundle_dir / "transcript" / "segments.json").is_file()
    assert (bundle_dir / "transcript" / "transcript.md").is_file()
    assert (bundle_dir / "analysis" / "summary.md").is_file()
    source_record = json.loads((bundle_dir / "source.json").read_text(encoding="utf-8"))
    assert source_record["transcript_sidecar"]["path"] == str(SYNTHETIC_TRANSCRIPT)
    assert source_record["transcript_sidecar"]["bytes"] == SYNTHETIC_TRANSCRIPT.stat().st_size

    reference = SYNTHETIC_TRANSCRIPT.read_text(encoding="utf-8").strip()
    transcript = (bundle_dir / "transcript" / "transcript.md").read_text(encoding="utf-8").strip()
    threshold = _synthetic_talk_threshold()
    assert _word_accuracy(reference, transcript) >= threshold

    segments = json.loads((bundle_dir / "transcript" / "segments.json").read_text(encoding="utf-8"))
    assert segments == [
        {
            "id": 0,
            "start_s": 0.0,
            "end_s": manifest.source.duration_s,
            "text": reference,
            "source": "fixture_transcript",
        }
    ]

    summary = (bundle_dir / "analysis" / "summary.md").read_text(encoding="utf-8")
    assert (
        "[t=00:00] Lectern turns recorded talks into local inspectable knowledge bundles."
        in summary
    )


def test_local_ingest_rejects_missing_source(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="source file does not exist"):
        ingest_local(tmp_path / "missing.wav", tmp_path / "bundles")


def test_local_ingest_requires_transcript_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "synthetic_talk.wav"
    source.write_bytes(SYNTHETIC_TALK.read_bytes())

    with pytest.raises(IngestError, match="no local transcription backend"):
        ingest_local(source, tmp_path / "bundles")

    assert not (tmp_path / "bundles").exists()


def test_local_ingest_rejects_empty_transcript_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "synthetic_talk.wav"
    source.write_bytes(SYNTHETIC_TALK.read_bytes())
    source.with_suffix(".transcript.txt").write_text("\n", encoding="utf-8")

    with pytest.raises(IngestError, match="fixture transcript is empty"):
        ingest_local(source, tmp_path / "bundles")


def test_local_ingest_rejects_invalid_transcript_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "synthetic_talk.wav"
    source.write_bytes(SYNTHETIC_TALK.read_bytes())
    source.with_suffix(".transcript.txt").write_bytes(b"\xff\xfe\x00")

    with pytest.raises(IngestError, match="not valid UTF-8"):
        ingest_local(source, tmp_path / "bundles")


def test_transcript_sidecar_changes_bundle_identity(tmp_path: Path) -> None:
    first_source = tmp_path / "first" / "synthetic_talk.wav"
    second_source = tmp_path / "second" / "synthetic_talk.wav"
    first_source.parent.mkdir()
    second_source.parent.mkdir()
    first_source.write_bytes(SYNTHETIC_TALK.read_bytes())
    second_source.write_bytes(SYNTHETIC_TALK.read_bytes())
    first_source.with_suffix(".transcript.txt").write_text("first transcript\n", encoding="utf-8")
    second_source.with_suffix(".transcript.txt").write_text("second transcript\n", encoding="utf-8")

    first = ingest_local(first_source, tmp_path / "bundles")
    second = ingest_local(second_source, tmp_path / "bundles")

    assert first.bundle_dir != second.bundle_dir


def test_noncanonical_audio_requires_ffmpeg(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    source = tmp_path / "not_audio.bin"
    source.write_bytes(b"not a canonical wav")
    source.with_suffix(".transcript.txt").write_text("synthetic transcript\n", encoding="utf-8")

    def missing_binary(name: str) -> str | None:
        del name
        return None

    monkeypatch.setattr(ingest_module.shutil, "which", missing_binary)

    with pytest.raises(IngestError, match="ffmpeg is required"):
        ingest_local(source, tmp_path / "bundles")


def test_local_ingest_uses_normalized_audio_duration(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    source = tmp_path / "captured.bin"
    source.write_bytes(b"non-wav media placeholder")
    source.with_suffix(".transcript.txt").write_text("synthetic transcript\n", encoding="utf-8")
    normalized_duration_s = 0.25

    def fake_normalize(source_path: Path, output: Path) -> None:
        assert source_path == source
        _write_test_wav(output, normalized_duration_s)

    monkeypatch.setattr(ingest_module, "_normalize_to_canonical_wav", fake_normalize)

    result = ingest_local(source, tmp_path / "bundles")

    assert result.manifest.source.duration_s == normalized_duration_s
    segments = json.loads((result.bundle_dir / "transcript" / "segments.json").read_text())
    assert segments[0]["end_s"] == normalized_duration_s


def test_empty_wav_probe_returns_ingest_error(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    empty.with_suffix(".transcript.txt").write_text("synthetic transcript\n", encoding="utf-8")

    def missing_binary(name: str) -> str | None:
        del name
        return None

    monkeypatch.setattr(ingest_module.shutil, "which", missing_binary)

    with pytest.raises(IngestError, match="ffmpeg is required"):
        ingest_local(empty, tmp_path / "bundles")


def _write_test_wav(path: Path, duration_s: float) -> None:
    frame_count = int(ingest_module.CANONICAL_SAMPLE_RATE * duration_s)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(ingest_module.CANONICAL_CHANNELS)
        audio.setsampwidth(ingest_module.CANONICAL_SAMPLE_WIDTH)
        audio.setframerate(ingest_module.CANONICAL_SAMPLE_RATE)
        audio.writeframes(b"\x00\x00" * frame_count)


def _synthetic_talk_threshold() -> float:
    config = tomllib.loads(QUALITY_CONFIG.read_text(encoding="utf-8"))
    threshold = config["transcript"]["synthetic_talk"]["word_accuracy_threshold"]
    assert isinstance(threshold, float)
    return threshold


def _word_accuracy(reference: str, candidate: str) -> float:
    reference_words = reference.split()
    candidate_words = candidate.split()
    if not reference_words:
        return 1.0 if not candidate_words else 0.0
    distance = _edit_distance(reference_words, candidate_words)
    return max(0.0, 1.0 - distance / len(reference_words))


def _edit_distance(left: list[str], right: list[str]) -> int:
    previous = list(range(len(right) + 1))
    for row_index, left_word in enumerate(left, start=1):
        current = [row_index]
        for column_index, right_word in enumerate(right, start=1):
            substitution_cost = 0 if left_word == right_word else 1
            current.append(
                min(
                    previous[column_index] + 1,
                    current[column_index - 1] + 1,
                    previous[column_index - 1] + substitution_cost,
                )
            )
        previous = current
    return previous[-1]
