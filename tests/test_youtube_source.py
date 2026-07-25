from __future__ import annotations

import email.message
import http.client
import json
import sqlite3
import stat
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from pytest import CaptureFixture, MonkeyPatch

from lectern import automation, cli
from lectern.automation import (
    STATE_SCHEMA_VERSION,
    YOUTUBE_METADATA_ONLY_ERROR,
    AutomationError,
    QueueItem,
    QueueState,
    SourceKind,
    SourcePolicy,
    SourceRecord,
    YouTubeAPIError,
    YouTubePlaylistAdapter,
    normalize_youtube_playlist_id,
    open_state,
    preflight_state_store,
    preflight_youtube_playlist,
)


class FakeTransport:
    def __init__(self, responses: Sequence[bytes | Exception]) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url: str, timeout_s: float) -> bytes:
        del timeout_s
        self.urls.append(url)
        if not self.responses:
            raise AssertionError("fake transport received unexpected request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_v1_state_migrates_to_v2_and_preserves_existing_rows(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    _create_v1_state(state_path, tmp_path)

    with open_state(state_path) as state:
        source = state.get_source("src_legacy")
        item = state.get_source_item("item_legacy")
        queue = state.get_queue_item("queue_legacy")
        library = state.get_library_bundle("legacy-bundle")

    with sqlite3.connect(state_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        source_metadata = connection.execute(
            "SELECT metadata_json FROM source_items WHERE id = 'item_legacy'"
        ).fetchone()[0]
        queue_metadata = connection.execute(
            "SELECT metadata_json FROM queue_items WHERE id = 'queue_legacy'"
        ).fetchone()[0]

    assert version == STATE_SCHEMA_VERSION
    assert source.name == "legacy"
    assert item.relative_path == "synthetic_talk.wav"
    assert item.metadata == {}
    assert queue.state is QueueState.DISCOVERED
    assert queue.metadata == {}
    assert library.bundle_id == "legacy-bundle"
    assert source_metadata == "{}"
    assert queue_metadata == "{}"


def test_preflight_state_store_accepts_migratable_v1_store(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    _create_v1_state(state_path, tmp_path)

    preflight = preflight_state_store(state_path)

    assert preflight.ok
    assert preflight.schema_version == 1
    assert preflight.error is None


def test_youtube_scan_records_metadata_and_repeat_scan_is_idempotent(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"

    with open_state(state_path) as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        first = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item(), _beta_item()])]),
            ),
        )
        second = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item(), _beta_item()])]),
            ),
        )
        queue_items = state.list_queue()

    assert first.metadata["quota"]["estimated_units_consumed"] == 1
    assert [item.metadata["video"]["id"] for item in first.added] == [
        "vid_alpha",
        "vid_beta",
    ]
    assert [item.metadata["playlist"]["id"] for item in first.added] == [
        "PL_SYNTH",
        "PL_SYNTH",
    ]
    assert len(first.queued) == 2
    assert second.added == []
    assert second.changed == []
    assert len(second.unchanged) == 2
    assert second.queued == []
    assert [item.metadata["video"]["id"] for item in queue_items] == [
        "vid_alpha",
        "vid_beta",
    ]
    assert all(item.metadata["discovery"]["method"] == "playlistItems.list" for item in queue_items)


def test_youtube_partial_scan_failure_does_not_mutate_existing_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    failure = YouTubeAPIError(
        "YouTube Data API error (500 backendError): backend failed",
        status_code=500,
        reason="backendError",
    )

    with open_state(state_path) as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        first = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport(
                    [
                        _playlist_page([_alpha_item()], next_page_token="NEXT"),
                        _playlist_page([_beta_item()]),
                    ]
                ),
            ),
        )

        with pytest.raises(YouTubeAPIError, match="backendError"):
            state.scan_source(
                source.id,
                adapter=YouTubePlaylistAdapter(
                    "fake-secret",
                    transport=FakeTransport(
                        [
                            _playlist_page([_alpha_item()], next_page_token="NEXT"),
                            failure,
                        ]
                    ),
                ),
            )

        after_items = [state.get_source_item(item.id) for item in first.added]
        queue_items = state.list_queue()

    assert [item.present for item in after_items] == [True, True]
    assert len(queue_items) == 2
    assert [item.state for item in queue_items] == [QueueState.DISCOVERED, QueueState.DISCOVERED]


def test_malformed_next_page_token_fails_scan_without_mutating_state(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        first = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport(
                    [
                        _playlist_page([_alpha_item()], next_page_token="NEXT"),
                        _playlist_page([_beta_item()]),
                    ]
                ),
            ),
        )

        # A non-null, non-string token is not an end-of-playlist signal: treating
        # it as one would commit a partial scan as complete and mark the pages
        # never fetched as removed.
        with pytest.raises(AutomationError, match="malformed nextPageToken"):
            state.scan_source(
                source.id,
                adapter=YouTubePlaylistAdapter(
                    "fake-secret",
                    transport=FakeTransport([_page_with_raw_next_page_token([_alpha_item()], 123)]),
                ),
            )

        after_items = [state.get_source_item(item.id) for item in first.added]
        queue_items = state.list_queue()

    assert [item.present for item in after_items] == [True, True]
    assert len(queue_items) == 2
    assert [item.state for item in queue_items] == [QueueState.DISCOVERED, QueueState.DISCOVERED]


