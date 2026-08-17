"""Regression tests for the final M5a review findings.

These cases exercise only public M5a behavior: migration privacy, multilingual
literal retrieval, source-to-bundle identity binding, cache reconciliation, and
artifact validity. They intentionally do not introduce the later M5b read
surface.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest

from lectern import cli
from lectern.automation import open_state
from lectern.bundle import MANIFEST_NAME, TranscriptSegmentsDocument
from lectern.ingest import ingest_local
from lectern.migrations import prepare_bundle_migration
from lectern.records import AutomationError, LibraryStatus

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC_TALK = FIXTURE_DIR / "synthetic_talk.wav"
SYNTHETIC_TRANSCRIPT = FIXTURE_DIR / "synthetic_talk.transcript.txt"


def _digest(path: Path) -> tuple[str, int]:
    payload = path.read_bytes()
    return hashlib.sha256(payload).hexdigest(), len(payload)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _refresh_manifest_outputs(bundle: Path, manifest: dict[str, Any]) -> None:
    stages = cast(dict[str, dict[str, Any]], manifest["stages"])
    for stage in stages.values():
        for output in cast(list[dict[str, Any]], stage["outputs"]):
            digest, size = _digest(bundle / str(output["path"]))
            output["sha256"] = digest
            output["bytes"] = size


def _registered_bundle(tmp_path: Path) -> tuple[Path, Path]:
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True)
    media = media_dir / "synthetic_talk.wav"
    media.write_bytes(SYNTHETIC_TALK.read_bytes())
    media.with_suffix(".transcript.txt").write_text(
        SYNTHETIC_TRANSCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    state_path = tmp_path / "state.sqlite"
    output = tmp_path / "bundles"
    assert (
        cli.main(
            [
                "ingest",
                str(media),
                "--output",
                str(output),
                "--state",
                str(state_path),
            ]
        )
        == 0
    )
    bundles = sorted(path for path in output.iterdir() if path.is_dir())
    assert len(bundles) == 1
    return state_path, bundles[0]


def test_migration_reduces_legacy_backend_argv0_to_a_basename(tmp_path: Path) -> None:
    media = tmp_path / "Private Recordings" / "synthetic_talk.wav"
    media.parent.mkdir(parents=True)
    media.write_bytes(SYNTHETIC_TALK.read_bytes())
    media.with_suffix(".transcript.txt").write_text(
        SYNTHETIC_TRANSCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    bundle = ingest_local(media, tmp_path / "legacy-bundles").bundle_dir

    manifest_path = bundle / MANIFEST_NAME
    source_path = bundle / "source.json"
    metadata_path = bundle / "transcript" / "metadata.json"
    manifest = _read_object(manifest_path)
    source = _read_object(source_path)
    metadata = _read_object(metadata_path)

    manifest["schema_version"] = "0.1.0"
    manifest_source = cast(dict[str, Any], manifest["source"])
    manifest_source["ref"] = str(media)
    manifest_source.pop("bytes", None)
    source_record = cast(dict[str, Any], source["source"])
    source_record["ref"] = str(media)
    source_record.pop("bytes", None)
    sidecar = cast(dict[str, Any], source["transcript_sidecar"])
    sidecar["path"] = str(media.with_suffix(".transcript.txt"))
    source_media = cast(dict[str, Any], metadata["source_media"])
    source_media["path"] = str(media)
    backend = cast(dict[str, Any], metadata["backend"])
    backend["path"] = str(media.with_suffix(".transcript.txt"))
    backend["argv0"] = str(tmp_path / "Private Tools" / "transcribe")

    _write_json(source_path, source)
    _write_json(metadata_path, metadata)
    _refresh_manifest_outputs(bundle, manifest)
    _write_json(manifest_path, manifest)

    prepared = prepare_bundle_migration(bundle)
    migrated_metadata = _read_object(prepared.staging_dir / "transcript" / "metadata.json")
    migrated_backend = cast(dict[str, Any], migrated_metadata["backend"])
    assert migrated_backend["argv0"] == "transcribe"
    assert "/" not in str(migrated_backend["argv0"])
    assert "\\" not in str(migrated_backend["argv0"])


@pytest.mark.parametrize(
    ("name", "text", "query"),
    [
        ("modern-jamo", "ᄀᄀᄀ", "ᄀᄀ"),
        ("compatibility-jamo", "ㅋㅋㅋ", "ㅋㅋ"),
        ("half-width-jamo", "ﾡﾡﾡ", "ﾡﾡ"),
    ],
)
def test_literal_search_segments_standalone_hangul_jamo(
    tmp_path: Path, name: str, text: str, query: str
) -> None:
    state_path = tmp_path / "state.sqlite"
    with open_state(state_path) as state:
        state.index_synthetic_segment(name, 0, text)
        assert [hit.bundle_id for hit in state.search_segments(query)] == [name]
        with pytest.raises(ValueError, match="operator-mode search does not support"):
            state.search_segments(f"{query[0]} OR {query[-1]}", literal=False)


def test_retrieval_binds_source_metadata_to_the_registered_manifest(tmp_path: Path) -> None:
    state_path, bundle = _registered_bundle(tmp_path)
    with open_state(state_path) as state:
        hit = state.search_segments("knowledge")[0]
        assert hit.segment_id is not None
        segment_id = hit.segment_id

    source_path = bundle / "source.json"
    source = _read_object(source_path)
    replacement_digest = "0" * 64
    source_record = cast(dict[str, Any], source["source"])
    source_record["ref"] = f"sha256:{replacement_digest}"
    source_record["bytes"] = 1
    source["sha256"] = replacement_digest
    source["bytes"] = 1
    _write_json(source_path, source)

    manifest_path = bundle / MANIFEST_NAME
    manifest = _read_object(manifest_path)
    _refresh_manifest_outputs(bundle, manifest)
    _write_json(manifest_path, manifest)

    with open_state(state_path) as state:
        assert state.search_segments("knowledge") == []
        with pytest.raises(AutomationError, match="readable transcript"):
            state.cite_segment(bundle.name, segment_id)
        assert state.get_library_bundle(bundle.name).status is not LibraryStatus.READY


@pytest.mark.parametrize("query", ["++", "???", "🙂"])
def test_literal_search_falls_back_for_tokenless_queries(tmp_path: Path, query: str) -> None:
    state_path = tmp_path / "state.sqlite"
    with open_state(state_path) as state:
        state.index_synthetic_segment("tokenless", 0, f"before {query} after")
        assert [hit.bundle_id for hit in state.search_segments(query)] == ["tokenless"]


def test_cached_fingerprint_uses_the_same_segment_order_as_the_index(tmp_path: Path) -> None:
    state_path, bundle = _registered_bundle(tmp_path)
    segments_path = bundle / "transcript" / "segments.json"
    segments = json.loads(segments_path.read_text(encoding="utf-8"))
    assert isinstance(segments, list) and segments
    template = cast(dict[str, Any], segments[0])
    reordered = [
        {**template, "id": 2, "start_s": 2.0, "end_s": 3.0, "text": "second marker"},
        {**template, "id": 1, "start_s": 1.0, "end_s": 2.0, "text": "first marker"},
    ]
    _write_json(segments_path, reordered)

    manifest_path = bundle / MANIFEST_NAME
    manifest = _read_object(manifest_path)
    _refresh_manifest_outputs(bundle, manifest)
    _write_json(manifest_path, manifest)

    with open_state(state_path) as state:
        assert state.search_segments("first marker")
    with open_state(state_path) as state:
        assert state.refresh_changed_bundles() == []


def test_transcript_segments_document_rejects_an_empty_array() -> None:
    with pytest.raises(ValueError, match="at least one segment"):
        TranscriptSegmentsDocument.model_validate([])
