"""Resolving to a segment is not the same as resolving to a real moment.

A transcriber can emit a segment at 3600s for a thirty-second recording. Ingest
validates that timestamps are finite, non-negative, and ordered within a segment
-- but not that they fit the recording. Such a segment is accepted, indexed, and
its anchor resolves `exact`, because the database lookup succeeds. A player then
cannot navigate to the cited evidence, because the moment does not exist.

So the sampler checks the property the milestone actually promises, rather than
the one the lookup happens to test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lectern import cli
from lectern.automation import open_state
from lectern.search import AnchorIssue

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


def _write_segments(bundle: Path, segments: list[dict[str, Any]]) -> None:
    valid_records = [
        {**segment, "source": segment.get("source", "fixture")} for segment in segments
    ]
    (bundle / "transcript" / "segments.json").write_text(
        json.dumps(valid_records), encoding="utf-8"
    )


def test_sampler_is_green_on_a_valid_bundle(tmp_path: Path) -> None:
    state_path, _ = _archive(tmp_path)
    with open_state(state_path) as state:
        assert state.sample_anchor_correctness() == []


def test_sampler_reports_an_unreadable_registered_bundle(tmp_path: Path) -> None:
    state_path, bundle = _archive(tmp_path)
    (bundle / "transcript" / "segments.json").write_text("{}", encoding="utf-8")
    with open_state(state_path) as state:
        issues = state.sample_anchor_correctness()
    assert {issue.kind for issue in issues} == {AnchorIssue.UNREADABLE}
    assert issues[0].bundle_id == bundle.name


def test_sampler_rejects_a_timestamp_past_the_recording(tmp_path: Path) -> None:
    state_path, bundle = _archive(tmp_path)
    _write_segments(bundle, [{"id": 0, "start_s": 3600.0, "end_s": 3601.0, "text": "impossible"}])
    with open_state(state_path) as state:
        issues = state.sample_anchor_correctness()
    # Membership rather than an exact list: the same segment can be out of
    # bounds at both ends, and asserting one issue would pin the sampler to
    # reporting less than it finds.
    assert {issue.kind for issue in issues} == {AnchorIssue.OUT_OF_BOUNDS}
    assert issues


def test_sampler_rejects_disordered_segments(tmp_path: Path) -> None:
    """Order is part of what makes a transcript navigable, not a nicety."""

    state_path, bundle = _archive(tmp_path)
    _write_segments(
        bundle,
        [
            {"id": 0, "start_s": 2.0, "end_s": 3.0, "text": "second"},
            {"id": 1, "start_s": 0.5, "end_s": 1.0, "text": "first"},
        ],
    )
    with open_state(state_path) as state:
        issues = state.sample_anchor_correctness()
    assert AnchorIssue.OUT_OF_ORDER in {issue.kind for issue in issues}


def test_sampler_rejects_a_negative_timestamp(tmp_path: Path) -> None:
    state_path, bundle = _archive(tmp_path)
    _write_segments(bundle, [{"id": 0, "start_s": -1.0, "end_s": 1.0, "text": "before the start"}])
    with open_state(state_path) as state:
        issues = state.sample_anchor_correctness()
    assert AnchorIssue.OUT_OF_BOUNDS in {issue.kind for issue in issues}


def test_sampler_reports_which_bundle_and_segment(tmp_path: Path) -> None:
    # An issue nobody can locate is an issue nobody can fix.
    state_path, bundle = _archive(tmp_path)
    _write_segments(bundle, [{"id": 7, "start_s": 9999.0, "end_s": 10000.0, "text": "x"}])
    with open_state(state_path) as state:
        issue = state.sample_anchor_correctness()[0]
    assert issue.bundle_id == bundle.name
    assert issue.segment_id == 7


def test_sampler_tolerates_a_bundle_without_a_declared_duration(tmp_path: Path) -> None:
    """Absence of a duration is not evidence of a bad timestamp.

    Reporting an issue here would make the sampler noisy on exactly the bundles
    whose provenance is weakest, which is where a real signal would be hardest
    to see.
    """

    state_path, bundle = _archive(tmp_path)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source"]["duration_s"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _write_segments(bundle, [{"id": 0, "start_s": 5.0, "end_s": 6.0, "text": "fine"}])
    with open_state(state_path) as state:
        assert state.sample_anchor_correctness() == []
