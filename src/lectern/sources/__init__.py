"""Source adapters: the discovery seam a new provider implements.

An adapter turns a `SourceRecord` into the `SourceItem`s currently visible at
that source. `default_source_adapter` is the only place that maps a source kind
to a concrete implementation.
"""

from __future__ import annotations

from lectern.records import (
    AutomationError,
    ScanMetadataProvider,
    SourceAdapter,
    SourceKind,
    SourceRecord,
)
from lectern.sources.local import (
    EXCLUDED_SCAN_DIR_NAMES,
    EXCLUDED_SCAN_DIR_PREFIXES,
    MEDIA_EXTENSIONS,
    LocalFolderAdapter,
    SourcePreflight,
    approval_digest_and_media_size,
    is_bundle_output_path,
    iter_local_media_files,
    preflight_local_folder,
)
from lectern.sources.youtube import (
    DEFAULT_YOUTUBE_API_KEY_ENV,
    YOUTUBE_METADATA_ONLY_ERROR,
    YouTubeAPIError,
    YouTubePlaylistAdapter,
    YouTubePreflight,
    normalize_youtube_playlist_id,
    preflight_youtube_playlist,
)

__all__ = [
    "DEFAULT_YOUTUBE_API_KEY_ENV",
    "EXCLUDED_SCAN_DIR_NAMES",
    "EXCLUDED_SCAN_DIR_PREFIXES",
    "MEDIA_EXTENSIONS",
    "YOUTUBE_METADATA_ONLY_ERROR",
    "LocalFolderAdapter",
    "ScanMetadataProvider",
    "SourceAdapter",
    "SourcePreflight",
    "YouTubeAPIError",
    "YouTubePlaylistAdapter",
    "YouTubePreflight",
    "approval_digest_and_media_size",
    "default_source_adapter",
    "is_bundle_output_path",
    "iter_local_media_files",
    "normalize_youtube_playlist_id",
    "preflight_local_folder",
    "preflight_youtube_playlist",
]


def default_source_adapter(source: SourceRecord) -> SourceAdapter:
    if source.kind is SourceKind.LOCAL_FOLDER:
        return LocalFolderAdapter()
    if source.kind is SourceKind.YOUTUBE_PLAYLIST:
        return YouTubePlaylistAdapter.from_environment()
    raise AutomationError(f"unsupported source kind for scan: {source.kind.value}")