def test_real_video_with_placeholder_title_is_queued(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        delta = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport(
                    [
                        _playlist_page(
                            [
                                _playlist_item(
                                    playlist_item_id="pli_titled",
                                    video_id="vid_titled",
                                    title="Private video",
                                    channel_id="chan_alpha",
                                    channel_title="Synthetic Alpha Channel",
                                    video_owner_channel_id="owner_chan_alpha",
                                    video_owner_channel_title="Synthetic Alpha Owner Channel",
                                    position=0,
                                ),
                                _placeholder_item(),
                            ]
                        )
                    ]
                ),
            ),
        )
        queue_items = state.list_queue()

    # A real public video may legitimately be titled "Private video"; only the
    # tombstone shape (no owner channel, no videoPublishedAt) is a placeholder.
    assert [item.metadata["video"]["placeholder"] for item in delta.added] == [False, True]
    assert [item.metadata["video"]["id"] for item in queue_items] == ["vid_titled"]


def test_youtube_reorder_does_not_change_digest_or_requeue(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport(
                    [_playlist_page([_alpha_item(position=0), _beta_item(position=1)])]
                ),
            ),
        )

        reordered = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport(
                    [_playlist_page([_alpha_item(position=10), _beta_item(position=11)])]
                ),
            ),
        )

    assert reordered.added == []
    assert reordered.changed == []
    assert len(reordered.unchanged) == 2
    assert reordered.queued == []


def test_youtube_preflight_reports_missing_key_and_quota_failure() -> None:
    missing = preflight_youtube_playlist("PL_SYNTH", environ={})
    quota = preflight_youtube_playlist(
        "PL_SYNTH",
        api_key="fake-secret",
        transport=FakeTransport(
            [
                YouTubeAPIError(
                    "YouTube Data API error (403 quotaExceeded): quota exceeded",
                    status_code=403,
                    reason="quotaExceeded",
                )
            ]
        ),
    )

    assert not missing.ok
    assert missing.credential_present is False
    assert "YOUTUBE_API_KEY" in str(missing.error)
    assert not quota.ok
    assert quota.credential_present is True
    assert "quotaExceeded" in str(quota.error)


def test_youtube_preflight_reports_attempted_units_on_api_error() -> None:
    failed = preflight_youtube_playlist(
        "PL_SYNTH",
        api_key="fake-secret",
        transport=FakeTransport(
            [
                YouTubeAPIError(
                    "YouTube Data API error (403 quotaExceeded): quota exceeded",
                    status_code=403,
                    reason="quotaExceeded",
                )
            ]
        ),
    )

    # The page request was issued before the endpoint failed, so its quota unit
    # was spent; reporting zero would contradict the documented accounting.
    assert not failed.ok
    assert failed.pages_checked == 1
    assert failed.estimated_units_consumed == 1


def test_youtube_preflight_reports_attempted_units_on_malformed_response() -> None:
    malformed = preflight_youtube_playlist(
        "PL_SYNTH",
        api_key="fake-secret",
        transport=FakeTransport([b'{"items": ']),
    )

    assert not malformed.ok
    assert "was not valid" in str(malformed.error)
    assert malformed.pages_checked == 1
    assert malformed.estimated_units_consumed == 1


def test_youtube_preflight_reports_no_units_for_prerequest_failures() -> None:
    missing_key = preflight_youtube_playlist("PL_SYNTH", environ={})
    bad_playlist = preflight_youtube_playlist("not a playlist", api_key="fake-secret")

    assert missing_key.pages_checked == 0
    assert missing_key.estimated_units_consumed == 0
    assert bad_playlist.pages_checked == 0
    assert bad_playlist.estimated_units_consumed == 0


def test_youtube_preflight_success_reports_single_unit() -> None:
    reachable = preflight_youtube_playlist(
        "PL_SYNTH",
        api_key="fake-secret",
        transport=FakeTransport([_playlist_page([_alpha_item()])]),
    )

    assert reachable.ok
    assert reachable.pages_checked == 1
    assert reachable.estimated_units_consumed == 1


def test_youtube_adapter_reports_attempted_units_after_failed_page() -> None:
    adapter = YouTubePlaylistAdapter(
        "fake-secret",
        transport=FakeTransport([b'{"items": ']),
        max_pages=1,
    )
    source = _youtube_source_record("PL_SYNTH")

    with pytest.raises(AutomationError, match="was not valid"):
        adapter.discover(source)

    assert adapter.pages_attempted == 1
    assert adapter.attempted_quota_units == 1


def test_youtube_queue_ingest_is_metadata_only(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        queue_item = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item()])]),
            ),
        ).queued[0]
        approved = state.approve_queue_item(queue_item.id)

        with pytest.raises(AutomationError, match="metadata-only discovery"):
            state.ingest_queue_item(approved.id, tmp_path / "bundles")
        unsupported = state.get_queue_item(approved.id)

    assert unsupported.state is QueueState.UNSUPPORTED
    assert unsupported.last_error == YOUTUBE_METADATA_ONLY_ERROR
    assert unsupported.attempts == 0


def test_unsupported_queue_item_rejects_retry_and_approve(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        queue_item = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item()])]),
            ),
        ).queued[0]
        approved = state.approve_queue_item(queue_item.id)
        with pytest.raises(AutomationError, match="metadata-only discovery"):
            state.ingest_queue_item(approved.id, tmp_path / "bundles")

        with pytest.raises(AutomationError, match="terminal state"):
            state.retry_queue_item(approved.id)
        with pytest.raises(AutomationError, match="terminal state"):
            state.approve_queue_item(approved.id)
        after = state.get_queue_item(approved.id)

    assert after.state is QueueState.UNSUPPORTED
    assert after.attempts == 0


