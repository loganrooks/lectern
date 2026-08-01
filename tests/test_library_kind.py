"""A library record's `kind`, reserved so the second one costs nothing.

The acceptance clause is that adding a second `kind` requires no response-shape
change. Asserting merely that a `kind` field exists would pass for a design that
cannot extend -- the field would be there and the second kind would still force
callers to change. So the test adds a second kind and compares shapes.
"""

from __future__ import annotations

from pathlib import Path

from lectern import cli
from lectern.automation import open_state
from lectern.records import LibraryBundle, LibraryKind

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC_TALK = FIXTURE_DIR / "synthetic_talk.wav"
SYNTHETIC_TRANSCRIPT = FIXTURE_DIR / "synthetic_talk.transcript.txt"


def _archive(tmp_path: Path) -> Path:
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    media = media_dir / "synthetic_talk.wav"
    media.write_bytes(SYNTHETIC_TALK.read_bytes())
    media.with_suffix(".transcript.txt").write_text(
        SYNTHETIC_TRANSCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    state_path = tmp_path / "state.sqlite"
    assert (
        cli.main(
            [
                "ingest",
                str(media),
                "--output",
                str(tmp_path / "bundles"),
                "--state",
                str(state_path),
            ]
        )
        == 0
    )
    return state_path


def test_library_record_declares_its_kind(tmp_path: Path) -> None:
    state_path = _archive(tmp_path)
    with open_state(state_path) as state:
        bundle = state.list_library()[0]
    assert bundle.kind is LibraryKind.RECORDING
    assert bundle.to_dict()["kind"] == "recording"


def test_adding_a_second_kind_changes_no_response_shape() -> None:
    """The clause, tested as written rather than as it is easy to satisfy."""

    recording = LibraryBundle(
        bundle_id="b1",
        bundle_path="/tmp/b1",
        source_id="s1",
        source_item_id="i1",
        queue_item_id="q1",
        created_at="2026-01-01T00:00:00+00:00",
        kind=LibraryKind.RECORDING,
    )
    # A second kind, constructed the same way and differing only in the
    # discriminator.
    other = LibraryBundle(
        bundle_id="b2",
        bundle_path="/tmp/b2",
        source_id="s1",
        source_item_id="i2",
        queue_item_id="q2",
        created_at="2026-01-01T00:00:00+00:00",
        kind=LibraryKind.NOTE,
    )

    assert other.to_dict().keys() == recording.to_dict().keys()
    assert other.to_dict()["kind"] != recording.to_dict()["kind"]


def test_kind_is_the_only_discriminator_a_caller_must_read() -> None:
    # If a caller had to branch on anything else to tell kinds apart, the
    # reservation would not have bought what it claims.
    assert {kind.value for kind in LibraryKind} >= {"recording", "note"}


def test_unknown_kinds_round_trip_as_the_default(tmp_path: Path) -> None:
    """A row written by a newer version must still be listable by an older one.

    Refusing to read it would make a forward-compatible field into a
    forward-incompatible one, which is the opposite of what reserving it is for.
    """

    state_path = _archive(tmp_path)
    with open_state(state_path) as state:
        state._connection.execute(  # pyright: ignore[reportPrivateUsage]
            "UPDATE library_bundles SET kind = 'kind-from-the-future'"
        )
        bundles = state.list_library()
    assert bundles[0].kind is LibraryKind.RECORDING
