"""Guards for resource lifetime and walk termination in the automation spine.

These cover paths the existing suite does not observe: connections that are
released only by refcounting, and an ancestor walk whose termination condition
assumes its input is under the root. Each is cheap today and load-bearing the
moment a long-lived process (an MCP server, a resident companion) holds a store
open or feeds the walk a path from outside the source tree.
"""

from __future__ import annotations

import json
import signal
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any

import pytest
from _pytest.monkeypatch import MonkeyPatch

from lectern import automation
from lectern.automation import (
    STATE_SCHEMA_VERSION,
    AutomationError,
    SourceKind,
    SourcePolicy,
    SourceRecord,
    YouTubePlaylistAdapter,
    open_state,
    preflight_state_store,
)


@contextmanager
def time_budget(seconds: float) -> Generator[None]:
    """Fail, rather than hang, when the body does not terminate.

    A non-terminating walk is otherwise indistinguishable from a slow test: the
    run never ends and no assertion is ever reported. `SIGALRM` turns the red
    state into an ordinary test failure. Both CI runners are POSIX.
    """

    def on_alarm(signum: int, frame: FrameType | None) -> None:
        raise TimeoutError(f"did not terminate within {seconds}s")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def recording_connect_factory(
    opened: list[sqlite3.Connection],
) -> object:
    """Return a `sqlite3.connect` stand-in that keeps every connection alive.

    Holding the references is the point: without them CPython's refcounting
    closes an abandoned connection at scope exit, which is exactly the effect
    these tests must not be fooled by.
    """

    real_connect = sqlite3.connect

    def recording_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)  # pyright: ignore[reportUnknownArgumentType]
        opened.append(connection)
        return connection

    return recording_connect


def assert_closed(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("PRAGMA user_version")


def test_preflight_state_store_closes_its_connection(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state_path = tmp_path / "state.sqlite"
    with open_state(state_path):
        pass

    opened: list[sqlite3.Connection] = []
    monkeypatch.setattr(sqlite3, "connect", recording_connect_factory(opened))
    result = preflight_state_store(state_path)
    monkeypatch.undo()

    assert result.schema_version == STATE_SCHEMA_VERSION
    assert result.error is None
    # A `sqlite3.Connection` context manager commits or rolls back; it does not
    # close. The read-only handle must be released explicitly.
    assert len(opened) == 1
    assert_closed(opened[0])


def test_failed_migration_closes_the_state_connection(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    def failing_migrate(self: automation.AutomationState) -> None:
        raise AutomationError("synthetic migration failure")

    opened: list[sqlite3.Connection] = []
    monkeypatch.setattr(sqlite3, "connect", recording_connect_factory(opened))
    monkeypatch.setattr(automation.AutomationState, "_migrate", failing_migrate)

    with pytest.raises(AutomationError, match="synthetic migration failure"):
        automation.AutomationState(tmp_path / "state.sqlite")

    monkeypatch.undo()

    # The store never became usable, so no caller holds it to close: the failed
    # constructor is the only place that can release the handle.
    assert len(opened) == 1
    assert_closed(opened[0])


def test_bundle_output_walk_terminates_for_path_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    outside = outside_dir / "clip.wav"
    outside.write_bytes(b"")

    # Walking up from `outside` never meets `root`, and the filesystem root is
    # its own parent, so the only other stopping condition is the guard.
    with time_budget(5.0):
        assert automation.is_bundle_output_path(root, outside) is False


def playlist_page(video_id: str) -> bytes:
    payload: dict[str, Any] = {
        "items": [
            {
                "id": f"PLI_{video_id}",
                "snippet": {
                    "title": f"Synthetic {video_id}",
                    "channelId": "UC_SYNTH",
                    "channelTitle": "Synthetic Channel",
                    "publishedAt": "2026-01-01T00:00:00Z",
                    "videoOwnerChannelId": "UC_OWNER",
                    "videoOwnerChannelTitle": "Synthetic Owner",
                    "position": 0,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                },
                "contentDetails": {
                    "videoId": video_id,
                    "videoPublishedAt": "2026-01-01T00:00:00Z",
                },
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


def youtube_source() -> SourceRecord:
    return SourceRecord(
        id="src_synthetic",
        kind=SourceKind.YOUTUBE_PLAYLIST,
        name="synthetic-playlist",
        root_path="PL_SYNTH",
        policy=SourcePolicy.SCAN_ONLY,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def test_reused_adapter_reports_no_stale_quota_after_a_failed_rescan() -> None:
    """Characterization test: green before this slice as well as after.

    The reset it pins was added in the M4a defect slice, so nothing here is a
    repair claim. Its job is to make the property fail loudly if a later move
    of the adapter drops the reset: a reused adapter whose second scan raises
    must not still be reporting the first scan's quota figures to a caller that
    reads `scan_metadata` after catching the error.
    """

    calls: list[str] = []

    def transport(url: str, timeout_s: float) -> bytes:
        calls.append(url)
        if len(calls) == 1:
            return playlist_page("VIDEO_1")
        raise AutomationError("synthetic transport failure")

    adapter = YouTubePlaylistAdapter("fake-secret", transport=transport)
    source = youtube_source()

    items = adapter.discover(source)
    assert [item.relative_path for item in items] == ["PL_SYNTH/VIDEO_1"]
    first_quota = adapter.scan_metadata["quota"]
    assert first_quota["pages_fetched"] == 1
    assert first_quota["estimated_units_consumed"] == 1
    assert adapter.pages_attempted == 1

    with pytest.raises(AutomationError, match="synthetic transport failure"):
        adapter.discover(source)

    # No stale figures survive the failed run: metadata is empty (this scan
    # never completed) and the attempt counter describes this scan only.
    assert adapter.scan_metadata == {}
    assert adapter.pages_attempted == 1
    assert adapter.attempted_quota_units == 1


def test_bundle_output_walk_rejects_a_bundle_outside_root(tmp_path: Path) -> None:
    """A bundle that is not under `root` is not this root's output.

    The marker check runs before the walk can discover it never reached `root`,
    so a path outside `root` that happens to sit inside some *other* Lectern
    bundle would otherwise be reported as this root's bundle output.
    """

    root = tmp_path / "root"
    root.mkdir()
    other_bundle = tmp_path / "elsewhere" / "some-bundle"
    other_bundle.mkdir(parents=True)
    (other_bundle / "manifest.json").write_text("{}", encoding="utf-8")
    (other_bundle / "source.json").write_text("{}", encoding="utf-8")
    outside = other_bundle / "media" / "audio.wav"
    outside.parent.mkdir()
    outside.write_bytes(b"")

    with time_budget(5.0):
        assert automation.is_bundle_output_path(root, outside) is False


def test_bundle_output_walk_still_finds_a_bundle_under_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    bundle = root / "bundles" / "talk-0001"
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text("{}", encoding="utf-8")
    (bundle / "source.json").write_text("{}", encoding="utf-8")
    inside = bundle / "media" / "audio.wav"
    inside.parent.mkdir()
    inside.write_bytes(b"")

    with time_budget(5.0):
        assert automation.is_bundle_output_path(root, inside) is True