def test_unsupported_queue_item_rejects_skip(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        queue_item = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item()])]),
            ),
        ).queued[0]
        approved = state.approve_queue_item(queue_item.id)
        with pytest.raises(AutomationError, match="metadata-only discovery"):
            state.ingest_queue_item(approved.id, tmp_path / "bundles")

        # Skip must be guarded like retry/approve: UNSUPPORTED -> SKIPPED -> APPROVED
        # would otherwise walk the item straight back into the unsupported cycle.
        with pytest.raises(AutomationError, match="terminal state"):
            state.skip_queue_item(approved.id)
        after = state.get_queue_item(approved.id)

    assert after.state is QueueState.UNSUPPORTED
    assert after.attempts == 0


def test_terminal_guard_holds_when_precheck_reads_a_stale_state(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    real_get_queue_item = automation.AutomationState.get_queue_item
    stale_reads = [0]

    def stale_once_get_queue_item(
        self: automation.AutomationState,
        queue_item_id: str,
    ) -> QueueItem:
        queue_item = real_get_queue_item(self, queue_item_id)
        if stale_reads[0] > 0:
            stale_reads[0] -= 1
            return replace(queue_item, state=QueueState.DISCOVERED)
        return queue_item

    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        queue_item = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item()])]),
            ),
        ).queued[0]
        approved = state.approve_queue_item(queue_item.id)
        with pytest.raises(AutomationError, match="metadata-only discovery"):
            state.ingest_queue_item(approved.id, tmp_path / "bundles")

        # Stand in for a concurrent writer that marks the row UNSUPPORTED after the
        # guard's read and before its write: the pre-check sees a nonterminal state
        # that the row no longer holds, so only a conditional update can refuse it.
        monkeypatch.setattr(
            automation.AutomationState,
            "get_queue_item",
            stale_once_get_queue_item,
        )
        for transition in (
            state.approve_queue_item,
            state.skip_queue_item,
            state.retry_queue_item,
        ):
            stale_reads[0] = 1
            with pytest.raises(AutomationError, match="terminal state"):
                transition(approved.id)

        monkeypatch.undo()
        after = state.get_queue_item(approved.id)

    assert after.state is QueueState.UNSUPPORTED
    assert after.attempts == 0


def test_cli_queue_list_accepts_unsupported_state_filter(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    state_path = tmp_path / "state.sqlite"
    with open_state(state_path) as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        queue_item = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item()])]),
            ),
        ).queued[0]
        approved = state.approve_queue_item(queue_item.id)
        with pytest.raises(AutomationError, match="metadata-only discovery"):
            state.ingest_queue_item(approved.id, tmp_path / "bundles")

    assert (
        cli.main(
            [
                "queue",
                "list",
                "--queue-state",
                "unsupported",
                "--state",
                str(state_path),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in payload["queue"]] == [queue_item.id]
    assert payload["queue"][0]["state"] == "unsupported"


def test_youtube_api_key_is_not_persisted(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    secret = "fake-secret-value"

    with open_state(state_path) as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                secret,
                transport=FakeTransport([_playlist_page([_alpha_item()])]),
            ),
        )
        serialized_records = json.dumps(
            {
                "sources": [item.to_dict() for item in state.list_sources()],
                "queue": [item.to_dict() for item in state.list_queue()],
            },
            sort_keys=True,
        )

    assert secret.encode("utf-8") not in state_path.read_bytes()
    assert secret not in serialized_records


def test_cli_youtube_source_commands_report_missing_key(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    state_path = tmp_path / "state.sqlite"

    assert (
        cli.main(
            [
                "sources",
                "add-youtube-playlist",
                "yt",
                "https://www.youtube.com/playlist?list=PL_SYNTH",
                "--state",
                str(state_path),
                "--json",
            ]
        )
        == 0
    )
    source_payload = json.loads(capsys.readouterr().out)
    assert source_payload["kind"] == "youtube-playlist"
    assert source_payload["root_path"] == "PL_SYNTH"

    assert cli.main(["sources", "preflight-youtube", "PL_SYNTH", "--json"]) == 1
    preflight_payload = json.loads(capsys.readouterr().out)
    assert preflight_payload["credential_present"] is False
    assert "YOUTUBE_API_KEY" in preflight_payload["error"]

    assert cli.main(["sources", "scan", "yt", "--state", str(state_path)]) == 3
    captured = capsys.readouterr()
    assert "missing YouTube API key" in captured.err


def test_youtube_scan_only_policy_discovers_without_queueing(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH", SourcePolicy.SCAN_ONLY)
        delta = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item()])]),
            ),
        )

    assert len(delta.added) == 1
    assert delta.queued == []


def test_youtube_metadata_records_every_documented_field(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        delta = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item()])]),
            ),
        )

    assert len(delta.added) == 1
    item = delta.added[0]
    assert item.relative_path == "PL_SYNTH/vid_alpha"
    assert item.absolute_path == "https://www.youtube.com/watch?v=vid_alpha&list=PL_SYNTH"
    assert item.metadata == {
        "source": {
            "kind": "youtube-playlist",
            "source_id": source.id,
            "source_name": "yt",
        },
        "playlist": {
            "id": "PL_SYNTH",
            "url": "https://www.youtube.com/playlist?list=PL_SYNTH",
        },
        "playlist_item": {
            "id": "pli_alpha",
            "position": 0,
            "published_at": "2026-06-01T00:00:00Z",
        },
        "video": {
            "id": "vid_alpha",
            "url": "https://www.youtube.com/watch?v=vid_alpha&list=PL_SYNTH",
            "title": "Synthetic Alpha Talk",
            "channel_id": "chan_alpha",
            "channel_title": "Synthetic Alpha Channel",
            "video_owner_channel_id": "owner_chan_alpha",
            "video_owner_channel_title": "Synthetic Alpha Owner Channel",
            "published_at": "2026-05-30T12:00:00Z",
            "placeholder": False,
        },
        "discovery": {
            "adapter": "youtube-playlist",
            "api": "youtube-data-api-v3",
            "method": "playlistItems.list",
            "part": "snippet,contentDetails",
            "page_index": 0,
            "units_per_page": 1,
        },
    }


