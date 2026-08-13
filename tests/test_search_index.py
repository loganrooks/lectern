"""The index's life, not just its shape.

The failure this module exists for is invisible to every other test in the
suite: a user upgrades with hundreds of existing bundles, the migration creates
an empty index, only newly ingested bundles are ever written to it, and every
recording they already had becomes unsearchable -- while a fixture suite that
only ever builds *new* bundles stays entirely green.

That asymmetry is the point. A test that ingests and then searches proves the
write path works. It cannot prove that anything already on disk was carried
across, because it never had anything already on disk.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from lectern import cli, search
from lectern.automation import open_state
from lectern.records import AutomationError, LibraryStatus
from lectern.state import STATE_SCHEMA_VERSION

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC_TALK = FIXTURE_DIR / "synthetic_talk.wav"
SYNTHETIC_TRANSCRIPT = FIXTURE_DIR / "synthetic_talk.transcript.txt"


def _ingest(tmp_path: Path, name: str = "synthetic_talk") -> tuple[Path, Path]:
    """Ingest the fixture and return (state_path, bundle_dir)."""

    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    media = media_dir / f"{name}.wav"
    media.write_bytes(SYNTHETIC_TALK.read_bytes())
    media.with_suffix(".transcript.txt").write_text(
        SYNTHETIC_TRANSCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    state_path = tmp_path / "state.sqlite"
    out = tmp_path / "bundles"
    assert cli.main(["ingest", str(media), "--output", str(out), "--state", str(state_path)]) == 0
    bundles = sorted(path for path in out.iterdir() if path.is_dir())
    return state_path, bundles[-1]


def _indexed_segment_count(state_path: Path) -> int:
    with open_state(state_path) as state:
        return state.indexed_segment_count()


def _revert_to_v2(state_path: Path) -> None:
    """Make a populated store look like one written before the index existed.

    Faithful to the situation that matters: the library rows are already there
    and the index is not, which is exactly what an upgrading user has.
    """

    connection = sqlite3.connect(state_path)
    connection.execute("DROP TABLE IF EXISTS segment_index")
    connection.execute("DROP TABLE IF EXISTS index_signature")
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()


def test_ingest_indexes_the_bundle(tmp_path: Path) -> None:
    state_path, _ = _ingest(tmp_path)
    assert _indexed_segment_count(state_path) > 0


def test_migration_backfills_existing_library_rows(tmp_path: Path) -> None:
    """The 500-bundle upgrade, in miniature.

    Without backfill this passes every other test in the suite and leaves a
    user's entire existing archive unsearchable.
    """

    state_path, _ = _ingest(tmp_path)
    indexed_before = _indexed_segment_count(state_path)
    assert indexed_before > 0

    _revert_to_v2(state_path)
    connection = sqlite3.connect(state_path)
    assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 2
    assert connection.execute("SELECT COUNT(*) FROM library_bundles").fetchone()[0] == 1
    connection.close()

    # Opening the store is what upgrades it.
    assert _indexed_segment_count(state_path) == indexed_before
    connection = sqlite3.connect(state_path)
    assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == STATE_SCHEMA_VERSION
    connection.close()


def test_reconciliation_reports_agreement_after_ingest(tmp_path: Path) -> None:
    state_path, _ = _ingest(tmp_path)
    with open_state(state_path) as state:
        assert state.unindexed_bundle_ids() == []


def test_retrieval_follows_the_validated_source_segments_pointer(tmp_path: Path) -> None:
    state_path, bundle = _ingest(tmp_path)
    source_path = bundle / "source.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    original_segments = bundle / str(source["transcript"]["segments"])
    moved_segments = bundle / "transcript" / "retrieval-segments.json"
    segments = json.loads(original_segments.read_text(encoding="utf-8"))
    segments[0]["text"] = "pointer-selected unique phrase"
    moved_segments.write_text(json.dumps(segments), encoding="utf-8")
    original_segments.unlink()
    source["transcript"]["segments"] = "transcript/retrieval-segments.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")

    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    replacements = {
        "source.json": source_path,
        "transcript/segments.json": moved_segments,
    }
    for stage in manifest["stages"].values():
        for output in stage["outputs"]:
            replacement = replacements.get(output["path"])
            if replacement is None:
                continue
            payload = replacement.read_bytes()
            output["path"] = replacement.relative_to(bundle).as_posix()
            output["sha256"] = hashlib.sha256(payload).hexdigest()
            output["bytes"] = len(payload)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with open_state(state_path) as state:
        hit = state.search_segments("pointer-selected unique phrase")[0]
        _, resolved = state.cite_segment(bundle.name, int(hit.segment_id or 0))
        assert resolved.current_text == "pointer-selected unique phrase"


def test_retrieval_refuses_a_symlinked_segments_pointer(tmp_path: Path) -> None:
    state_path, bundle = _ingest(tmp_path)
    segments_path = bundle / "transcript" / "segments.json"
    external = tmp_path / "external-segments.json"
    segments_path.rename(external)
    segments_path.symlink_to(external)

    with open_state(state_path) as state:
        assert state.indexed_segment_count(bundle_id=bundle.name) == 0
        assert state.search_segments("knowledge") == []
        with pytest.raises(AutomationError, match="readable transcript"):
            state.cite_segment(bundle.name, 0)


def test_registered_bundle_identity_must_match_the_loaded_manifest(tmp_path: Path) -> None:
    state_path, bundle = _ingest(tmp_path)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["bundle_id"] = "replacement-bundle"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with open_state(state_path) as state:
        assert state.indexed_segment_count(bundle_id=bundle.name) == 0
        assert state.search_segments("knowledge") == []
        with pytest.raises(AutomationError, match="readable transcript"):
            state.cite_segment(bundle.name, 0)
        assert state.get_library_bundle(bundle.name).status is LibraryStatus.INCOMPLETE


def test_reconciliation_repairs_a_missing_cached_row(tmp_path: Path) -> None:
    """A cached digest must not hide missing FTS rows."""

    state_path, _ = _ingest(tmp_path)
    connection = sqlite3.connect(state_path)
    connection.execute("DELETE FROM segment_index")
    connection.commit()
    connection.close()

    with open_state(state_path) as state:
        assert state.unindexed_bundle_ids() == []
        assert state.indexed_segment_count() > 0


def test_reconciliation_removes_stale_hits_when_segments_disappear(tmp_path: Path) -> None:
    """Search must not retain text the current bundle can no longer support."""

    state_path, bundle = _ingest(tmp_path)
    with open_state(state_path) as state:
        assert [hit for hit in state.search_segments("knowledge") if hit.bundle_id == bundle.name]

    (bundle / "transcript" / "segments.json").unlink()

    with open_state(state_path) as state:
        assert not [
            hit for hit in state.search_segments("knowledge") if hit.bundle_id == bundle.name
        ]
        assert bundle.name in state.unindexed_bundle_ids()


@pytest.mark.parametrize("corrupt_bytes", [b"{", b"\xff"])
def test_reconciliation_removes_stale_hits_when_segments_are_corrupt(
    tmp_path: Path, corrupt_bytes: bytes
) -> None:
    """Readable but unparsable bytes cannot continue supporting old search results."""

    state_path, bundle = _ingest(tmp_path)
    with open_state(state_path) as state:
        assert [hit for hit in state.search_segments("knowledge") if hit.bundle_id == bundle.name]

    (bundle / "transcript" / "segments.json").write_bytes(corrupt_bytes)

    with open_state(state_path) as state:
        assert not [
            hit for hit in state.search_segments("knowledge") if hit.bundle_id == bundle.name
        ]
        assert bundle.name in state.unindexed_bundle_ids()


def test_rebuild_on_signature_mismatch(tmp_path: Path) -> None:
    """An index built under one segmentation rule must not be queried under another.

    The stored signature is what turns a silent wrong-answer into a rebuild.
    Without it, upgrading the segmenter leaves an index whose tokens no query
    will ever produce, and retrieval fails by returning nothing.
    """

    state_path, _ = _ingest(tmp_path)
    connection = sqlite3.connect(state_path)
    connection.execute("UPDATE index_signature SET segmenter_version = -1")
    connection.execute("DELETE FROM segment_index")
    connection.commit()
    connection.close()

    # Reopening detects the stale signature and rebuilds from the library.
    assert _indexed_segment_count(state_path) > 0
    with open_state(state_path) as state:
        assert state.index_signature_row() == search.index_signature()


def test_deleting_a_library_row_deletes_its_index_rows(tmp_path: Path) -> None:
    state_path, bundle = _ingest(tmp_path)
    bundle_id = bundle.name
    with open_state(state_path) as state:
        assert state.indexed_segment_count(bundle_id=bundle_id) > 0
        state.forget_library_bundle(bundle_id)
        assert state.indexed_segment_count(bundle_id=bundle_id) == 0


def test_index_rows_carry_no_filesystem_path(tmp_path: Path) -> None:
    # The index is a new store of transcript text; it must not become a new
    # place for paths to accumulate.
    state_path, _ = _ingest(tmp_path)
    connection = sqlite3.connect(state_path)
    rows = connection.execute("SELECT bundle_id, segment_id, body FROM segment_index").fetchall()
    connection.close()
    assert rows
    for _, _, body in rows:
        assert str(tmp_path) not in body


def test_indexed_text_is_segmented(tmp_path: Path) -> None:
    """What is stored must be what a query will be compared against.

    Asserted on the stored text rather than on a retrieval result, so a failure
    points at the writer rather than at the whole pipeline.
    """

    state_path, bundle = _ingest(tmp_path)
    segments = json.loads((bundle / "transcript" / "segments.json").read_text(encoding="utf-8"))
    connection = sqlite3.connect(state_path)
    stored = connection.execute(
        "SELECT body FROM segment_index WHERE segment_id = ?", (segments[0]["id"],)
    ).fetchone()
    connection.close()
    assert stored is not None
    assert stored[0] == search.segment_text(segments[0]["text"])


@pytest.mark.parametrize("missing", ["segments", "bundle"])
def test_backfill_survives_an_unreadable_bundle(tmp_path: Path, missing: str) -> None:
    """A bundle that cannot be read must not abort the upgrade.

    An archive is exactly the place where one directory has been moved, renamed,
    or half-deleted. Refusing to open the store in that case would make a single
    stale row cost the user their whole library.
    """

    state_path, bundle = _ingest(tmp_path)
    _revert_to_v2(state_path)
    if missing == "segments":
        (bundle / "transcript" / "segments.json").unlink()
    else:
        for path in sorted(bundle.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        bundle.rmdir()

    with open_state(state_path) as state:
        assert state.indexed_segment_count() == 0
        assert state.unindexed_bundle_ids() != []


@pytest.mark.parametrize("malformed_id", [[0], {"nested": 0}])
def test_refresh_skips_malformed_segment_ids_without_blocking_store(
    tmp_path: Path, malformed_id: object
) -> None:
    """One malformed bundle must not make the entire local library unavailable."""

    state_path, bundle = _ingest(tmp_path)
    segments_path = bundle / "transcript" / "segments.json"
    segments = json.loads(segments_path.read_text(encoding="utf-8"))
    segments[0]["id"] = malformed_id
    segments_path.write_text(json.dumps(segments), encoding="utf-8")

    with open_state(state_path) as state:
        assert state.list_library()
        assert not [
            hit for hit in state.search_segments("knowledge") if hit.bundle_id == bundle.name
        ]


def test_refresh_rejects_the_whole_duplicate_id_document(tmp_path: Path) -> None:
    state_path, bundle = _ingest(tmp_path)
    segments_path = bundle / "transcript" / "segments.json"
    segments = json.loads(segments_path.read_text(encoding="utf-8"))
    segments.append({**segments[0], "text": "duplicate-id unique-marker"})
    segments_path.write_text(json.dumps(segments), encoding="utf-8")

    with open_state(state_path) as state:
        assert not state.search_segments("unique-marker")
        assert state.indexed_segment_count(bundle_id=bundle.name) == 0


def test_refresh_repairs_partial_cached_index_rows(tmp_path: Path) -> None:
    state_path, bundle = _ingest(tmp_path)
    with open_state(state_path) as state:
        expected = state.indexed_segment_count(bundle_id=bundle.name)
        state._connection.execute(  # pyright: ignore[reportPrivateUsage]
            "DELETE FROM segment_index WHERE rowid IN "
            "(SELECT rowid FROM segment_index WHERE bundle_id = ? LIMIT 1)",
            (bundle.name,),
        )
        state._connection.commit()  # pyright: ignore[reportPrivateUsage]

    with open_state(state_path) as state:
        assert state.indexed_segment_count(bundle_id=bundle.name) == expected


@pytest.mark.parametrize("column", ["segment_id", "display", "body"])
def test_refresh_repairs_poisoned_cached_index_rows(tmp_path: Path, column: str) -> None:
    state_path, bundle = _ingest(tmp_path)
    connection = sqlite3.connect(state_path)
    poison: object = "not-an-int" if column == "segment_id" else "forged unique-marker"
    connection.execute(
        f"UPDATE segment_index SET {column} = ? WHERE bundle_id = ?",  # noqa: S608
        (poison, bundle.name),
    )
    connection.commit()
    connection.close()

    with open_state(state_path) as state:
        assert not state.search_segments("forged unique-marker")
        assert state.search_segments("knowledge")


def test_index_rows_and_cached_digest_come_from_one_segments_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path, bundle = _ingest(tmp_path)
    with open_state(state_path) as state:

        def unexpected_second_read(_bundle_dir: Path) -> object:
            raise AssertionError("indexing reread segments after building rows")

        monkeypatch.setattr(state, "_segments_fingerprint", unexpected_second_read)
        assert state._index_bundle_segments(bundle.name, bundle) > 0  # pyright: ignore[reportPrivateUsage]


def test_refresh_contains_deep_malformed_json_to_one_bundle(tmp_path: Path) -> None:
    state_path, bundle = _ingest(tmp_path)
    segments_path = bundle / "transcript" / "segments.json"
    segments_path.write_text("[" * 100_000 + "0" + "]" * 100_000, encoding="utf-8")

    with open_state(state_path) as state:
        assert state.list_library()
        assert state.indexed_segment_count(bundle_id=bundle.name) == 0
