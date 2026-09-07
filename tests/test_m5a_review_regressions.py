"""Regression tests for the final M5a review findings.

These cases exercise only public M5a behavior: migration privacy, multilingual
literal retrieval, source-to-bundle identity binding, artifact integrity, cache
reconciliation, CLI literal search, and artifact validity. They intentionally do
not introduce the later M5b read surface.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from lectern import cli
from lectern.automation import open_state
from lectern.bundle import MANIFEST_NAME, TranscriptSegmentsDocument
from lectern.ingest import ingest_local
from lectern.migrations import prepare_bundle_migration
from lectern.records import AutomationError, LibraryStatus
from lectern.search import AnchorResolution

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
    backend["command"] = f"{tmp_path / 'Private Tools' / 'transcribe'} --input {media}"

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
    assert "command" not in migrated_backend


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


def test_retrieval_rejects_segments_that_do_not_match_the_manifest(tmp_path: Path) -> None:
    state_path, bundle = _registered_bundle(tmp_path)
    segments_path = bundle / "transcript" / "segments.json"
    raw_segments = json.loads(segments_path.read_text(encoding="utf-8"))
    assert isinstance(raw_segments, list) and raw_segments
    segments = cast(list[dict[str, Any]], raw_segments)
    segment = segments[0]
    segment_id = int(segment["id"])

    with open_state(state_path) as state:
        anchor, _ = state.cite_segment(bundle.name, segment_id)

    segment["text"] = "forged but schema-valid evidence marker"
    _write_json(segments_path, segments)

    with open_state(state_path) as state:
        assert state.search_segments("forged but schema-valid evidence marker") == []
        assert state.indexed_segment_count(bundle_id=bundle.name) == 0
        assert state.get_library_bundle(bundle.name).status is LibraryStatus.NEEDS_REPROCESSING
        assert state.resolve_anchor(anchor).outcome is AnchorResolution.MODIFIED
        with pytest.raises(AutomationError, match="no readable transcript"):
            state.cite_segment(bundle.name, segment_id)


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


def test_forgetting_a_bundle_removes_its_cached_fingerprint(tmp_path: Path) -> None:
    state_path, bundle = _registered_bundle(tmp_path)
    with open_state(state_path) as state:
        assert state.search_segments("knowledge")
        state.forget_library_bundle(bundle.name)

    with sqlite3.connect(state_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM indexed_bundles WHERE bundle_id = ?", (bundle.name,)
        ).fetchone()
    assert row is not None and row[0] == 0


@pytest.mark.parametrize(
    "query_tokens",
    [
        ["use", "--json", "output"],
        ["literal", "--state", "evidence"],
        ["trailing", "--operators"],
    ],
)
def test_library_search_option_terminator_preserves_literal_cli_tokens(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    query_tokens: list[str],
) -> None:
    state_path = tmp_path / "state.sqlite"
    with open_state(state_path) as state:
        state.index_synthetic_segment(
            "cli-literal",
            0,
            "use --json output; literal --state evidence; trailing --operators",
        )

    assert (
        cli.main(
            [
                "library",
                "search",
                "--state",
                str(state_path),
                "--",
                *query_tokens,
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "cli-literal" in captured.out
    assert captured.err == ""


def test_transcript_segments_document_rejects_an_empty_array() -> None:
    with pytest.raises(ValueError, match="at least one segment"):
        TranscriptSegmentsDocument.model_validate([])


@pytest.mark.parametrize("text", ["", " \t\r\n", "\u001c\u0085\u2003\u3000"])
def test_blank_current_segments_remove_stale_index_and_refuse_citations(
    tmp_path: Path, text: str
) -> None:
    state_path, bundle = _registered_bundle(tmp_path)
    segments_path = bundle / "transcript" / "segments.json"
    segments = cast(list[dict[str, Any]], json.loads(segments_path.read_text()))
    segment_id = int(segments[0]["id"])
    with open_state(state_path) as state:
        anchor, _ = state.cite_segment(bundle.name, segment_id)
        assert state.indexed_segment_count(bundle_id=bundle.name) > 0
    segments[0]["text"] = text
    _write_json(segments_path, segments)
    manifest = _read_object(bundle / MANIFEST_NAME)
    _refresh_manifest_outputs(bundle, manifest)
    _write_json(bundle / MANIFEST_NAME, manifest)
    for stage in manifest["stages"].values():
        for output in stage["outputs"]:
            assert _digest(bundle / output["path"]) == (output["sha256"], output["bytes"])
    # Model a persisted pre-tightening blank entry; registered readers must clear
    # this derived cache even though the SQLite layout/version is unchanged.
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "UPDATE segment_index SET display = ?, body = ?, literal = ? "
            "WHERE bundle_id = ? AND segment_id = ?",
            (text, text, text, bundle.name, segment_id),
        )
    with open_state(state_path) as state:
        assert state.search_segments("knowledge") == []
        assert state.indexed_segment_count(bundle_id=bundle.name) == 0
        with pytest.raises(AutomationError, match="no readable transcript"):
            state.cite_segment(bundle.name, segment_id)
        assert state.resolve_anchor(anchor).outcome is not AnchorResolution.EXACT