def test_same_video_under_two_playlist_items_yields_one_item(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        delta = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport(
                    [
                        _playlist_page(
                            [
                                _alpha_item(position=0, playlist_item_id="pli_first"),
                                _alpha_item(position=1, playlist_item_id="pli_second"),
                            ]
                        )
                    ]
                ),
            ),
        )
        queue_items = state.list_queue()

    assert [item.relative_path for item in delta.added] == ["PL_SYNTH/vid_alpha"]
    assert len(delta.queued) == 1
    assert len(queue_items) == 1
    # First occurrence wins.
    assert delta.added[0].metadata["playlist_item"]["id"] == "pli_first"


def test_remove_and_readd_with_new_playlist_item_id_does_not_requeue(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        first = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item()])]),
            ),
        )
        readded = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport(
                    [_playlist_page([_alpha_item(playlist_item_id="pli_alpha_readded")])]
                ),
            ),
        )
        queue_items = state.list_queue()

    assert len(first.queued) == 1
    assert readded.added == []
    assert readded.changed == []
    assert readded.removed == []
    assert [item.relative_path for item in readded.unchanged] == ["PL_SYNTH/vid_alpha"]
    assert readded.queued == []
    assert len(queue_items) == 1


def test_readd_that_changes_playlist_insertion_timestamp_does_not_requeue(
    tmp_path: Path,
) -> None:
    """A real remove-and-re-add rewrites `snippet.publishedAt` (playlist-insertion
    time). The digest excludes positional/curation noise — playlist item ID,
    position, and timestamps (accepted design constraint H1) — so the re-added
    video must stay one source item and must not be re-queued."""

    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item()])]),
            ),
        )
        readded = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport(
                    [
                        _playlist_page(
                            [
                                _alpha_item(
                                    playlist_item_id="pli_alpha_readded",
                                    published_at="2026-07-24T00:00:00Z",
                                )
                            ]
                        )
                    ]
                ),
            ),
        )
        queue_items = state.list_queue()

    assert readded.added == []
    assert readded.removed == []
    assert readded.changed == []
    assert readded.queued == []
    assert len(queue_items) == 1
    assert {item.metadata["video"]["id"] for item in queue_items} == {"vid_alpha"}


def test_truncated_scan_skips_removals_and_flags_scan_metadata(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        full = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport(
                    [
                        _playlist_page([_alpha_item()], next_page_token="NEXT"),
                        _playlist_page([_beta_item()]),
                    ]
                ),
            ),
        )
        truncated = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item()], next_page_token="NEXT")]),
                max_pages=1,
            ),
        )
        stored = [state.get_source_item(item.id) for item in full.added]

    assert [item.relative_path for item in full.added] == [
        "PL_SYNTH/vid_alpha",
        "PL_SYNTH/vid_beta",
    ]
    assert truncated.removed == []
    assert truncated.metadata["quota"]["truncated_by_max_pages"] is True
    assert truncated.metadata["removals_skipped_due_to_truncation"] is True
    assert [item.present for item in stored] == [True, True]


def test_untruncated_scan_does_not_flag_skipped_removals(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        first = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item(), _beta_item()])]),
            ),
        )
        shrunk = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item()])]),
                max_pages=2,
            ),
        )

    assert len(first.added) == 2
    assert [item.relative_path for item in shrunk.removed] == ["PL_SYNTH/vid_beta"]
    assert "removals_skipped_due_to_truncation" not in shrunk.metadata


def test_placeholder_entries_are_flagged_and_not_queued(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        delta = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport(
                    [
                        _playlist_page(
                            [
                                _alpha_item(),
                                _placeholder_item(),
                                _placeholder_item(
                                    playlist_item_id="pli_deleted",
                                    video_id="vid_deleted",
                                    title="Deleted video",
                                    position=3,
                                ),
                            ]
                        )
                    ]
                ),
            ),
        )
        queue_items = state.list_queue()

    assert [item.relative_path for item in delta.added] == [
        "PL_SYNTH/vid_alpha",
        "PL_SYNTH/vid_private",
        "PL_SYNTH/vid_deleted",
    ]
    assert [item.metadata["video"]["placeholder"] for item in delta.added] == [False, True, True]
    assert [item.metadata["video"]["id"] for item in delta.queued] == ["vid_alpha"]
    assert [item.metadata["video"]["id"] for item in queue_items] == ["vid_alpha"]


def test_half_migrated_v1_state_opens_and_reaches_v2(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    _create_v1_state(state_path, tmp_path)
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "ALTER TABLE source_items ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
        )
        connection.execute("PRAGMA user_version = 1")

    with open_state(state_path) as state:
        item = state.get_source_item("item_legacy")
        queue = state.get_queue_item("queue_legacy")

    with sqlite3.connect(state_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        queue_metadata = connection.execute(
            "SELECT metadata_json FROM queue_items WHERE id = 'queue_legacy'"
        ).fetchone()[0]

    assert version == STATE_SCHEMA_VERSION
    assert item.metadata == {}
    assert queue.metadata == {}
    assert queue_metadata == "{}"


def _is_backup_temporary(target: str | Path) -> bool:
    """Report whether a sqlite3.connect target is a v1-backup temporary file.

    The temporary carries a per-attempt unique component, so tests match the
    surrounding name rather than a fixed suffix.
    """

    name = Path(target).name
    return name.startswith(".state.sqlite.v1.bak.") and name.endswith(".tmp")


def _assert_backup_is_pre_migration(backup: Path) -> None:
    assert backup.is_file()
    connection = sqlite3.connect(backup)
    try:
        backup_version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(source_items)").fetchall()
        }
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert backup_version == 1
    assert "metadata_json" not in columns
    assert {"sources", "source_items", "queue_items"} <= table_names


