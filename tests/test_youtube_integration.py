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

    assert len(first.added) > 0
    assert len(first.queued) == len(first.added)
    assert second.added == []
    assert second.changed == []
    assert second.queued == []
    assert len(queue_items) == len(first.queued)
    assert queue_items[0].metadata["playlist"]["id"] == playlist_id
    assert queue_items[0].metadata["video"]["id"]
    assert queue_items[0].metadata["discovery"]["method"] == "playlistItems.list"
