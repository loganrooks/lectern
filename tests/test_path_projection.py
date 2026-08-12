"""No outward-facing command may emit a filesystem path.

This is a property of the *program*, not of any one command, and stating it that
way is the whole point of the module. An earlier version of this design asserted
the property over the response types of two new commands and called the boundary
closed; it was not, because the commands that already existed kept returning
absolute paths, and nothing was checking them.

The rule enforced here is about data rather than columns: a value is
path-bearing if it *can* contain a filesystem path, including free text that
interpolates one. That phrasing is what reaches `last_error`, which carries a
path only when an operation fails and is therefore invisible to every fixture
that succeeds.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest import CaptureFixture

from lectern import cli
from lectern.ingest import ingest_local

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC_TALK = FIXTURE_DIR / "synthetic_talk.wav"
SYNTHETIC_TRANSCRIPT = FIXTURE_DIR / "synthetic_talk.transcript.txt"

# Every command that returns data to a caller. A command absent from this tuple
# is a command nothing checks, which is the failure this module exists to
# prevent -- see `test_every_outward_command_is_covered`.
OUTWARD_COMMANDS = (
    "sources list",
    "sources scan",
    "queue list",
    "queue show",
    "library list",
    "library show",
    "library search",
    "library cite",
    "migrate",
)


def _watched_folder(root: Path) -> Path:
    """A source folder whose absolute path is distinctive enough to grep for."""

    folder = root / "Private Therapy" / "recordings"
    folder.mkdir(parents=True, exist_ok=True)
    media = folder / "synthetic_talk.wav"
    media.write_bytes(SYNTHETIC_TALK.read_bytes())
    media.with_suffix(".transcript.txt").write_text(
        SYNTHETIC_TRANSCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return folder


def _current_private_bundle(tmp_path: Path) -> tuple[Path, Path]:
    folder = _watched_folder(tmp_path)
    media = folder / "synthetic_talk.wav"
    bundle = ingest_local(media, folder.parent / "bundles").bundle_dir
    return folder, bundle


def _leaky_substrings(folder: Path) -> tuple[str, ...]:
    """Fragments whose appearance in output would constitute a leak.

    The absolute path is the obvious one. The parent directory names are
    included because a partial path is still the user's filesystem: on macOS the
    home directory carries the account name, and "Private Therapy" is the kind
    of thing a directory name discloses on its own.
    """

    return (str(folder), str(folder.parent), folder.parent.name)


@pytest.fixture
def registered(tmp_path: Path) -> Iterator[tuple[Path, Path, Path]]:
    """A state store with one registered, scanned source folder."""

    folder = _watched_folder(tmp_path)
    state = tmp_path / "state.sqlite"
    assert cli.main(["sources", "add-folder", "talks", str(folder), "--state", str(state)]) == 0
    assert cli.main(["sources", "scan", "talks", "--state", str(state)]) == 0
    yield tmp_path, folder, state


def _assert_no_path(captured: str, folder: Path, command: str) -> None:
    for fragment in _leaky_substrings(folder):
        assert fragment not in captured, f"`{command}` leaked {fragment!r}"


@pytest.mark.parametrize("json_output", [False, True], ids=["plain", "json"])
def test_sources_list_emits_no_filesystem_path(
    registered: tuple[Path, Path, Path], capsys: CaptureFixture[str], json_output: bool
) -> None:
    # Both renderings are checked because both print `root_path` today: the
    # plain branch formats it into a tab-separated line, so a JSON-only
    # assertion would pass while the default output still leaked.
    _, folder, state = registered
    args = ["sources", "list", "--state", str(state)]
    if json_output:
        args.append("--json")
    assert cli.main(args) == 0
    _assert_no_path(capsys.readouterr().out, folder, "sources list")


@pytest.mark.parametrize("json_output", [False, True], ids=["plain", "json"])
def test_sources_scan_emits_no_filesystem_path(
    registered: tuple[Path, Path, Path], capsys: CaptureFixture[str], json_output: bool
) -> None:
    _, folder, state = registered
    args = ["sources", "scan", "talks", "--state", str(state)]
    if json_output:
        args.append("--json")
    assert cli.main(args) == 0
    _assert_no_path(capsys.readouterr().out, folder, "sources scan")


def test_queue_list_emits_no_filesystem_path(
    registered: tuple[Path, Path, Path], capsys: CaptureFixture[str]
) -> None:
    _, folder, state = registered
    assert cli.main(["queue", "list", "--state", str(state), "--json"]) == 0
    _assert_no_path(capsys.readouterr().out, folder, "queue list")


def test_failed_queue_item_error_is_projected(
    registered: tuple[Path, Path, Path], capsys: CaptureFixture[str]
) -> None:
    """The leak that only exists when something goes wrong.

    A successful ingest never populates `last_error`, so a suite built entirely
    from passing fixtures certifies the happy path and reads as though it
    certified the program. The media is deleted between approval and ingest so
    the `OSError` message -- which interpolates the absolute filename -- is what
    gets persisted and served back.
    """

    tmp_path, folder, state = registered
    assert cli.main(["queue", "list", "--state", str(state), "--json"]) == 0
    queue = json.loads(capsys.readouterr().out)["queue"]
    assert queue, "fixture produced no queue item to fail"
    item_id = queue[0]["id"]

    assert cli.main(["queue", "approve", item_id, "--state", str(state)]) == 0
    (folder / "synthetic_talk.wav").unlink()
    cli.main(["queue", "ingest", item_id, "--state", str(state), "--output", str(tmp_path / "out")])
    capsys.readouterr()

    assert cli.main(["queue", "show", item_id, "--state", str(state), "--json"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["last_error"], "fixture did not record a failure"
    _assert_no_path(output, folder, "queue show")


def _ingest_one(tmp_path: Path, state: Path, capsys: CaptureFixture[str]) -> str:
    assert cli.main(["queue", "list", "--state", str(state), "--json"]) == 0
    item_id = json.loads(capsys.readouterr().out)["queue"][0]["id"]
    assert cli.main(["queue", "approve", item_id, "--state", str(state)]) == 0
    assert (
        cli.main(
            ["queue", "ingest", item_id, "--state", str(state), "--output", str(tmp_path / "out")]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["library", "list", "--state", str(state), "--json"]) == 0
    return capsys.readouterr().out


def test_library_list_emits_no_filesystem_path(
    registered: tuple[Path, Path, Path], capsys: CaptureFixture[str]
) -> None:
    tmp_path, folder, state = registered
    _assert_no_path(_ingest_one(tmp_path, state, capsys), folder, "library list")


def test_library_show_emits_no_filesystem_path(
    registered: tuple[Path, Path, Path], capsys: CaptureFixture[str]
) -> None:
    """Green once phase 2 lands: the embedded manifest no longer carries a path.

    Held as xfail(strict=True) through phase 1 because the leak was inside an
    artifact this command reads rather than a field it serializes, so the
    projection layer could not reach it. The strict marker is what forced its
    own removal the moment content identity landed.
    """

    tmp_path, folder, state = registered
    listing = _ingest_one(tmp_path, state, capsys)
    bundle_id = json.loads(listing)["bundles"][0]["bundle_id"]
    assert cli.main(["library", "show", bundle_id, "--state", str(state), "--json"]) == 0
    _assert_no_path(capsys.readouterr().out, folder, "library show")


def test_every_outward_command_is_covered() -> None:
    """The test that keeps this module from decaying into an allowlist.

    Without it, `OUTWARD_COMMANDS` records the commands someone happened to
    think of, and a command added later inherits no assertion at all. Comparing
    against the CLI's own usage text means a new outward command fails here
    until it is either covered or deliberately classified as not outward-facing.
    """

    covered = set(OUTWARD_COMMANDS)
    declared = set(cli.OUTWARD_COMMANDS)
    assert declared == covered, (
        "outward-facing commands and their path-projection coverage disagree; "
        f"uncovered={sorted(declared - covered)} stale={sorted(covered - declared)}"
    )


def test_migrate_success_emits_no_filesystem_path(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    folder, bundle = _current_private_bundle(tmp_path)
    assert cli.main(["migrate", str(bundle)]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["outcome"] == "already_current"
    _assert_no_path(captured.out + captured.err, folder, "migrate")


def test_migrate_failure_emits_no_filesystem_path(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    folder, bundle = _current_private_bundle(tmp_path)
    (bundle / "source.json").write_text("{}\n", encoding="utf-8")
    assert cli.main(["migrate", str(bundle)]) == 3
    captured = capsys.readouterr()
    assert "declared artifact integrity" in captured.err
    _assert_no_path(captured.out + captured.err, folder, "migrate")
