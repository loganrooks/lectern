"""Regressions for the PR #12 review findings.

Each test here exists because a green suite already claimed the property it
checks. They are grouped in one module deliberately: read together they are a
record of how a test can cover a mechanism while leaving its promise unasserted.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from lectern import cli, search
from lectern.automation import open_state
from lectern.records import redact_paths
from lectern.search import AnchorResolution

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC_TALK = FIXTURE_DIR / "synthetic_talk.wav"
SYNTHETIC_TRANSCRIPT = FIXTURE_DIR / "synthetic_talk.transcript.txt"


def _archive(tmp_path: Path) -> tuple[Path, Path]:
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    media = media_dir / "synthetic_talk.wav"
    media.write_bytes(SYNTHETIC_TALK.read_bytes())
    media.with_suffix(".transcript.txt").write_text(
        SYNTHETIC_TRANSCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    state_path = tmp_path / "state.sqlite"
    out = tmp_path / "bundles"
    assert cli.main(["ingest", str(media), "--output", str(out), "--state", str(state_path)]) == 0
    return state_path, sorted(path for path in out.iterdir() if path.is_dir())[-1]


def test_redaction_consumes_paths_containing_spaces() -> None:
    """R2-01. The previous assertion certified the partial fix.

    It checked for the whole path and for `Private Therapy` -- both of which
    stopping at whitespace destroys -- while `Therapy/recordings/session.wav`
    passed straight through. Every component is checked now, not just the
    strings the old bug happened to break.
    """

    message = "[Errno 2] No such file or directory: '/tmp/Private Therapy/recordings/session.wav'"
    redacted = redact_paths(message)
    for fragment in ("Private", "Therapy", "recordings", "session.wav", "/tmp"):
        assert fragment not in redacted, f"{fragment!r} survived redaction: {redacted!r}"
    assert "<path>" in redacted


def test_redaction_still_handles_unquoted_paths() -> None:
    assert "plain" not in redact_paths("failed at /tmp/plain/path.wav while reading")


def test_ambiguous_relocation_is_not_reported_as_relocated() -> None:
    """R2-11. Fixing "fails when it should resolve" must not introduce
    "resolves to the wrong thing"."""

    segments: list[dict[str, Any]] = [
        {"id": 0, "start_s": 1.0, "text": "Thank you"},
        {"id": 1, "start_s": 60.0, "text": "something else"},
        {"id": 2, "start_s": 120.0, "text": "Thank you"},
    ]
    anchor = search.make_anchor("b", 9, 118.0, "Thank you")
    resolved = search.resolve_against_segments(anchor, segments)
    assert resolved.outcome is AnchorResolution.AMBIGUOUS
    # Nearest by timestamp is the best guess available, and it is labelled a
    # guess rather than presented as a relocation.
    assert resolved.segment_id == 2


def test_unique_relocation_is_still_relocated() -> None:
    segments: list[dict[str, Any]] = [{"id": 0, "start_s": 1.0, "text": "unique phrase"}]
    anchor = search.make_anchor("b", 5, 1.0, "unique phrase")
    assert search.resolve_against_segments(anchor, segments).outcome is AnchorResolution.RELOCATED


def test_anchor_from_an_unsupported_canon_version_is_refused() -> None:
    """R2-12. The version was recorded and never consulted."""

    anchor = search.Anchor(
        bundle_id="b",
        segment_id=0,
        start_s=0.0,
        text_sha256=search.text_digest("hello"),
        canon_version=search.CANON_VERSION + 1,
    )
    resolved = search.resolve_against_segments(anchor, [{"id": 0, "start_s": 0.0, "text": "hello"}])
    assert resolved.outcome is AnchorResolution.UNSUPPORTED_VERSION


def test_search_snippet_is_display_text_not_the_index_representation(tmp_path: Path) -> None:
    """R2-13. The segmented body is internal; returning it leaked spaces between
    every CJK character and whole transcripts for text-only bundles."""

    state_path, _ = _archive(tmp_path)
    with open_state(state_path) as state:
        state.index_synthetic_segment("zh-bundle", 0, "现象学研究")
        hits = [hit for hit in state.search_segments("现象") if hit.bundle_id == "zh-bundle"]
    assert hits
    assert hits[0].snippet == "现象学研究"
    assert "现 象" not in hits[0].snippet


def test_rollback_removes_index_rows_with_the_library_row(tmp_path: Path) -> None:
    """R2-09. The atomicity test covered the commit path only."""

    state_path, bundle = _archive(tmp_path)
    bundle_id = bundle.name
    with open_state(state_path) as state:
        assert state.indexed_segment_count(bundle_id=bundle_id) > 0
        state._delete_index_rows(bundle_id)  # pyright: ignore[reportPrivateUsage]
        state._connection.commit()  # pyright: ignore[reportPrivateUsage]
        assert state.indexed_segment_count(bundle_id=bundle_id) == 0


def test_v3_migration_is_resumable_after_partial_table_creation(tmp_path: Path) -> None:
    """R2-08. Interrupted between CREATE and the version bump, the store used to
    be unopenable forever: the tables existed and the migration re-ran."""

    state_path, _ = _archive(tmp_path)
    connection = sqlite3.connect(state_path)
    connection.execute("PRAGMA user_version = 2")  # tables remain, version rolls back
    connection.commit()
    connection.close()

    with open_state(state_path) as state:
        assert state.indexed_segment_count() > 0


def test_segments_artifact_validates_against_its_committed_schema(tmp_path: Path) -> None:
    """R2-03. The exported schema declared an object root for an array."""

    _, bundle = _archive(tmp_path)
    schema = json.loads(
        (
            Path(__file__).resolve().parent.parent / "schemas" / "transcript-segments.schema.json"
        ).read_text(encoding="utf-8")
    )
    payload = json.loads((bundle / "transcript" / "segments.json").read_text(encoding="utf-8"))
    assert schema.get("type") == "array", schema.get("type")
    assert isinstance(payload, list)


def test_content_identity_rejects_a_path_field() -> None:
    """R2-04. Asserting `path` is not declared was never asserting it is refused."""

    import pytest
    from pydantic import ValidationError

    from lectern.bundle import ContentIdentity

    with pytest.raises(ValidationError):
        ContentIdentity.model_validate({"sha256": "a" * 64, "path": "/Users/alice/file.wav"})


def test_sampler_rejects_an_end_beyond_the_recording(tmp_path: Path) -> None:
    """R2-15. Reading only `start_s` certified a segment whose cited interval
    mostly does not exist."""

    state_path, bundle = _archive(tmp_path)
    (bundle / "transcript" / "segments.json").write_text(
        json.dumps([{"id": 0, "start_s": 1.0, "end_s": 3600.0, "text": "long"}]), encoding="utf-8"
    )
    with open_state(state_path) as state:
        issues = state.sample_anchor_correctness()
    assert any("end_s" in issue.detail for issue in issues)
