"""Citations that say what happened to what they cited.

The shipped anchor form was `[t=MM:SS]`, which truncates to whole seconds: two
segments starting at 0.4s and 0.9s render identically, so the anchor is not
injective into segments. An earlier revision fixed that with
`{segment_id, start_s, text_sha256}` and called it injective -- which was
verified within one bundle and claimed across all of them, while `segment_id`
restarts for every transcript.

The second thing this module pins is that resolution is not a boolean. "Fails
loudly on any change" treats an integrity validator as though it were every
consumer. A deletion that renumbers a later segment leaves the cited words
exactly where they were; refusing to resolve that is not caution, it is a wrong
answer delivered confidently.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from lectern import cli, search
from lectern.automation import open_state
from lectern.bundle import MANIFEST_NAME
from lectern.records import AutomationError
from lectern.search import AnchorResolution

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC_TALK = FIXTURE_DIR / "synthetic_talk.wav"
SYNTHETIC_TRANSCRIPT = FIXTURE_DIR / "synthetic_talk.transcript.txt"
MULTISCRIPT = json.loads((FIXTURE_DIR / "multiscript_segments.json").read_text(encoding="utf-8"))[
    "cases"
]


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
    bundle = sorted(path for path in out.iterdir() if path.is_dir())[-1]
    return state_path, bundle


def _segments(bundle: Path) -> list[dict[str, Any]]:
    return json.loads((bundle / "transcript" / "segments.json").read_text(encoding="utf-8"))


def _write_segments(bundle: Path, segments: list[dict[str, Any]]) -> None:
    (bundle / "transcript" / "segments.json").write_text(json.dumps(segments), encoding="utf-8")


def test_anchor_includes_bundle_identity() -> None:
    """Two bundles can hold the same words at the same moment.

    Without bundle identity their anchors are equal, and a citation stored
    outside its search response names neither recording.
    """

    first = search.make_anchor("bundle-a", 0, 0.0, "Welcome")
    second = search.make_anchor("bundle-b", 0, 0.0, "Welcome")
    assert first != second
    assert first.bundle_id != second.bundle_id


def test_two_segments_in_one_second_are_distinguishable() -> None:
    # The case `[t=MM:SS]` loses: both render 00:00.
    early = search.make_anchor("bundle-a", 3, 0.4, "first")
    late = search.make_anchor("bundle-a", 4, 0.9, "second")
    assert early.rendered() == late.rendered() == "[t=00:00]"
    assert early != late


def test_anchor_records_its_canonicalization_version() -> None:
    anchor = search.make_anchor("bundle-a", 0, 0.0, "Welcome")
    assert anchor.canon_version == search.CANON_VERSION


def test_anchor_digest_ignores_cosmetic_text_changes() -> None:
    assert (
        search.make_anchor("b", 0, 0.0, "café  spoken").text_sha256
        == search.make_anchor("b", 0, 0.0, "café spoken").text_sha256
    )


def test_resolution_exact(tmp_path: Path) -> None:
    state_path, bundle = _archive(tmp_path)
    segment = _segments(bundle)[0]
    anchor = search.make_anchor(bundle.name, int(segment["id"]), 0.0, str(segment["text"]))
    with open_state(state_path) as state:
        resolved = state.resolve_anchor(anchor)
    assert resolved.outcome is AnchorResolution.EXACT
    assert resolved.segment_id == segment["id"]


def test_resolution_relocated_when_a_deletion_renumbers(tmp_path: Path) -> None:
    """The case a boolean resolution gets wrong.

    An earlier segment is removed, so the cited one shifts down by an index. Its
    words are unchanged and still present; reporting `missing` would be false.
    """

    state_path, bundle = _archive(tmp_path)
    segments = _segments(bundle)
    if len(segments) < 2:
        segments = segments + [
            {
                "id": 1,
                "start_s": 9.0,
                "end_s": 10.0,
                "text": "second line",
                "source": "fixture",
            }
        ]
        _write_segments(bundle, segments)
    cited = segments[-1]
    anchor = search.make_anchor(bundle.name, int(cited["id"]), 9.0, str(cited["text"]))

    _write_segments(bundle, [{**cited, "id": int(cited["id"]) - 1}])
    with open_state(state_path) as state:
        resolved = state.resolve_anchor(anchor)
    assert resolved.outcome is AnchorResolution.RELOCATED
    assert resolved.segment_id == int(cited["id"]) - 1


def test_resolution_modified_when_the_text_changes(tmp_path: Path) -> None:
    state_path, bundle = _archive(tmp_path)
    segment = _segments(bundle)[0]
    anchor = search.make_anchor(bundle.name, int(segment["id"]), 0.0, str(segment["text"]))

    _write_segments(bundle, [{**segment, "text": "corrected wording entirely"}])
    with open_state(state_path) as state:
        resolved = state.resolve_anchor(anchor)
    assert resolved.outcome is AnchorResolution.MODIFIED
    # The drift is shown, not hidden: a correction UI needs both texts.
    assert resolved.current_text == "corrected wording entirely"


def test_resolution_missing_when_the_segment_is_gone(tmp_path: Path) -> None:
    state_path, bundle = _archive(tmp_path)
    segment = _segments(bundle)[0]
    anchor = search.make_anchor(bundle.name, int(segment["id"]), 0.0, str(segment["text"]))

    _write_segments(bundle, [])
    with open_state(state_path) as state:
        resolved = state.resolve_anchor(anchor)
    assert resolved.outcome is AnchorResolution.MISSING


def test_resolution_missing_for_an_unknown_bundle(tmp_path: Path) -> None:
    state_path, _ = _archive(tmp_path)
    anchor = search.make_anchor("no-such-bundle", 0, 0.0, "Welcome")
    with open_state(state_path) as state:
        assert state.resolve_anchor(anchor).outcome is AnchorResolution.MISSING


def test_cite_renders_a_timestamp_and_stores_an_anchor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_path, bundle = _archive(tmp_path)
    capsys.readouterr()
    assert (
        cli.main(["library", "cite", bundle.name, "0", "--state", str(state_path), "--json"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["rendered"].startswith("[t=")
    # What is rendered is a timestamp; what is stored is the anchor. Storing the
    # rendering is what made the old form unresolvable.
    assert payload["anchor"]["bundle_id"] == bundle.name
    assert payload["anchor"]["text_sha256"]
    assert payload["outcome"] == "exact"


def test_plain_cite_serializes_distinct_resolvable_same_second_anchors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_path, bundle = _archive(tmp_path)
    _write_segments(
        bundle,
        [
            {
                "id": 0,
                "start_s": 0.4,
                "end_s": 0.6,
                "text": "first same-second segment",
                "source": "fixture",
            },
            {
                "id": 1,
                "start_s": 0.9,
                "end_s": 1.1,
                "text": "second same-second segment",
                "source": "fixture",
            },
        ],
    )
    segments_payload = (bundle / "transcript" / "segments.json").read_bytes()
    manifest_path = bundle / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for stage in manifest["stages"].values():
        for output in stage["outputs"]:
            if output["path"] == "transcript/segments.json":
                output["sha256"] = hashlib.sha256(segments_payload).hexdigest()
                output["bytes"] = len(segments_payload)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    capsys.readouterr()

    anchors: list[search.Anchor] = []
    rendered: list[str] = []
    for segment_id, expected_start in ((0, 0.4), (1, 0.9)):
        assert (
            cli.main(["library", "cite", bundle.name, str(segment_id), "--state", str(state_path)])
            == 0
        )
        output = capsys.readouterr().out.rstrip("\n")
        rendered_value, serialized_anchor, outcome = output.split("\t")
        anchor_data = json.loads(serialized_anchor)
        assert set(anchor_data) == {
            "bundle_id",
            "segment_id",
            "start_s",
            "text_sha256",
            "canon_version",
        }
        assert anchor_data["start_s"] == expected_start
        assert str(tmp_path) not in output
        assert outcome == AnchorResolution.EXACT.value
        rendered.append(rendered_value)
        anchors.append(search.Anchor(**anchor_data))

    assert rendered == ["[t=00:00]", "[t=00:00]"]
    assert anchors[0] != anchors[1]
    with open_state(state_path) as state:
        assert [state.resolve_anchor(anchor).outcome for anchor in anchors] == [
            AnchorResolution.EXACT,
            AnchorResolution.EXACT,
        ]


def test_cite_emits_no_filesystem_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state_path, bundle = _archive(tmp_path)
    capsys.readouterr()
    assert (
        cli.main(["library", "cite", bundle.name, "0", "--state", str(state_path), "--json"]) == 0
    )
    assert str(tmp_path) not in capsys.readouterr().out


def test_cite_refuses_malformed_segment_records(tmp_path: Path) -> None:
    state_path, bundle = _archive(tmp_path)
    _write_segments(bundle, [{"id": 0}])
    with (
        open_state(state_path) as state,
        pytest.raises(AutomationError, match="readable transcript"),
    ):
        state.cite_segment(bundle.name, 0)


@pytest.mark.parametrize("case", MULTISCRIPT, ids=[c["script"] for c in MULTISCRIPT])
def test_anchors_resolve_for_every_supported_script(tmp_path: Path, case: dict[str, Any]) -> None:
    """The Tier-A clause says the non-Latin fixture exercises indexing AND anchors.

    Indexing was covered by the search tests; anchors were not, so this closes
    the half of the clause that a Latin-only citation fixture leaves open. It
    matters beyond box-ticking: the digest is taken over canonicalized text, and
    NFC normalization is exactly the step most likely to behave differently
    outside Latin script.
    """

    state_path, bundle = _archive(tmp_path)
    _write_segments(
        bundle, [{"id": 0, "start_s": 1.0, "end_s": 2.0, "text": case["text"], "source": "fixture"}]
    )
    anchor = search.make_anchor(bundle.name, 0, 1.0, case["text"])
    with open_state(state_path) as state:
        resolved = state.resolve_anchor(anchor)
    assert resolved.outcome is AnchorResolution.EXACT, case["script"]
    assert resolved.current_text == case["text"]


@pytest.mark.parametrize("case", MULTISCRIPT, ids=[c["script"] for c in MULTISCRIPT])
def test_anchor_digests_are_script_independent(case: dict[str, Any]) -> None:
    """A decomposed rewrite must not break a citation in any script.

    Greek and Hangul both have composed and decomposed forms, so this is not a
    Latin-only concern -- and a citation that survives an editor's save in
    English while breaking in Korean would be a worse failure than one that
    broke everywhere, because nobody would notice it.
    """

    decomposed = unicodedata.normalize("NFD", case["text"])
    assert (
        search.make_anchor("b", 0, 0.0, decomposed).text_sha256
        == search.make_anchor("b", 0, 0.0, case["text"]).text_sha256
    )