def test_v1_file_store_migration_writes_pre_migration_backup(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    _create_v1_state(state_path, tmp_path)

    with open_state(state_path):
        pass

    _assert_backup_is_pre_migration(tmp_path / "state.sqlite.v1.bak")


def test_v1_wal_mode_store_backup_captures_committed_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    _create_v1_state(state_path, tmp_path)
    connection = sqlite3.connect(state_path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.commit()
    finally:
        connection.close()

    with open_state(state_path):
        pass

    _assert_backup_is_pre_migration(tmp_path / "state.sqlite.v1.bak")


def test_v1_backup_inherits_source_database_permissions(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    _create_v1_state(state_path, tmp_path)
    state_path.chmod(0o600)

    with open_state(state_path):
        pass

    backup = tmp_path / "state.sqlite.v1.bak"
    _assert_backup_is_pre_migration(backup)
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_v1_backup_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    _create_v1_state(state_path, tmp_path)

    with open_state(state_path):
        pass

    _assert_backup_is_pre_migration(tmp_path / "state.sqlite.v1.bak")
    assert [path.name for path in tmp_path.iterdir() if path.name.endswith(".tmp")] == []


def test_v1_backup_temporary_name_is_unique_per_attempt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    real_connect = sqlite3.connect
    observed: list[str] = []

    def recording_connect(target: str | Path) -> sqlite3.Connection:
        if _is_backup_temporary(target):
            observed.append(Path(target).name)
        return real_connect(target)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)

    for directory in ("first", "second"):
        state_path = tmp_path / directory / "state.sqlite"
        state_path.parent.mkdir()
        _create_v1_state(state_path, tmp_path)
        with open_state(state_path):
            pass

    monkeypatch.undo()

    # Two processes migrating the same v1 store must not share a temporary name:
    # a fixed name lets one attempt's cleanup unlink the other's in-progress copy.
    assert len(observed) == 2
    assert observed[0] != observed[1]
    for directory in ("first", "second"):
        _assert_backup_is_pre_migration(tmp_path / directory / "state.sqlite.v1.bak")
        leftovers = [path.name for path in (tmp_path / directory).iterdir()]
        assert [name for name in leftovers if name.endswith(".tmp")] == []


def test_interrupted_v1_backup_does_not_publish_partial_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    state_path = tmp_path / "state.sqlite"
    _create_v1_state(state_path, tmp_path)

    real_connect = sqlite3.connect

    def flaky_connect(target: str | Path) -> sqlite3.Connection:
        connection = real_connect(target)
        if _is_backup_temporary(target):
            # Leave partial bytes in the destination file and then fail inside
            # backup(), the way a killed process would.
            connection.execute("CREATE TABLE partial(x)")
            connection.commit()
            connection.close()
        return connection

    monkeypatch.setattr(sqlite3, "connect", flaky_connect)

    with pytest.raises(AutomationError, match="state database error"):
        open_state(state_path)

    monkeypatch.undo()

    # The half-written copy must not survive under the final name: the existence
    # check would otherwise treat it as a complete pre-migration backup.
    assert not (tmp_path / "state.sqlite.v1.bak").exists()
    assert [path.name for path in tmp_path.iterdir() if path.name.endswith(".tmp")] == []
    with sqlite3.connect(state_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_youtube_incomplete_read_raises_automation_error(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    def fail_urlopen(*args: object, **kwargs: object) -> object:
        raise http.client.IncompleteRead(b'{"items":', 128)

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)

    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        with pytest.raises(AutomationError, match="YouTube Data API request failed"):
            state.scan_source(source.id, adapter=YouTubePlaylistAdapter("fake-secret"))

    preflight = preflight_youtube_playlist("PL_SYNTH", api_key="fake-secret")

    assert not preflight.ok
    assert "YouTube Data API request failed" in str(preflight.error)


class _TruncatedBodyHTTPError(urllib.error.HTTPError):
    """An HTTP error whose body read fails the way a truncated response does."""

    def __init__(self) -> None:
        super().__init__(
            "https://youtube.invalid/playlistItems",
            500,
            "Internal Server Error",
            email.message.Message(),
            None,
        )

    def read(self, *args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise http.client.IncompleteRead(b'{"error":', 128)


def test_youtube_truncated_error_body_raises_domain_error(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    def fail_urlopen(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise _TruncatedBodyHTTPError()

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)

    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        with pytest.raises(YouTubeAPIError) as raised:
            state.scan_source(source.id, adapter=YouTubePlaylistAdapter("fake-secret"))

    preflight = preflight_youtube_playlist("PL_SYNTH", api_key="fake-secret")

    # A body that cannot be read must not turn the domain error into a raw
    # protocol error: the status code still has to reach the caller.
    assert raised.value.status_code == 500
    assert "YouTube Data API error (500" in str(raised.value)
    assert not preflight.ok
    assert "YouTube Data API error (500" in str(preflight.error)


def test_youtube_preflight_keeps_credential_status_for_malformed_playlist() -> None:
    with_key = preflight_youtube_playlist("not a playlist", api_key="fake-secret")
    without_key = preflight_youtube_playlist("not a playlist", environ={})

    assert not with_key.ok
    assert "playlist" in str(with_key.error)
    assert with_key.credential_present is True
    assert not without_key.ok
    assert without_key.credential_present is False


def test_unparseable_playlist_url_is_a_domain_error() -> None:
    # urllib.parse.urlparse raises ValueError on this input; callers only handle
    # AutomationError, so an unconverted ValueError would escape the CLI.
    with pytest.raises(AutomationError, match="playlist"):
        normalize_youtube_playlist_id("http://[broken")

    preflight = preflight_youtube_playlist("http://[broken", api_key="fake-secret")

    assert not preflight.ok
    assert "playlist" in str(preflight.error)
    assert preflight.credential_present is True
    assert preflight_youtube_playlist("http://[broken", environ={}).credential_present is False


def test_v1_backup_temporary_file_is_restricted_before_backup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    state_path = tmp_path / "state.sqlite"
    _create_v1_state(state_path, tmp_path)
    state_path.chmod(0o600)

    real_connect = sqlite3.connect
    observed: list[int | None] = []

    def recording_connect(target: str | Path) -> sqlite3.Connection:
        connection = real_connect(target)
        if _is_backup_temporary(target):
            temporary = Path(target)
            observed.append(stat.S_IMODE(temporary.stat().st_mode) if temporary.exists() else None)
        return connection

    monkeypatch.setattr(sqlite3, "connect", recording_connect)

    with open_state(state_path):
        pass

    monkeypatch.undo()
    backup = tmp_path / "state.sqlite.v1.bak"

    # The temporary copy holds the same bytes as the source database, so it must
    # never exist under broader permissions — not even for the backup's duration.
    assert observed == [0o600]
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    _assert_backup_is_pre_migration(backup)


def test_adapter_rejects_nonpositive_max_pages() -> None:
    for bad in (0, -1):
        with pytest.raises(AutomationError, match="max_pages"):
            YouTubePlaylistAdapter("fake-secret", max_pages=bad)


def test_youtube_response_invalid_utf8_raises_automation_error(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        with pytest.raises(AutomationError, match="was not valid"):
            state.scan_source(
                source.id,
                adapter=YouTubePlaylistAdapter(
                    "fake-secret",
                    transport=FakeTransport([b"\xff\xfe\xfd not utf-8"]),
                ),
            )


def test_cli_scan_of_disabled_youtube_source_requires_no_api_key(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    state_path = tmp_path / "state.sqlite"
    with open_state(state_path) as state:
        state.add_youtube_playlist_source("yt", "PL_SYNTH", policy=SourcePolicy.DISABLED)

    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)

    assert cli.main(["sources", "scan", "yt", "--state", str(state_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"] == {
        "added": 0,
        "changed": 0,
        "removed": 0,
        "unchanged": 0,
        "queued": 0,
    }


def test_cli_sources_scan_max_pages_limits_pages_and_skips_removals(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    state_path = tmp_path / "state.sqlite"
    with open_state(state_path) as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport(
                    [
                        _playlist_page([_alpha_item()], next_page_token="NEXT"),
                        _playlist_page([_beta_item()]),
                    ]
                ),
            ),
        )

    monkeypatch.setenv("YOUTUBE_API_KEY", "fake-secret")
    # FakeTransport raises if a second page is requested, so an unbounded scan fails here.
    monkeypatch.setattr(
        automation,
        "_urllib_get",
        FakeTransport([_playlist_page([_alpha_item()], next_page_token="NEXT")]),
    )

    assert (
        cli.main(
            [
                "sources",
                "scan",
                "yt",
                "--max-pages",
                "1",
                "--state",
                str(state_path),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["counts"]["removed"] == 0
    assert payload["metadata"]["quota"]["pages_fetched"] == 1
    assert payload["metadata"]["removals_skipped_due_to_truncation"] is True


def test_cli_sources_scan_rejects_non_positive_max_pages(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    state_path = tmp_path / "state.sqlite"
    with open_state(state_path) as state:
        state.add_youtube_playlist_source("yt", "PL_SYNTH")

    assert cli.main(["sources", "scan", "yt", "--max-pages", "0", "--state", str(state_path)]) == 2
    assert "--max-pages" in capsys.readouterr().err
    assert (
        cli.main(["sources", "scan", "yt", "--max-pages", "many", "--state", str(state_path)]) == 2
    )
    assert "--max-pages" in capsys.readouterr().err


def _youtube_source_record(playlist_id: str) -> SourceRecord:
    return SourceRecord(
        id="src_synthetic_youtube",
        kind=SourceKind.YOUTUBE_PLAYLIST,
        name="yt",
        root_path=playlist_id,
        policy=SourcePolicy.SCAN_ONLY,
        created_at="2026-07-24T00:00:00+00:00",
        updated_at="2026-07-24T00:00:00+00:00",
    )


def _playlist_page(items: list[dict[str, object]], *, next_page_token: str | None = None) -> bytes:
    payload: dict[str, object] = {
        "kind": "youtube#playlistItemListResponse",
        "etag": "synthetic-etag",
        "pageInfo": {"totalResults": len(items), "resultsPerPage": len(items)},
        "items": items,
    }
    if next_page_token is not None:
        payload["nextPageToken"] = next_page_token
    return json.dumps(payload).encode("utf-8")


def _page_with_raw_next_page_token(items: list[dict[str, object]], token: object) -> bytes:
    """A page whose nextPageToken is present but not a string (or null)."""

    payload = cast(dict[str, object], json.loads(_playlist_page(items)))
    payload["nextPageToken"] = token
    return json.dumps(payload).encode("utf-8")


def _alpha_item(
    *,
    position: int = 0,
    playlist_item_id: str = "pli_alpha",
    published_at: str = "2026-06-01T00:00:00Z",
) -> dict[str, object]:
    return _playlist_item(
        playlist_item_id=playlist_item_id,
        video_id="vid_alpha",
        title="Synthetic Alpha Talk",
        channel_id="chan_alpha",
        channel_title="Synthetic Alpha Channel",
        video_owner_channel_id="owner_chan_alpha",
        video_owner_channel_title="Synthetic Alpha Owner Channel",
        position=position,
        published_at=published_at,
    )


def _beta_item(*, position: int = 1, playlist_item_id: str = "pli_beta") -> dict[str, object]:
    return _playlist_item(
        playlist_item_id=playlist_item_id,
        video_id="vid_beta",
        title="Synthetic Beta Talk",
        channel_id="chan_beta",
        channel_title="Synthetic Beta Channel",
        video_owner_channel_id="owner_chan_beta",
        video_owner_channel_title="Synthetic Beta Owner Channel",
        position=position,
    )


def _playlist_item(
    *,
    playlist_item_id: str,
    video_id: str,
    title: str,
    channel_id: str,
    channel_title: str,
    video_owner_channel_id: str,
    video_owner_channel_title: str,
    position: int,
    published_at: str = "2026-06-01T00:00:00Z",
    video_published_at: str = "2026-05-30T12:00:00Z",
) -> dict[str, object]:
    """Full-shape playlist item: every documented snippet/contentDetails field.

    Field locations follow the ``playlistItems`` resource reference: the
    ``videoOwnerChannel*`` pair lives in ``snippet``, ``videoPublishedAt`` in
    ``contentDetails``.
    """

    return {
        "kind": "youtube#playlistItem",
        "etag": f"synthetic-{playlist_item_id}",
        "id": playlist_item_id,
        "snippet": {
            "publishedAt": published_at,
            "channelId": channel_id,
            "title": title,
            "description": f"Synthetic description for {video_id}.",
            "thumbnails": {
                "default": {
                    "url": f"https://i.ytimg.com/vi/{video_id}/default.jpg",
                    "width": 120,
                    "height": 90,
                }
            },
            "channelTitle": channel_title,
            "videoOwnerChannelId": video_owner_channel_id,
            "videoOwnerChannelTitle": video_owner_channel_title,
            "playlistId": "PL_SYNTH",
            "position": position,
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id,
            },
        },
        "contentDetails": {
            "videoId": video_id,
            "videoPublishedAt": video_published_at,
        },
    }


def _placeholder_item(
    *,
    playlist_item_id: str = "pli_private",
    video_id: str = "vid_private",
    title: str = "Private video",
    position: int = 2,
) -> dict[str, object]:
    """Tombstone entry shape: no owner channel fields, no videoPublishedAt."""

    return {
        "kind": "youtube#playlistItem",
        "etag": f"synthetic-{playlist_item_id}",
        "id": playlist_item_id,
        "snippet": {
            "publishedAt": "2026-06-01T00:00:00Z",
            "channelId": "chan_alpha",
            "title": title,
            "description": "This video is unavailable.",
            "channelTitle": "Synthetic Alpha Channel",
            "playlistId": "PL_SYNTH",
            "position": position,
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id,
            },
        },
        "contentDetails": {
            "videoId": video_id,
        },
    }


def _create_v1_state(path: Path, tmp_path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE sources (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL UNIQUE,
                root_path TEXT NOT NULL,
                policy TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE source_items (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL,
                absolute_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                present INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_id, relative_path)
            );

            CREATE TABLE queue_items (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                source_item_id TEXT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
                content_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,
                policy TEXT NOT NULL,
                bundle_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_item_id, content_sha256)
            );

            CREATE TABLE library_bundles (
                bundle_id TEXT PRIMARY KEY,
                bundle_path TEXT NOT NULL UNIQUE,
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                source_item_id TEXT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
                queue_item_id TEXT NOT NULL REFERENCES queue_items(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            );
            PRAGMA user_version = 1;
            """
        )
        connection.execute(
            """
            INSERT INTO sources(id, kind, name, root_path, policy, created_at, updated_at)
            VALUES ('src_legacy', 'local-folder', 'legacy', ?, 'review', ?, ?)
            """,
            (str(tmp_path / "source"), "2026-06-24T00:00:00+00:00", "2026-06-24T00:00:00+00:00"),
        )
        connection.execute(
            """
            INSERT INTO source_items(
                id, source_id, relative_path, absolute_path, sha256, size_bytes, mtime_ns,
                present, created_at, updated_at
            )
            VALUES (
                'item_legacy', 'src_legacy', 'synthetic_talk.wav', ?, 'digest', 12, 34, 1, ?, ?
            )
            """,
            (
                str(tmp_path / "source" / "synthetic_talk.wav"),
                "2026-06-24T00:00:00+00:00",
                "2026-06-24T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO queue_items(
                id, source_id, source_item_id, content_sha256, state, policy,
                bundle_id, attempts, last_error, created_at, updated_at
            )
            VALUES (
                'queue_legacy', 'src_legacy', 'item_legacy', 'digest', 'discovered',
                'review', NULL, 0, NULL, ?, ?
            )
            """,
            ("2026-06-24T00:00:00+00:00", "2026-06-24T00:00:00+00:00"),
        )
        connection.execute(
            """
            INSERT INTO library_bundles(
                bundle_id, bundle_path, source_id, source_item_id, queue_item_id, created_at
            )
            VALUES ('legacy-bundle', ?, 'src_legacy', 'item_legacy', 'queue_legacy', ?)
            """,
            (str(tmp_path / "bundles" / "legacy-bundle"), "2026-06-24T00:00:00+00:00"),
        )


def test_repeated_next_page_token_raises_without_mutating_state(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        first = state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport(
                    [
                        _playlist_page([_alpha_item()], next_page_token="LOOP"),
                        _playlist_page([_beta_item()]),
                    ]
                ),
            ),
        )

        with pytest.raises(AutomationError, match="repeated nextPageToken"):
            state.scan_source(
                source.id,
                adapter=YouTubePlaylistAdapter(
                    "fake-secret",
                    transport=FakeTransport(
                        [
                            _playlist_page([_alpha_item()], next_page_token="LOOP"),
                            _playlist_page([_beta_item()], next_page_token="LOOP"),
                        ]
                    ),
                ),
            )

        after_items = [state.get_source_item(item.id) for item in first.added]

    assert [item.present for item in after_items] == [True, True]


def test_stale_column_check_migration_race_is_tolerated(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state_path = tmp_path / "state.sqlite"
    _create_v1_state(state_path, tmp_path)
    # Another process wins the race: columns already added, user_version still 1.
    connection = sqlite3.connect(state_path)
    try:
        connection.execute(
            "ALTER TABLE source_items ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
        )
        connection.execute(
            "ALTER TABLE queue_items ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
        )
        connection.commit()
    finally:
        connection.close()

    # This process's column check reads stale absence (TOCTOU across processes).
    def stale_column_check(self: automation.AutomationState, table: str, column: str) -> bool:
        del self, table, column
        return False

    monkeypatch.setattr(automation.AutomationState, "_table_has_column", stale_column_check)

    with open_state(state_path) as state:
        assert state.get_source("src_legacy").name == "legacy"

    connection = sqlite3.connect(state_path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    assert version == STATE_SCHEMA_VERSION


def test_concurrently_published_backup_is_not_replaced(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state_path = tmp_path / "state.sqlite"
    _create_v1_state(state_path, tmp_path)
    backup = tmp_path / "state.sqlite.v1.bak"
    marker = b"concurrent pristine backup"
    real_connect = sqlite3.connect

    def racing_connect(target: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        # Simulate another process publishing its backup after this process's
        # existence check but before this process publishes its own copy.
        if isinstance(target, (str, Path)) and str(target).endswith(".tmp") and not backup.exists():
            backup.write_bytes(marker)
        return real_connect(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite3, "connect", racing_connect)

    with open_state(state_path):
        pass

    assert backup.read_bytes() == marker


def test_rejected_guarded_transition_rolls_back_write_transaction(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item()])]),
            ),
        )
        queue_item = state.list_queue()[0]
        approved = state.approve_queue_item(queue_item.id)
        with pytest.raises(AutomationError, match=YOUTUBE_METADATA_ONLY_ERROR):
            state.ingest_queue_item(approved.id, tmp_path / "bundles")
        unsupported = state.get_queue_item(approved.id)
        assert unsupported.state is QueueState.UNSUPPORTED

        # Force the guarded zero-row UPDATE to execute by making the pre-check
        # read a stale nonterminal state, as a concurrent process would.
        real_get = automation.AutomationState.get_queue_item
        stale_reads = [approved]

        def stale_get(self: automation.AutomationState, queue_item_id: str) -> automation.QueueItem:
            if stale_reads:
                return stale_reads.pop(0)
            return real_get(self, queue_item_id)

        monkeypatch.setattr(automation.AutomationState, "get_queue_item", stale_get)
        with pytest.raises(AutomationError, match="terminal state"):
            state.retry_queue_item(unsupported.id)
        monkeypatch.setattr(automation.AutomationState, "get_queue_item", real_get)
        # The zero-row guarded UPDATE must not leave the connection holding an
        # open write transaction after the expected rejection is caught.
        assert state._connection.in_transaction is False  # noqa: SLF001


def test_unsupported_write_does_not_clobber_concurrent_skip(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("yt", "PL_SYNTH")
        state.scan_source(
            source.id,
            adapter=YouTubePlaylistAdapter(
                "fake-secret",
                transport=FakeTransport([_playlist_page([_alpha_item()])]),
            ),
        )
        queue_item = state.list_queue()[0]
        approved = state.approve_queue_item(queue_item.id)

        real_get = automation.AutomationState.get_queue_item
        stale_reads = [approved, approved]

        def stale_get(self: automation.AutomationState, queue_item_id: str) -> automation.QueueItem:
            if stale_reads:
                return stale_reads.pop(0)
            return real_get(self, queue_item_id)

        # A concurrent operator skip commits between this process's approved
        # read and its terminal write.
        state._connection.execute(  # noqa: SLF001
            "UPDATE queue_items SET state = ? WHERE id = ?",
            (QueueState.SKIPPED.value, approved.id),
        )
        state._connection.commit()  # noqa: SLF001
        monkeypatch.setattr(automation.AutomationState, "get_queue_item", stale_get)

        with pytest.raises(AutomationError, match=YOUTUBE_METADATA_ONLY_ERROR):
            state.ingest_queue_item(approved.id, tmp_path / "bundles")
        monkeypatch.setattr(automation.AutomationState, "get_queue_item", real_get)
        final = state.get_queue_item(approved.id)

    assert final.state is QueueState.SKIPPED
