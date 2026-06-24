from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
from pytest import CaptureFixture, MonkeyPatch

from lectern import cli
from lectern.automation import (
    STATE_SCHEMA_VERSION,
    YOUTUBE_METADATA_ONLY_ERROR,
    AutomationError,
    QueueState,
    SourcePolicy,
    YouTubeAPIError,
    YouTubePlaylistAdapter,
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
        failed = state.get_queue_item(approved.id)

    assert failed.state is QueueState.FAILED
    assert failed.last_error == YOUTUBE_METADATA_ONLY_ERROR


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


def _alpha_item(*, position: int = 0) -> dict[str, object]:
    return _playlist_item(
        playlist_item_id="pli_alpha",
        video_id="vid_alpha",
        title="Synthetic Alpha Talk",
        channel_id="chan_alpha",
        channel_title="Synthetic Alpha Channel",
        position=position,
    )


def _beta_item(*, position: int = 1) -> dict[str, object]:
    return _playlist_item(
        playlist_item_id="pli_beta",
        video_id="vid_beta",
        title="Synthetic Beta Talk",
        channel_id="chan_beta",
        channel_title="Synthetic Beta Channel",
        position=position,
    )


def _playlist_item(
    *,
    playlist_item_id: str,
    video_id: str,
    title: str,
    channel_id: str,
    channel_title: str,
    position: int,
) -> dict[str, object]:
    return {
        "kind": "youtube#playlistItem",
        "etag": f"synthetic-{playlist_item_id}",
        "id": playlist_item_id,
        "snippet": {
            "publishedAt": "2026-06-01T00:00:00Z",
            "channelId": channel_id,
            "title": title,
            "channelTitle": channel_title,
            "playlistId": "PL_SYNTH",
            "position": position,
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id,
            },
        },
        "contentDetails": {
            "videoId": video_id,
            "videoPublishedAt": "2026-06-01T00:00:00Z",
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
