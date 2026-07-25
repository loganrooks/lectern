from __future__ import annotations

import os
from pathlib import Path

import pytest

from lectern.automation import open_state


@pytest.mark.integration
def test_youtube_public_playlist_scan_repeats_without_duplicates(tmp_path: Path) -> None:
    api_key = os.environ.get("YOUTUBE_API_KEY")
    playlist_id = os.environ.get("LECTERN_YOUTUBE_PLAYLIST_ID")
    if not api_key or not playlist_id:
        pytest.skip("set YOUTUBE_API_KEY and LECTERN_YOUTUBE_PLAYLIST_ID for integration")

    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_youtube_playlist_source("integration-youtube", playlist_id)
        first = state.scan_source(source.id)
        second = state.scan_source(source.id)
        queue_items = state.list_queue()

    ingestible = [item for item in first.added if not item.metadata["video"]["placeholder"]]

    assert len(first.added) > 0
    assert len(ingestible) > 0
    # Placeholder (private/deleted) entries are recorded but never enqueued.
    assert len(first.queued) == len(ingestible)
    assert second.added == []
    assert second.changed == []
    assert second.removed == []
    assert second.queued == []
    assert len(queue_items) == len(first.queued)

    video_ids = [item.metadata["video"]["id"] for item in queue_items]
    relative_paths = [item.relative_path for item in first.added]
    assert len(video_ids) == len(set(video_ids))
    assert len(relative_paths) == len(set(relative_paths))

    metadata = queue_items[0].metadata
    assert metadata["playlist"]["id"] == playlist_id
    assert metadata["discovery"]["method"] == "playlistItems.list"
    assert metadata["video"]["placeholder"] is False
    assert metadata["video"]["id"]
    assert metadata["video"]["title"]
    assert metadata["video"]["channel_id"]
    assert metadata["video"]["channel_title"]
    # snippet.videoOwnerChannel* — asserted unconditionally on purpose: reading them
    # from contentDetails instead of snippet made them permanently null, and only a
    # live assertion can disconfirm that. Do not relax without a recorded reason.
    assert metadata["video"]["video_owner_channel_id"]
    assert metadata["video"]["video_owner_channel_title"]
    # contentDetails.videoPublishedAt, not the playlist-insertion timestamp.
    assert metadata["video"]["published_at"]
    assert metadata["video"]["url"] == (
        f"https://www.youtube.com/watch?v={metadata['video']['id']}&list={playlist_id}"
    )
