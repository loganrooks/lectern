"""Source registry, discovery queue, and library state.

The automation spine records source discovery policy, queues reviewable items,
and bridges approved local items into the existing bundle ingest path. External
adapters may discover metadata, but local-source media artifacts stay local
unless a future stage adds explicit per-item consent.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, Self, cast, runtime_checkable

from lectern.bundle import MANIFEST_NAME, ArtifactRef, Manifest, StageName, atomic_write_text
from lectern.ingest import (
    IngestError,
    IngestResult,
    can_plan_local_bundle_id,
    ingest_local,
    plan_local_bundle_id,
)

STATE_SCHEMA_VERSION = 2
DEFAULT_STATE_PATH = Path(".lectern") / "state.sqlite"
DEFAULT_YOUTUBE_API_KEY_ENV = "YOUTUBE_API_KEY"
YOUTUBE_PLAYLIST_ITEMS_ENDPOINT = "https://www.googleapis.com/youtube/v3/playlistItems"
YOUTUBE_PLAYLIST_PARTS = "snippet,contentDetails"
YOUTUBE_PLAYLIST_PAGE_SIZE = 50
YOUTUBE_PLAYLIST_QUOTA_UNITS_PER_PAGE = 1
YOUTUBE_REQUEST_TIMEOUT_S = 20.0
YOUTUBE_METADATA_ONLY_ERROR = (
    "YouTube media acquisition is not implemented; M4 supports metadata-only discovery"
)
YOUTUBE_PLACEHOLDER_TITLES = frozenset({"Private video", "Deleted video"})
MEDIA_EXTENSIONS = frozenset(
    {".aac", ".avi", ".flac", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".wav", ".webm"}
)
EXCLUDED_SCAN_DIR_NAMES = frozenset({".lectern"})
EXCLUDED_SCAN_DIR_PREFIXES = (".lectern-ingest.",)
UPGRADABLE_STATE_SCHEMA_VERSIONS = frozenset({0, 1, STATE_SCHEMA_VERSION})
HttpGet = Callable[[str, float], bytes]


def _empty_metadata() -> dict[str, Any]:
    return {}


class AutomationError(RuntimeError):
    """Raised when the local automation spine cannot complete a requested action."""


class YouTubeAPIError(AutomationError):
    """Raised when YouTube Data API returns a structured request failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class SourcePolicy(StrEnum):
    DISABLED = "disabled"
    SCAN_ONLY = "scan-only"
    REVIEW = "review"


class QueueState(StrEnum):
    DISCOVERED = "discovered"
    APPROVED = "approved"
    SKIPPED = "skipped"
    FAILED = "failed"
    COMPLETED = "completed"
    UNSUPPORTED = "unsupported"


TERMINAL_QUEUE_STATES = frozenset({QueueState.UNSUPPORTED})

# Keys attach_provenance_to_bundle writes into source.json["provenance"]; a
# completed bundle missing any of them predates a finished provenance attach.
PROVENANCE_KEYS = frozenset(
    {
        "state_schema_version",
        "source_id",
        "source_kind",
        "source_name",
        "source_item_id",
        "queue_item_id",
        "queue_state",
        "policy",
        "consent",
        "remote_services",
    }
)


class SourceKind(StrEnum):
    LOCAL_FOLDER = "local-folder"
    ONE_SHOT = "one-shot"
    YOUTUBE_PLAYLIST = "youtube-playlist"


@dataclass(frozen=True)
class SourceRecord:
    id: str
    kind: SourceKind
    name: str
    root_path: str
    policy: SourcePolicy
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "name": self.name,
            "root_path": self.root_path,
            "policy": self.policy.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class SourceItem:
    id: str
    source_id: str
    relative_path: str
    absolute_path: str
    sha256: str
    size_bytes: int
    mtime_ns: int
    present: bool
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "relative_path": self.relative_path,
            "absolute_path": self.absolute_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "present": self.present,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class QueueItem:
    id: str
    source_id: str
    source_item_id: str
    content_sha256: str
    state: QueueState
    policy: SourcePolicy
    bundle_id: str | None
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "source_item_id": self.source_item_id,
            "content_sha256": self.content_sha256,
            "state": self.state.value,
            "policy": self.policy.value,
            "bundle_id": self.bundle_id,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class LibraryBundle:
    bundle_id: str
    bundle_path: str
    source_id: str
    source_item_id: str
    queue_item_id: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "bundle_path": self.bundle_path,
            "source_id": self.source_id,
            "source_item_id": self.source_item_id,
            "queue_item_id": self.queue_item_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class _LibraryRecordOutcome:
    """What a staged library record changed, so the undo path can reverse it.

    A rerun into a different output root can produce the same deterministic bundle
    id, in which case the row is updated rather than inserted; reversing that needs
    the prior row, not just the knowledge that nothing was inserted.
    """

    inserted: bool
    previous: LibraryBundle | None


@dataclass(frozen=True)
class SourcePreflight:
    path: str
    exists: bool
    is_dir: bool
    readable: bool
    media_files: int

    @property
    def ok(self) -> bool:
        return self.exists and self.is_dir and self.readable

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "is_dir": self.is_dir,
            "readable": self.readable,
            "media_files": self.media_files,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class StateStorePreflight:
    path: str
    exists: bool
    writable_location: bool
    schema_version: int | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.writable_location and self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "writable_location": self.writable_location,
            "schema_version": self.schema_version,
            "error": self.error,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class YouTubePreflight:
    playlist_id: str
    api_key_env: str
    credential_present: bool
    reachable: bool
    pages_checked: int
    estimated_units_consumed: int
    error: str | None

    @property
    def ok(self) -> bool:
        return self.credential_present and self.reachable and self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "playlist_id": self.playlist_id,
            "api_key_env": self.api_key_env,
            "credential_present": self.credential_present,
            "reachable": self.reachable,
            "pages_checked": self.pages_checked,
            "estimated_units_consumed": self.estimated_units_consumed,
            "error": self.error,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class ScanDelta:
    source: SourceRecord
    added: list[SourceItem]
    changed: list[SourceItem]
    removed: list[SourceItem]
    unchanged: list[SourceItem]
    queued: list[QueueItem]
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "added": [item.to_dict() for item in self.added],
            "changed": [item.to_dict() for item in self.changed],
            "removed": [item.to_dict() for item in self.removed],
            "unchanged": [item.to_dict() for item in self.unchanged],
            "queued": [item.to_dict() for item in self.queued],
            "metadata": self.metadata,
            "counts": {
                "added": len(self.added),
                "changed": len(self.changed),
                "removed": len(self.removed),
                "unchanged": len(self.unchanged),
                "queued": len(self.queued),
            },
        }


class SourceAdapter(Protocol):
    """Discovery seam for local folders now and external adapters later."""

    def discover(self, source: SourceRecord) -> Sequence[SourceItem]:
        """Return the current source items for a source."""
        ...


@runtime_checkable
class ScanMetadataProvider(Protocol):
    @property
    def scan_metadata(self) -> dict[str, Any]:
        """Return scan-level metadata from the most recent discovery run."""
        ...


def iter_local_media_files(root: Path) -> Iterator[Path]:
    """Yield, in scan order, the media files a local-folder scan will discover.

    `LocalFolderAdapter.discover` and `preflight_local_folder` share this helper so a
    preflight count reports what a scan of the same tree actually discovers, rather
    than every media-suffixed file including Lectern's own bundle output.
    """

    root_resolved = root.resolve()
    for path in sorted(root.rglob("*")):
        relative_parts = path.parent.relative_to(root).parts
        is_excluded_dir = any(part in EXCLUDED_SCAN_DIR_NAMES for part in relative_parts)
        is_excluded_temp_dir = any(
            part.startswith(EXCLUDED_SCAN_DIR_PREFIXES) for part in relative_parts
        )
        if is_excluded_dir or is_excluded_temp_dir or _is_bundle_output_path(root, path):
            continue
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        try:
            path.resolve().relative_to(root_resolved)
        except ValueError:
            continue
        yield path


class LocalFolderAdapter:
    """Discover media files under a local directory without network access."""

    def discover(self, source: SourceRecord) -> list[SourceItem]:
        root = Path(source.root_path)
        if not root.is_dir():
            raise AutomationError(f"local folder source is not a directory: {root}")

        root_resolved = root.resolve()
        items: list[SourceItem] = []
        for path in iter_local_media_files(root):
            absolute = path.resolve()
            relative = path.relative_to(root).as_posix()
            try:
                digest, size = _approval_digest_and_media_size(path, root=root_resolved)
            except AutomationError:
                continue
            stat = path.stat()
            now = _now()
            items.append(
                SourceItem(
                    id=_source_item_id(source.id, relative),
                    source_id=source.id,
                    relative_path=relative,
                    absolute_path=str(absolute),
                    sha256=digest,
                    size_bytes=size,
                    mtime_ns=stat.st_mtime_ns,
                    present=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        return items


class YouTubePlaylistAdapter:
    """Discover public YouTube playlist metadata with an API key."""

    def __init__(
        self,
        api_key: str,
        *,
        api_key_env: str = DEFAULT_YOUTUBE_API_KEY_ENV,
        transport: HttpGet | None = None,
        max_pages: int | None = None,
        max_results: int = YOUTUBE_PLAYLIST_PAGE_SIZE,
        timeout_s: float = YOUTUBE_REQUEST_TIMEOUT_S,
    ) -> None:
        if not api_key:
            raise AutomationError(f"missing YouTube API key; set {api_key_env}")
        if not 1 <= max_results <= YOUTUBE_PLAYLIST_PAGE_SIZE:
            raise AutomationError("YouTube playlist page size must be between 1 and 50")
        if max_pages is not None and max_pages < 1:
            # A nonpositive cap would return an empty page-zero result that carries
            # no truncation marker, which scan_source would treat as a complete
            # scan and mark every stored item removed.
            raise AutomationError("max_pages must be a positive integer")
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._transport = transport or _urllib_get
        self._max_pages = max_pages
        self._max_results = max_results
        self._timeout_s = timeout_s
        self._scan_metadata: dict[str, Any] = {}
        self._pages_attempted = 0

    @classmethod
    def from_environment(
        cls,
        *,
        api_key_env: str = DEFAULT_YOUTUBE_API_KEY_ENV,
        environ: Mapping[str, str] | None = None,
        transport: HttpGet | None = None,
        max_pages: int | None = None,
        max_results: int = YOUTUBE_PLAYLIST_PAGE_SIZE,
    ) -> YouTubePlaylistAdapter:
        env = environ if environ is not None else os.environ
        return cls(
            env.get(api_key_env, ""),
            api_key_env=api_key_env,
            transport=transport,
            max_pages=max_pages,
            max_results=max_results,
        )

    @property
    def scan_metadata(self) -> dict[str, Any]:
        return dict(self._scan_metadata)

    @property
    def pages_attempted(self) -> int:
        """Page requests issued during the last discover, including failed ones."""

        return self._pages_attempted

    @property
    def attempted_quota_units(self) -> int:
        """Quota units the last discover attempted, whether or not it succeeded."""

        return self._pages_attempted * YOUTUBE_PLAYLIST_QUOTA_UNITS_PER_PAGE

    def discover(self, source: SourceRecord) -> list[SourceItem]:
        if source.kind is not SourceKind.YOUTUBE_PLAYLIST:
            raise AutomationError(
                f"YouTube playlist adapter cannot scan source kind: {source.kind.value}"
            )
        self._scan_metadata = {}
        self._pages_attempted = 0
        playlist_id = normalize_youtube_playlist_id(source.root_path)
        page_token: str | None = None
        pages_fetched = 0
        items: list[SourceItem] = []
        next_page_token_present = False

        while True:
            if self._max_pages is not None and pages_fetched >= self._max_pages:
                next_page_token_present = page_token is not None
                break
            payload = self._fetch_playlist_page(playlist_id, page_token=page_token)
            page_index = pages_fetched
            pages_fetched += 1
            raw_items_obj = payload.get("items")
            if not isinstance(raw_items_obj, list):
                raise AutomationError("YouTube API response missing items list")
            raw_items = cast(list[object], raw_items_obj)
            items.extend(
                _youtube_source_item(source, playlist_id, raw, page_index=page_index)
                for raw in raw_items
            )
            raw_next_page_token = payload.get("nextPageToken")
            # A present-but-non-string token is not an end-of-playlist signal:
            # treating it as one would commit a partial scan as a complete one
            # and mark every unfetched item removed.
            if raw_next_page_token is not None and not isinstance(raw_next_page_token, str):
                raise AutomationError("YouTube API returned a malformed nextPageToken")
            if isinstance(raw_next_page_token, str) and raw_next_page_token:
                page_token = raw_next_page_token
                next_page_token_present = True
                continue
            next_page_token_present = False
            break

        estimated_units = pages_fetched * YOUTUBE_PLAYLIST_QUOTA_UNITS_PER_PAGE
        self._scan_metadata = {
            "source_kind": SourceKind.YOUTUBE_PLAYLIST.value,
            "youtube": {
                "playlist_id": playlist_id,
                "api": "youtube-data-api-v3",
                "method": "playlistItems.list",
                "part": YOUTUBE_PLAYLIST_PARTS,
            },
            "quota": {
                "units_per_page": YOUTUBE_PLAYLIST_QUOTA_UNITS_PER_PAGE,
                "pages_fetched": pages_fetched,
                "estimated_units_consumed": estimated_units,
                "next_page_token_present": next_page_token_present,
                "truncated_by_max_pages": (self._max_pages is not None and next_page_token_present),
            },
        }
        return items

    def _fetch_playlist_page(
        self,
        playlist_id: str,
        *,
        page_token: str | None,
    ) -> dict[str, Any]:
        params = {
            "part": YOUTUBE_PLAYLIST_PARTS,
            "playlistId": playlist_id,
            "maxResults": str(self._max_results),
            "key": self._api_key,
        }
        if page_token is not None:
            params["pageToken"] = page_token
        url = f"{YOUTUBE_PLAYLIST_ITEMS_ENDPOINT}?{urllib.parse.urlencode(params)}"
        # Count the page before the call: the quota unit is spent as soon as the
        # request is issued, so a post-request failure still consumed it.
        self._pages_attempted += 1
        body = self._transport(url, self._timeout_s)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AutomationError("YouTube API response was not valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise AutomationError("YouTube API response was not a JSON object")
        return cast(dict[str, Any], payload)


class AutomationState:
    """SQLite-backed local automation state store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def add_local_folder_source(
        self,
        name: str,
        root_path: Path,
        policy: SourcePolicy = SourcePolicy.REVIEW,
    ) -> SourceRecord:
        root = root_path.resolve()
        if not root.is_dir():
            raise AutomationError(f"source path is not a directory: {root}")
        source_id = _source_id(SourceKind.LOCAL_FOLDER.value, str(root))
        now = _now()
        existing = self._connection.execute(
            "SELECT * FROM sources WHERE id = ? OR name = ?",
            (source_id, name),
        ).fetchone()
        if existing is not None:
            source = _source_from_row(existing)
            if (
                source.kind is SourceKind.LOCAL_FOLDER
                and source.name == name
                and source.root_path == str(root)
                and source.policy is policy
            ):
                return source
            raise AutomationError("source name or path already exists with different settings")
        self._connection.execute(
            """
            INSERT INTO sources(id, kind, name, root_path, policy, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (source_id, SourceKind.LOCAL_FOLDER.value, name, str(root), policy.value, now, now),
        )
        self._connection.commit()
        return self.get_source(source_id)

    def add_youtube_playlist_source(
        self,
        name: str,
        playlist: str,
        policy: SourcePolicy = SourcePolicy.REVIEW,
    ) -> SourceRecord:
        playlist_id = normalize_youtube_playlist_id(playlist)
        source_id = _source_id(SourceKind.YOUTUBE_PLAYLIST.value, playlist_id)
        now = _now()
        existing = self._connection.execute(
            "SELECT * FROM sources WHERE id = ? OR name = ?",
            (source_id, name),
        ).fetchone()
        if existing is not None:
            source = _source_from_row(existing)
            if (
                source.kind is SourceKind.YOUTUBE_PLAYLIST
                and source.name == name
                and source.root_path == playlist_id
                and source.policy is policy
            ):
                return source
            raise AutomationError("source name or playlist already exists with different settings")
        self._connection.execute(
            """
            INSERT INTO sources(id, kind, name, root_path, policy, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                SourceKind.YOUTUBE_PLAYLIST.value,
                name,
                playlist_id,
                policy.value,
                now,
                now,
            ),
        )
        self._connection.commit()
        return self.get_source(source_id)

    def list_sources(self) -> list[SourceRecord]:
        rows = self._connection.execute(
            "SELECT * FROM sources ORDER BY created_at, name"
        ).fetchall()
        return [_source_from_row(row) for row in rows]

    def get_source(self, source_id_or_name: str) -> SourceRecord:
        row = self._connection.execute(
            "SELECT * FROM sources WHERE id = ? OR name = ?",
            (source_id_or_name, source_id_or_name),
        ).fetchone()
        if row is None:
            raise AutomationError(f"source not found: {source_id_or_name}")
        return _source_from_row(row)

    def scan_source(
        self,
        source_id_or_name: str,
        adapter: SourceAdapter | None = None,
    ) -> ScanDelta:
        source = self.get_source(source_id_or_name)
        if source.policy is SourcePolicy.DISABLED:
            return ScanDelta(
                source=source,
                added=[],
                changed=[],
                removed=[],
                unchanged=[],
                queued=[],
            )

        adapter_for_scan = adapter or _default_source_adapter(source)
        current_items = _dedupe_by_relative_path(adapter_for_scan.discover(source))
        scan_metadata = _scan_metadata_from_adapter(adapter_for_scan)
        truncated = _scan_reports_truncation(scan_metadata)
        if truncated:
            scan_metadata = {**scan_metadata, "removals_skipped_due_to_truncation": True}
        previous = {
            item.relative_path: item
            for item in self._list_source_items(source.id, present_only=False)
        }
        current_by_relative = {item.relative_path: item for item in current_items}

        added: list[SourceItem] = []
        changed: list[SourceItem] = []
        removed: list[SourceItem] = []
        unchanged: list[SourceItem] = []
        queued: list[QueueItem] = []

        for item in current_items:
            old = previous.get(item.relative_path)
            should_enqueue = False
            if old is None or not old.present:
                added.append(item)
                should_enqueue = True
            elif old.sha256 != item.sha256 or old.size_bytes != item.size_bytes:
                changed.append(item)
                should_enqueue = True
            else:
                unchanged.append(item)
            self._upsert_source_item(item, created_at=old.created_at if old is not None else None)
            if should_enqueue:
                queue_item = self._enqueue_if_review(source, item)
                if queue_item is not None:
                    queued.append(queue_item)

        # A truncated discovery is a partial view of the source: absence from it is not
        # evidence of removal, so removals are skipped entirely rather than persisted.
        removal_candidates: list[SourceItem] = [] if truncated else list(previous.values())
        for old in removal_candidates:
            if old.present and old.relative_path not in current_by_relative:
                removed_item = SourceItem(
                    id=old.id,
                    source_id=old.source_id,
                    relative_path=old.relative_path,
                    absolute_path=old.absolute_path,
                    sha256=old.sha256,
                    size_bytes=old.size_bytes,
                    mtime_ns=old.mtime_ns,
                    present=False,
                    created_at=old.created_at,
                    updated_at=_now(),
                    metadata=old.metadata,
                )
                removed.append(removed_item)
                self._upsert_source_item(removed_item, created_at=old.created_at)

        self._connection.commit()
        return ScanDelta(
            source=source,
            added=added,
            changed=changed,
            removed=removed,
            unchanged=unchanged,
            queued=queued,
            metadata=scan_metadata,
        )

    def list_queue(self, state: QueueState | None = None) -> list[QueueItem]:
        if state is None:
            rows = self._connection.execute(
                "SELECT * FROM queue_items ORDER BY created_at, id"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM queue_items WHERE state = ? ORDER BY created_at, id",
                (state.value,),
            ).fetchall()
        return [_queue_from_row(row) for row in rows]

    def get_queue_item(self, queue_item_id: str) -> QueueItem:
        row = self._connection.execute(
            "SELECT * FROM queue_items WHERE id = ?",
            (queue_item_id,),
        ).fetchone()
        if row is None:
            raise AutomationError(f"queue item not found: {queue_item_id}")
        return _queue_from_row(row)

    def approve_queue_item(self, queue_item_id: str) -> QueueItem:
        self._reject_terminal_transition(queue_item_id, "approve")
        return self._set_queue_state(queue_item_id, QueueState.APPROVED, clear_error=True)

    def skip_queue_item(self, queue_item_id: str) -> QueueItem:
        self._reject_terminal_transition(queue_item_id, "skip")
        return self._set_queue_state(queue_item_id, QueueState.SKIPPED, clear_error=True)

    def retry_queue_item(self, queue_item_id: str) -> QueueItem:
        self._reject_terminal_transition(queue_item_id, "retry")
        return self._set_queue_state(queue_item_id, QueueState.DISCOVERED, clear_error=True)

    def _reject_terminal_transition(self, queue_item_id: str, action: str) -> None:
        queue_item = self.get_queue_item(queue_item_id)
        if queue_item.state in TERMINAL_QUEUE_STATES:
            raise AutomationError(
                f"queue item {queue_item.id} is in terminal state "
                f"{queue_item.state.value}; {action} is not a legal transition"
            )

    def ingest_queue_item(
        self,
        queue_item_id: str,
        output_root: Path,
        *,
        transcriber_command: str | None = None,
    ) -> IngestResult:
        queue_item = self.get_queue_item(queue_item_id)
        if queue_item.state is not QueueState.APPROVED:
            raise AutomationError("queue item requires explicit approval before ingest")
        source = self.get_source(queue_item.source_id)
        source_item = self.get_source_item(queue_item.source_item_id)
        if source.kind is SourceKind.YOUTUBE_PLAYLIST:
            self._record_unsupported_queue_item(queue_item.id, YOUTUBE_METADATA_ONLY_ERROR)
            raise AutomationError(YOUTUBE_METADATA_ONLY_ERROR)
        source_path = Path(source_item.absolute_path)
        root = Path(source.root_path).resolve() if source.kind is SourceKind.LOCAL_FOLDER else None
        try:
            current_digest, _ = _approval_digest_and_media_size(source_path, root=root)
        except AutomationError as exc:
            self._record_failed_queue_item(queue_item.id, str(exc))
            raise
        except OSError as exc:
            self._record_failed_queue_item(queue_item.id, str(exc))
            raise
        if current_digest != queue_item.content_sha256:
            message = "source file changed since queue approval; rescan before ingest"
            self._record_failed_queue_item(queue_item.id, message)
            raise AutomationError(message)
        planned_bundle_id: str | None = None
        try:
            if can_plan_local_bundle_id(source_path, transcriber_command):
                planned_bundle_id = plan_local_bundle_id(source_path, transcriber_command)
                if planned_bundle_id == queue_item.bundle_id:
                    completed_result = self._queue_owned_bundle_result(queue_item)
                    if completed_result is not None:
                        self._set_queue_state(
                            queue_item.id,
                            QueueState.COMPLETED,
                            bundle_id=planned_bundle_id,
                            clear_error=True,
                        )
                        return self._repaired_replay_result(
                            completed_result,
                            source=source,
                            source_item=source_item,
                            queue_item_id=queue_item.id,
                            consent="explicit_queue_approval",
                        )
                self._ensure_bundle_id_available(planned_bundle_id, queue_item, output_root)
            result = ingest_local(
                source_path,
                output_root,
                transcriber_command=transcriber_command,
            )
            try:
                self._ensure_library_bundle_id_available(result.manifest.bundle_id, queue_item)
            except AutomationError:
                shutil.rmtree(result.bundle_dir, ignore_errors=True)
                raise
        except AutomationError as exc:
            self._record_failed_queue_item(queue_item.id, str(exc))
            raise
        except (IngestError, OSError) as exc:
            if (
                isinstance(exc, IngestError)
                and queue_item.bundle_id is not None
                and _bundle_exists_error_matches(exc, queue_item.bundle_id)
            ):
                completed_result = self._queue_owned_bundle_result(queue_item)
                if completed_result is not None:
                    self._set_queue_state(
                        queue_item.id,
                        QueueState.COMPLETED,
                        bundle_id=queue_item.bundle_id,
                        clear_error=True,
                    )
                    return self._repaired_replay_result(
                        completed_result,
                        source=source,
                        source_item=source_item,
                        queue_item_id=queue_item.id,
                        consent="explicit_queue_approval",
                    )
            self._record_failed_queue_item(queue_item.id, str(exc))
            raise

        # Commit the completed state and the library record together so a crash can
        # never leave a COMPLETED queue row without the library row that makes the
        # bundle findable (and the on-disk bundle a retry blocker). Provenance is
        # attached afterwards so it can report the queue state the store actually
        # holds instead of asserting a literal.
        try:
            self._apply_queue_state(
                queue_item.id,
                QueueState.COMPLETED,
                bundle_id=result.manifest.bundle_id,
                clear_error=True,
            )
            library_record = self._record_library_bundle(
                result.bundle_dir, source, source_item, queue_item
            )
            self._connection.commit()
        except Exception as exc:
            self._connection.rollback()
            shutil.rmtree(result.bundle_dir, ignore_errors=True)
            self._record_failed_queue_item(queue_item.id, str(exc))
            raise
        completed = self.get_queue_item(queue_item.id)
        try:
            attach_provenance_to_bundle(
                result.bundle_dir,
                source=source,
                source_item=source_item,
                queue_item=completed,
                consent="explicit_queue_approval",
            )
        except Exception as exc:
            # Remove the half-written bundle: leaving it on disk with no library
            # record makes the retry path collide with an unrecorded directory.
            shutil.rmtree(result.bundle_dir, ignore_errors=True)
            self._undo_completed_ingest(
                queue_item.id,
                result.manifest.bundle_id,
                str(exc),
                library_record=library_record,
                restore_bundle_id=None,
            )
            raise
        return IngestResult(
            bundle_dir=result.bundle_dir,
            manifest=Manifest.load(result.bundle_dir),
        )

    def ingest_one_shot(
        self,
        source_path: Path,
        output_root: Path,
        *,
        transcriber_command: str | None = None,
    ) -> IngestResult:
        source_path = source_path.expanduser()
        if not source_path.is_file():
            raise IngestError(f"source file does not exist: {source_path}")
        planned_bundle_id = (
            plan_local_bundle_id(source_path, transcriber_command)
            if can_plan_local_bundle_id(source_path, transcriber_command)
            else None
        )
        source = self._ensure_one_shot_source(source_path)
        source_item = self._ensure_one_shot_item(source, source_path)
        queue_item = self._ensure_one_shot_queue(source, source_item)
        completed_bundle_id = queue_item.bundle_id
        if (
            planned_bundle_id is not None
            and queue_item.state is QueueState.COMPLETED
            and completed_bundle_id is not None
            and completed_bundle_id == planned_bundle_id
        ):
            completed_result = self._completed_bundle_result(completed_bundle_id)
            if completed_result is not None:
                return self._repaired_replay_result(
                    completed_result,
                    source=source,
                    source_item=source_item,
                    queue_item_id=queue_item.id,
                    consent="explicit_cli_invocation",
                )
        try:
            if planned_bundle_id is not None:
                self._ensure_bundle_id_available(planned_bundle_id, queue_item, output_root)
        except AutomationError as exc:
            self._record_failed_queue_item(queue_item.id, str(exc))
            raise
        try:
            result = ingest_local(
                source_path,
                output_root,
                transcriber_command=transcriber_command,
            )
        except (IngestError, OSError) as exc:
            if (
                isinstance(exc, IngestError)
                and completed_bundle_id is not None
                and _bundle_exists_error_matches(exc, completed_bundle_id)
            ):
                completed_result = self._completed_bundle_result(completed_bundle_id)
                if completed_result is not None:
                    return self._repaired_replay_result(
                        completed_result,
                        source=source,
                        source_item=source_item,
                        queue_item_id=queue_item.id,
                        consent="explicit_cli_invocation",
                    )
            if queue_item.state is not QueueState.COMPLETED:
                self._record_failed_queue_item(queue_item.id, str(exc))
            raise
        try:
            self._ensure_library_bundle_id_available(result.manifest.bundle_id, queue_item)
        except AutomationError as exc:
            shutil.rmtree(result.bundle_dir, ignore_errors=True)
            if queue_item.state is not QueueState.COMPLETED:
                self._record_failed_queue_item(queue_item.id, str(exc))
            raise

        # Mirror of the queue-ingest path: completion and its library record share one
        # transaction, and provenance runs against the committed state afterwards.
        try:
            self._apply_queue_state(
                queue_item.id,
                QueueState.COMPLETED,
                bundle_id=result.manifest.bundle_id,
                clear_error=True,
            )
            library_record = self._record_library_bundle(
                result.bundle_dir, source, source_item, queue_item
            )
            self._connection.commit()
        except Exception as exc:
            self._connection.rollback()
            shutil.rmtree(result.bundle_dir, ignore_errors=True)
            if queue_item.state is not QueueState.COMPLETED:
                self._record_failed_queue_item(queue_item.id, str(exc))
            raise
        completed = self.get_queue_item(queue_item.id)
        try:
            attach_provenance_to_bundle(
                result.bundle_dir,
                source=source,
                source_item=source_item,
                queue_item=completed,
                consent="explicit_cli_invocation",
            )
        except Exception as exc:
            # Mirror of the queue-ingest failure path: never leave a half-written
            # bundle that a later run would reject as an unrecorded directory.
            shutil.rmtree(result.bundle_dir, ignore_errors=True)
            # A rerun of an already-completed item must not lose the earlier success:
            # restore the prior COMPLETED bundle id rather than recording FAILED with
            # the new (now deleted) bundle on the row.
            restore_bundle_id = (
                completed_bundle_id
                if queue_item.state is QueueState.COMPLETED and completed_bundle_id is not None
                else None
            )
            self._undo_completed_ingest(
                queue_item.id,
                result.manifest.bundle_id,
                str(exc),
                library_record=library_record,
                restore_bundle_id=restore_bundle_id,
            )
            raise
        return IngestResult(
            bundle_dir=result.bundle_dir,
            manifest=Manifest.load(result.bundle_dir),
        )

    def get_source_item(self, source_item_id: str) -> SourceItem:
        row = self._connection.execute(
            "SELECT * FROM source_items WHERE id = ?",
            (source_item_id,),
        ).fetchone()
        if row is None:
            raise AutomationError(f"source item not found: {source_item_id}")
        return _source_item_from_row(row)

    def list_library(self) -> list[LibraryBundle]:
        rows = self._connection.execute(
            "SELECT * FROM library_bundles ORDER BY created_at, bundle_id"
        ).fetchall()
        return [_library_bundle_from_row(row) for row in rows]

    def get_library_bundle(self, bundle_id: str) -> LibraryBundle:
        row = self._connection.execute(
            "SELECT * FROM library_bundles WHERE bundle_id = ?",
            (bundle_id,),
        ).fetchone()
        if row is None:
            raise AutomationError(f"bundle not found in library: {bundle_id}")
        return _library_bundle_from_row(row)

    def _migrate(self) -> None:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version == STATE_SCHEMA_VERSION:
            return
        if version > STATE_SCHEMA_VERSION:
            raise AutomationError(
                f"unsupported automation state schema {version}; expected {STATE_SCHEMA_VERSION}"
            )
        if version == 0:
            self._create_schema_v2()
            return
        if version == 1:
            self._migrate_v1_to_v2()
            return
        raise AutomationError(
            f"unsupported automation state schema {version}; expected {STATE_SCHEMA_VERSION}"
        )

    def _create_schema_v2(self) -> None:
        self._connection.executescript(
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
                metadata_json TEXT NOT NULL DEFAULT '{}',
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
                metadata_json TEXT NOT NULL DEFAULT '{}',
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

            PRAGMA user_version = 2;
            """
        )
        self._connection.commit()

    def _migrate_v1_to_v2(self) -> None:
        # `ALTER TABLE` and `PRAGMA user_version` do not share a transaction, so a
        # process killed mid-migration leaves columns present at user_version 1. Each
        # step is therefore guarded and re-runnable, and the v1 file is copied aside
        # first so the pre-migration bytes survive a failed upgrade.
        self._backup_v1_store()
        for table in ("source_items", "queue_items"):
            if self._table_has_column(table, "metadata_json"):
                continue
            self._connection.execute(
                f"ALTER TABLE {table} ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{{}}'"
            )
        self._connection.execute(f"PRAGMA user_version = {STATE_SCHEMA_VERSION}")
        self._connection.commit()

    def _table_has_column(self, table: str, column: str) -> bool:
        rows = self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        return any(cast(str, row["name"]) == column for row in rows)

    def _backup_v1_store(self) -> None:
        if not self.path.is_file():
            return
        backup = self.path.with_name(f"{self.path.name}.v1.bak")
        if backup.exists():
            return
        # SQLite's backup API captures the committed database regardless of
        # journal mode; a plain file copy can miss changes still living in an
        # adjacent -wal file — the exact case where the backup matters.
        #
        # The backup is written to a temporary name and published with os.replace so
        # an interrupted run cannot leave a partial file that the existence check
        # above would later trust as a complete pre-migration copy.
        temporary = self.path.with_name(f"{backup.name}.tmp")
        temporary.unlink(missing_ok=True)
        # sqlite3.connect would create the file under the process umask, so the
        # copy would hold the database's bytes under broader permissions for the
        # length of the backup (and past a SIGKILL). Create it with the source
        # database's own mode first, before any bytes are written into it.
        mode = self.path.stat().st_mode & 0o777
        os.close(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode))
        try:
            # The umask also masks os.open's mode, so restate it explicitly.
            os.chmod(temporary, mode)
            destination = sqlite3.connect(temporary)
            try:
                self._connection.backup(destination)
            finally:
                destination.close()
            os.replace(temporary, backup)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _list_source_items(self, source_id: str, *, present_only: bool) -> list[SourceItem]:
        if present_only:
            rows = self._connection.execute(
                "SELECT * FROM source_items WHERE source_id = ? AND present = 1",
                (source_id,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM source_items WHERE source_id = ?",
                (source_id,),
            ).fetchall()
        return [_source_item_from_row(row) for row in rows]

    def _upsert_source_item(self, item: SourceItem, *, created_at: str | None) -> None:
        self._connection.execute(
            """
            INSERT INTO source_items(
                id, source_id, relative_path, absolute_path, sha256, size_bytes, mtime_ns,
                present, created_at, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, relative_path) DO UPDATE SET
                absolute_path = excluded.absolute_path,
                sha256 = excluded.sha256,
                size_bytes = excluded.size_bytes,
                mtime_ns = excluded.mtime_ns,
                present = excluded.present,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (
                item.id,
                item.source_id,
                item.relative_path,
                item.absolute_path,
                item.sha256,
                item.size_bytes,
                item.mtime_ns,
                1 if item.present else 0,
                created_at or item.created_at,
                item.updated_at,
                _metadata_to_json(item.metadata),
            ),
        )

    def _enqueue_if_review(self, source: SourceRecord, item: SourceItem) -> QueueItem | None:
        if source.policy is not SourcePolicy.REVIEW:
            return None
        if _is_placeholder_item(item):
            return None
        queue_item_id = _queue_item_id(item.id, item.sha256)
        now = _now()
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO queue_items(
                id, source_id, source_item_id, content_sha256, state, policy,
                bundle_id, attempts, last_error, created_at, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?, ?, ?)
            """,
            (
                queue_item_id,
                source.id,
                item.id,
                item.sha256,
                QueueState.DISCOVERED.value,
                source.policy.value,
                now,
                now,
                _metadata_to_json(item.metadata),
            ),
        )
        if cursor.rowcount == 0:
            return None
        return self.get_queue_item(queue_item_id)

    def _set_queue_state(
        self,
        queue_item_id: str,
        state: QueueState,
        *,
        bundle_id: str | None = None,
        clear_error: bool = False,
    ) -> QueueItem:
        queue_item = self._apply_queue_state(
            queue_item_id, state, bundle_id=bundle_id, clear_error=clear_error
        )
        self._connection.commit()
        return self.get_queue_item(queue_item.id)

    def _apply_queue_state(
        self,
        queue_item_id: str,
        state: QueueState,
        *,
        bundle_id: str | None = None,
        clear_error: bool = False,
    ) -> QueueItem:
        """Stage a queue-state transition without committing; the caller commits."""

        existing = self.get_queue_item(queue_item_id)
        self._connection.execute(
            """
            UPDATE queue_items
            SET state = ?, bundle_id = COALESCE(?, bundle_id),
                last_error = CASE WHEN ? THEN NULL ELSE last_error END,
                updated_at = ?
            WHERE id = ?
            """,
            (state.value, bundle_id, 1 if clear_error else 0, _now(), existing.id),
        )
        return existing

    def _undo_completed_ingest(
        self,
        queue_item_id: str,
        bundle_id: str,
        message: str,
        *,
        library_record: _LibraryRecordOutcome,
        restore_bundle_id: str | None,
    ) -> None:
        """Reverse a committed completion in one transaction after provenance failed."""

        if library_record.inserted:
            self._connection.execute(
                "DELETE FROM library_bundles WHERE bundle_id = ? AND queue_item_id = ?",
                (bundle_id, queue_item_id),
            )
        elif library_record.previous is not None:
            # The row was updated onto the bundle that has just been deleted; the
            # earlier bundle survives on disk, so the library must point back at it.
            previous = library_record.previous
            self._connection.execute(
                """
                UPDATE library_bundles
                SET bundle_path = ?, source_id = ?, source_item_id = ?, queue_item_id = ?
                WHERE bundle_id = ?
                """,
                (
                    previous.bundle_path,
                    previous.source_id,
                    previous.source_item_id,
                    previous.queue_item_id,
                    previous.bundle_id,
                ),
            )
        if restore_bundle_id is None:
            self._apply_failed_queue_item(queue_item_id, message)
        else:
            self._apply_queue_state(
                queue_item_id,
                QueueState.COMPLETED,
                bundle_id=restore_bundle_id,
                clear_error=True,
            )
        self._connection.commit()

    def _record_failed_queue_item(self, queue_item_id: str, message: str) -> None:
        self._apply_failed_queue_item(queue_item_id, message)
        self._connection.commit()

    def _apply_failed_queue_item(self, queue_item_id: str, message: str) -> None:
        """Stage a failure transition without committing; the caller commits."""

        queue_item = self.get_queue_item(queue_item_id)
        self._connection.execute(
            """
            UPDATE queue_items
            SET state = ?, attempts = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                QueueState.FAILED.value,
                queue_item.attempts + 1,
                message,
                _now(),
                queue_item.id,
            ),
        )
        self._connection.commit()

    def _record_unsupported_queue_item(self, queue_item_id: str, message: str) -> None:
        """Record a terminal unsupported-by-design outcome without spending an attempt."""

        queue_item = self.get_queue_item(queue_item_id)
        self._connection.execute(
            """
            UPDATE queue_items
            SET state = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                QueueState.UNSUPPORTED.value,
                message,
                _now(),
                queue_item.id,
            ),
        )
        self._connection.commit()

    def _ensure_one_shot_source(self, source_path: Path) -> SourceRecord:
        source = source_path.resolve()
        source_id = _source_id(SourceKind.ONE_SHOT.value, str(source))
        now = _now()
        self._connection.execute(
            """
            INSERT INTO sources(id, kind, name, root_path, policy, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (
                source_id,
                SourceKind.ONE_SHOT.value,
                f"one-shot:{source.name}:{source_id[-8:]}",
                str(source),
                SourcePolicy.REVIEW.value,
                now,
                now,
            ),
        )
        self._connection.commit()
        return self.get_source(source_id)

    def _ensure_one_shot_item(self, source: SourceRecord, source_path: Path) -> SourceItem:
        path = source_path.resolve()
        digest, size = _approval_digest_and_media_size(path)
        stat = path.stat()
        now = _now()
        item = SourceItem(
            id=_source_item_id(source.id, path.name),
            source_id=source.id,
            relative_path=path.name,
            absolute_path=str(path),
            sha256=digest,
            size_bytes=size,
            mtime_ns=stat.st_mtime_ns,
            present=True,
            created_at=now,
            updated_at=now,
        )
        old = self._connection.execute(
            "SELECT * FROM source_items WHERE id = ?",
            (item.id,),
        ).fetchone()
        self._upsert_source_item(
            item,
            created_at=_source_item_from_row(old).created_at if old is not None else None,
        )
        self._connection.commit()
        return self.get_source_item(item.id)

    def _ensure_one_shot_queue(self, source: SourceRecord, item: SourceItem) -> QueueItem:
        queue_item_id = _queue_item_id(item.id, item.sha256)
        existing = self._connection.execute(
            "SELECT * FROM queue_items WHERE id = ?",
            (queue_item_id,),
        ).fetchone()
        if existing is not None:
            queue_item = _queue_from_row(existing)
            if queue_item.state is QueueState.COMPLETED:
                return queue_item
            return self._set_queue_state(queue_item.id, QueueState.APPROVED, clear_error=True)

        now = _now()
        self._connection.execute(
            """
            INSERT INTO queue_items(
                id, source_id, source_item_id, content_sha256, state, policy,
                bundle_id, attempts, last_error, created_at, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?, ?, ?)
            """,
            (
                queue_item_id,
                source.id,
                item.id,
                item.sha256,
                QueueState.APPROVED.value,
                source.policy.value,
                now,
                now,
                _metadata_to_json(item.metadata),
            ),
        )
        self._connection.commit()
        return self.get_queue_item(queue_item_id)

    def _record_library_bundle(
        self,
        bundle_dir: Path,
        source: SourceRecord,
        source_item: SourceItem,
        queue_item: QueueItem,
    ) -> _LibraryRecordOutcome:
        """Stage the library record without committing; reports what it changed.

        The caller commits, so completion state and library record land together,
        and keeps the outcome so a later failure can restore the prior row.
        """

        manifest = Manifest.load(bundle_dir)
        existing = self._existing_library_bundle(manifest.bundle_id)
        values = (
            str(bundle_dir.resolve()),
            source.id,
            source_item.id,
            queue_item.id,
        )
        if existing is None:
            self._connection.execute(
                """
                INSERT INTO library_bundles(
                    bundle_id, bundle_path, source_id, source_item_id, queue_item_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.bundle_id,
                    *values,
                    _now(),
                ),
            )
            return _LibraryRecordOutcome(inserted=True, previous=None)
        if existing.queue_item_id == queue_item.id:
            self._connection.execute(
                """
                UPDATE library_bundles
                SET bundle_path = ?, source_id = ?, source_item_id = ?, queue_item_id = ?
                WHERE bundle_id = ?
                """,
                (*values, manifest.bundle_id),
            )
            return _LibraryRecordOutcome(inserted=False, previous=existing)
        raise AutomationError(
            "bundle id already exists for different source provenance; "
            "duplicate-content multi-source provenance is not implemented"
        )

    def _ensure_bundle_id_available(
        self, bundle_id: str, queue_item: QueueItem, output_root: Path
    ) -> None:
        existing_bundle = self._existing_library_bundle(bundle_id)
        if existing_bundle is not None:
            if existing_bundle.queue_item_id == queue_item.id:
                return
            raise AutomationError(
                "bundle id already exists for different source provenance; "
                "duplicate-content multi-source provenance is not implemented"
            )
        bundle_dir = output_root / bundle_id
        if bundle_dir.exists():
            raise AutomationError(
                "bundle id already exists on disk but is not recorded in state; "
                "choose a different output directory or remove the existing bundle"
            )

    def _ensure_library_bundle_id_available(self, bundle_id: str, queue_item: QueueItem) -> None:
        existing_bundle = self._existing_library_bundle(bundle_id)
        if existing_bundle is None or existing_bundle.queue_item_id == queue_item.id:
            return
        raise AutomationError(
            "bundle id already exists for different source provenance; "
            "duplicate-content multi-source provenance is not implemented"
        )

    def _existing_library_bundle(self, bundle_id: str) -> LibraryBundle | None:
        existing = self._connection.execute(
            "SELECT * FROM library_bundles WHERE bundle_id = ?",
            (bundle_id,),
        ).fetchone()
        if existing is None:
            return None
        return _library_bundle_from_row(existing)

    def _completed_bundle_result(self, bundle_id: str) -> IngestResult | None:
        try:
            library_bundle = self.get_library_bundle(bundle_id)
        except AutomationError:
            return None
        bundle_dir = Path(library_bundle.bundle_path)
        if not bundle_dir.is_dir():
            return None
        return IngestResult(
            bundle_dir=bundle_dir,
            manifest=Manifest.load(bundle_dir),
        )

    def _repaired_replay_result(
        self,
        result: IngestResult,
        *,
        source: SourceRecord,
        source_item: SourceItem,
        queue_item_id: str,
        consent: str,
    ) -> IngestResult:
        """Re-attach provenance to a replayed bundle left stale by a crash.

        Completion commits before provenance is attached, so a replay can find a
        completed, library-recorded bundle whose provenance never landed. Re-attach
        is idempotent (identical inputs rewrite identical bytes), so a healthy
        bundle is returned untouched.
        """

        if not _bundle_provenance_needs_repair(result.bundle_dir):
            return result
        attach_provenance_to_bundle(
            result.bundle_dir,
            source=source,
            source_item=source_item,
            queue_item=self.get_queue_item(queue_item_id),
            consent=consent,
        )
        return IngestResult(
            bundle_dir=result.bundle_dir,
            manifest=Manifest.load(result.bundle_dir),
        )

    def _queue_owned_bundle_result(self, queue_item: QueueItem) -> IngestResult | None:
        if queue_item.bundle_id is None:
            return None
        try:
            library_bundle = self.get_library_bundle(queue_item.bundle_id)
        except AutomationError:
            return None
        if library_bundle.queue_item_id != queue_item.id:
            return None
        bundle_dir = Path(library_bundle.bundle_path)
        if not bundle_dir.is_dir():
            return None
        return IngestResult(
            bundle_dir=bundle_dir,
            manifest=Manifest.load(bundle_dir),
        )


def open_state(path: Path = DEFAULT_STATE_PATH) -> AutomationState:
    try:
        return AutomationState(path)
    except sqlite3.Error as exc:
        raise AutomationError(f"state database error: {exc}") from exc


def preflight_local_folder(path: Path) -> SourcePreflight:
    resolved = path.resolve()
    exists = resolved.exists()
    is_dir = resolved.is_dir()
    readable = False
    media_files = 0
    if is_dir:
        try:
            media_files = sum(
                1
                for media in iter_local_media_files(resolved)
                if not _transcript_sidecar_escapes_root(media, resolved)
            )
            readable = True
        except OSError:
            readable = False
    return SourcePreflight(
        path=str(resolved),
        exists=exists,
        is_dir=is_dir,
        readable=readable,
        media_files=media_files,
    )


def preflight_state_store(path: Path = DEFAULT_STATE_PATH) -> StateStorePreflight:
    resolved = path.resolve()
    writable_location = _nearest_existing_parent_is_writable(resolved)
    exists = resolved.exists()
    schema_version: int | None = None
    error: str | None = None
    if exists:
        writable_location = writable_location and os.access(resolved, os.W_OK)
        try:
            with sqlite3.connect(f"file:{resolved}?mode=ro", uri=True) as connection:
                schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.Error as exc:
            error = str(exc)
        if schema_version not in (None, *UPGRADABLE_STATE_SCHEMA_VERSIONS):
            error = (
                f"unsupported automation state schema {schema_version}; "
                f"expected {STATE_SCHEMA_VERSION}"
            )
    return StateStorePreflight(
        path=str(path),
        exists=exists,
        writable_location=writable_location,
        schema_version=schema_version,
        error=error,
    )


def preflight_youtube_playlist(
    playlist: str,
    *,
    api_key: str | None = None,
    api_key_env: str = DEFAULT_YOUTUBE_API_KEY_ENV,
    environ: Mapping[str, str] | None = None,
    transport: HttpGet | None = None,
) -> YouTubePreflight:
    # Resolve the credential before normalization so an invalid playlist still
    # reports whether a key was supplied; reporting "absent" would send the
    # operator after a credential they already have.
    env = environ if environ is not None else os.environ
    resolved_api_key = api_key if api_key is not None else env.get(api_key_env, "")
    try:
        playlist_id = normalize_youtube_playlist_id(playlist)
    except AutomationError as exc:
        return YouTubePreflight(
            playlist_id=playlist,
            api_key_env=api_key_env,
            credential_present=bool(resolved_api_key),
            reachable=False,
            pages_checked=0,
            estimated_units_consumed=0,
            error=str(exc),
        )

    if not resolved_api_key:
        return YouTubePreflight(
            playlist_id=playlist_id,
            api_key_env=api_key_env,
            credential_present=False,
            reachable=False,
            pages_checked=0,
            estimated_units_consumed=0,
            error=f"missing YouTube API key; set {api_key_env}",
        )

    adapter = YouTubePlaylistAdapter(
        resolved_api_key,
        api_key_env=api_key_env,
        transport=transport,
        max_pages=1,
        max_results=1,
    )
    source = SourceRecord(
        id=_source_id(SourceKind.YOUTUBE_PLAYLIST.value, playlist_id),
        kind=SourceKind.YOUTUBE_PLAYLIST,
        name="youtube-preflight",
        root_path=playlist_id,
        policy=SourcePolicy.SCAN_ONLY,
        created_at=_now(),
        updated_at=_now(),
    )
    try:
        adapter.discover(source)
    except AutomationError as exc:
        # A page request that failed after it was issued still spent its quota
        # unit; reporting zero would understate what the preflight consumed.
        return YouTubePreflight(
            playlist_id=playlist_id,
            api_key_env=api_key_env,
            credential_present=True,
            reachable=False,
            pages_checked=adapter.pages_attempted,
            estimated_units_consumed=adapter.attempted_quota_units,
            error=str(exc),
        )
    metadata = adapter.scan_metadata
    quota = cast(dict[str, Any], metadata.get("quota", {}))
    pages_checked = int(quota.get("pages_fetched", 1))
    estimated_units = int(quota.get("estimated_units_consumed", pages_checked))
    return YouTubePreflight(
        playlist_id=playlist_id,
        api_key_env=api_key_env,
        credential_present=True,
        reachable=True,
        pages_checked=pages_checked,
        estimated_units_consumed=estimated_units,
        error=None,
    )


def attach_provenance_to_bundle(
    bundle_dir: Path,
    *,
    source: SourceRecord,
    source_item: SourceItem,
    queue_item: QueueItem,
    consent: str,
) -> None:
    source_path = bundle_dir / "source.json"
    source_payload = cast(dict[str, Any], json.loads(source_path.read_text(encoding="utf-8")))
    source_payload["provenance"] = {
        "state_schema_version": STATE_SCHEMA_VERSION,
        "source_id": source.id,
        "source_kind": source.kind.value,
        "source_name": source.name,
        "source_item_id": source_item.id,
        "queue_item_id": queue_item.id,
        "queue_state": queue_item.state.value,
        "policy": queue_item.policy.value,
        "consent": consent,
        "remote_services": _bundle_remote_services(source_payload),
    }
    # Publish atomically: the queue/library rows that point at this bundle are
    # already committed, and _bundle_provenance_needs_repair deliberately gives
    # up on unparseable base content, so a half-written source.json would be
    # unrecoverable by the replay repair path.
    atomic_write_text(source_path, json.dumps(source_payload, indent=2) + "\n")

    manifest = Manifest.load(bundle_dir)
    acquire = manifest.stages[StageName.ACQUIRE]
    updated_outputs: list[ArtifactRef] = []
    for output in acquire.outputs:
        if output.path == "source.json":
            digest, size = _digest_and_size(source_path)
            updated_outputs.append(ArtifactRef(path=output.path, sha256=digest, bytes=size))
        else:
            updated_outputs.append(output)
    acquire.outputs = updated_outputs
    manifest.save(bundle_dir)


def _bundle_provenance_needs_repair(bundle_dir: Path) -> bool:
    """Report whether a completed bundle's automation provenance is out of date.

    Completion and the library row commit before provenance is attached, so a
    crash in that window can leave a completed, library-recorded bundle whose
    source.json lacks provenance, or whose manifest still records the
    pre-provenance source.json digest. Both are repairable by re-attaching.
    """

    source_path = bundle_dir / "source.json"
    try:
        payload_obj = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Nothing re-attaching could fix; leave the bundle exactly as found.
        return False
    if not isinstance(payload_obj, dict):
        return False
    payload = cast(dict[str, Any], payload_obj)
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return True
    if not set(cast(dict[str, Any], provenance)) >= PROVENANCE_KEYS:
        return True

    try:
        manifest = Manifest.load(bundle_dir)
        acquire = manifest.stages[StageName.ACQUIRE]
    except (OSError, KeyError, ValueError):
        return False
    recorded = next(
        (output.sha256 for output in acquire.outputs if output.path == "source.json"),
        None,
    )
    if recorded is None:
        return False
    try:
        digest, _ = _digest_and_size(source_path)
    except OSError:
        return False
    return recorded != digest


def _bundle_remote_services(source_payload: dict[str, Any]) -> dict[str, Any]:
    """Record the bundle's own remote-services metadata instead of fresh literals."""

    recorded: dict[str, Any] = {}
    transcript = source_payload.get("transcript")
    if isinstance(transcript, dict):
        candidate = cast(dict[str, Any], transcript).get("remote_services")
        if isinstance(candidate, dict):
            recorded = cast(dict[str, Any], candidate)
    return {
        "allowed": recorded.get("allowed", False),
        "scope": recorded.get("scope", "lectern_core"),
        "lectern_invoked": recorded.get("lectern_invoked", False),
        "requires_explicit_per_item_consent": recorded.get(
            "requires_explicit_per_item_consent", True
        ),
        "transcriber_network_posture": recorded.get("transcriber_network_posture", "not_recorded"),
    }


def state_summary(path: Path) -> dict[str, Any]:
    with open_state(path) as state:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "state_path": str(path),
            "sources": len(state.list_sources()),
            "queue": len(state.list_queue()),
            "library": len(state.list_library()),
        }


def _source_from_row(row: sqlite3.Row) -> SourceRecord:
    return SourceRecord(
        id=cast(str, row["id"]),
        kind=SourceKind(cast(str, row["kind"])),
        name=cast(str, row["name"]),
        root_path=cast(str, row["root_path"]),
        policy=SourcePolicy(cast(str, row["policy"])),
        created_at=cast(str, row["created_at"]),
        updated_at=cast(str, row["updated_at"]),
    )


def _source_item_from_row(row: sqlite3.Row) -> SourceItem:
    return SourceItem(
        id=cast(str, row["id"]),
        source_id=cast(str, row["source_id"]),
        relative_path=cast(str, row["relative_path"]),
        absolute_path=cast(str, row["absolute_path"]),
        sha256=cast(str, row["sha256"]),
        size_bytes=cast(int, row["size_bytes"]),
        mtime_ns=cast(int, row["mtime_ns"]),
        present=bool(row["present"]),
        created_at=cast(str, row["created_at"]),
        updated_at=cast(str, row["updated_at"]),
        metadata=_metadata_from_row(row),
    )


def _queue_from_row(row: sqlite3.Row) -> QueueItem:
    return QueueItem(
        id=cast(str, row["id"]),
        source_id=cast(str, row["source_id"]),
        source_item_id=cast(str, row["source_item_id"]),
        content_sha256=cast(str, row["content_sha256"]),
        state=QueueState(cast(str, row["state"])),
        policy=SourcePolicy(cast(str, row["policy"])),
        bundle_id=cast(str | None, row["bundle_id"]),
        attempts=cast(int, row["attempts"]),
        last_error=cast(str | None, row["last_error"]),
        created_at=cast(str, row["created_at"]),
        updated_at=cast(str, row["updated_at"]),
        metadata=_metadata_from_row(row),
    )


def _library_bundle_from_row(row: sqlite3.Row) -> LibraryBundle:
    return LibraryBundle(
        bundle_id=cast(str, row["bundle_id"]),
        bundle_path=cast(str, row["bundle_path"]),
        source_id=cast(str, row["source_id"]),
        source_item_id=cast(str, row["source_item_id"]),
        queue_item_id=cast(str, row["queue_item_id"]),
        created_at=cast(str, row["created_at"]),
    )


def normalize_youtube_playlist_id(playlist: str) -> str:
    value = playlist.strip()
    if not value:
        raise AutomationError("YouTube playlist ID is required")
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError as exc:
        # urlparse raises on inputs like "http://[broken"; callers (CLI included)
        # only handle AutomationError, so let the domain error carry it.
        raise AutomationError("YouTube playlist ID or URL could not be parsed") from exc
    if parsed.scheme or parsed.netloc:
        query = urllib.parse.parse_qs(parsed.query)
        values = query.get("list", [])
        value = values[0].strip() if values else ""
        if not value:
            raise AutomationError("YouTube playlist URL must include a non-empty list parameter")
    if any(character.isspace() for character in value):
        raise AutomationError("YouTube playlist ID must not contain whitespace")
    return value


def _default_source_adapter(source: SourceRecord) -> SourceAdapter:
    if source.kind is SourceKind.LOCAL_FOLDER:
        return LocalFolderAdapter()
    if source.kind is SourceKind.YOUTUBE_PLAYLIST:
        return YouTubePlaylistAdapter.from_environment()
    raise AutomationError(f"unsupported source kind for scan: {source.kind.value}")


def _scan_metadata_from_adapter(adapter: SourceAdapter) -> dict[str, Any]:
    if isinstance(adapter, ScanMetadataProvider):
        return adapter.scan_metadata
    return {}


def _scan_reports_truncation(scan_metadata: dict[str, Any]) -> bool:
    quota = scan_metadata.get("quota")
    if isinstance(quota, dict) and cast(dict[str, Any], quota).get("truncated_by_max_pages"):
        return True
    return bool(scan_metadata.get("truncated_by_max_pages"))


def _dedupe_by_relative_path(items: Iterable[SourceItem]) -> list[SourceItem]:
    """Collapse repeats of one identity within a single discovery; first occurrence wins."""

    seen: set[str] = set()
    deduped: list[SourceItem] = []
    for item in items:
        if item.relative_path in seen:
            continue
        seen.add(item.relative_path)
        deduped.append(item)
    return deduped


def _is_placeholder_item(item: SourceItem) -> bool:
    video = item.metadata.get("video")
    if not isinstance(video, dict):
        return False
    return bool(cast(dict[str, Any], video).get("placeholder"))


def _youtube_source_item(
    source: SourceRecord,
    playlist_id: str,
    raw: object,
    *,
    page_index: int,
) -> SourceItem:
    if not isinstance(raw, dict):
        raise AutomationError("YouTube API response item was not an object")
    item = cast(dict[str, Any], raw)
    snippet = _object_field(item, "snippet")
    content_details = _object_field(item, "contentDetails")
    playlist_item_id = _optional_string(item.get("id"))
    video_id = _optional_string(content_details.get("videoId"))
    if video_id is None:
        resource_id = _object_field(snippet, "resourceId")
        video_id = _optional_string(resource_id.get("videoId"))
    if video_id is None:
        raise AutomationError("YouTube playlist item missing video ID")
    # Identity is the video, not the playlist slot: playlist-item IDs change on
    # remove-and-re-add and repeat when one video is listed twice in a playlist.
    relative_path = f"{playlist_id}/{video_id}"
    playlist_url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
    video_url = (
        "https://www.youtube.com/watch?"
        f"{urllib.parse.urlencode({'v': video_id, 'list': playlist_id})}"
    )
    title = _optional_string(snippet.get("title"))
    channel_id = _optional_string(snippet.get("channelId"))
    channel_title = _optional_string(snippet.get("channelTitle"))
    published_at = _optional_string(snippet.get("publishedAt"))
    # videoOwnerChannel* are snippet properties; contentDetails carries videoPublishedAt.
    video_owner_channel_id = _optional_string(snippet.get("videoOwnerChannelId"))
    video_owner_channel_title = _optional_string(snippet.get("videoOwnerChannelTitle"))
    video_published_at = _optional_string(content_details.get("videoPublishedAt"))
    position = _optional_int(snippet.get("position"))
    # Title alone does not identify a tombstone: a real public video may be
    # titled "Private video". API tombstones also drop the availability fields
    # real entries carry, so require their absence before excluding the item.
    placeholder = (
        title in YOUTUBE_PLACEHOLDER_TITLES
        and video_owner_channel_id is None
        and video_published_at is None
    )
    # Digest holds content-meaningful fields only. Playlist item ID, position,
    # and timestamps are positional/curation noise excluded per accepted design
    # constraint H1: including them turns reorders and remove-and-re-adds into
    # spurious re-enqueues.
    digest_payload = {
        "playlist_id": playlist_id,
        "video_id": video_id,
        "title": title,
        "channel_id": channel_id,
        "channel_title": channel_title,
        "video_owner_channel_id": video_owner_channel_id,
        "video_owner_channel_title": video_owner_channel_title,
    }
    metadata = {
        "source": {
            "kind": SourceKind.YOUTUBE_PLAYLIST.value,
            "source_id": source.id,
            "source_name": source.name,
        },
        "playlist": {
            "id": playlist_id,
            "url": playlist_url,
        },
        "playlist_item": {
            "id": playlist_item_id,
            "position": position,
            "published_at": published_at,
        },
        "video": {
            "id": video_id,
            "url": video_url,
            "title": title,
            "channel_id": channel_id,
            "channel_title": channel_title,
            "video_owner_channel_id": video_owner_channel_id,
            "video_owner_channel_title": video_owner_channel_title,
            "published_at": video_published_at,
            "placeholder": placeholder,
        },
        "discovery": {
            "adapter": "youtube-playlist",
            "api": "youtube-data-api-v3",
            "method": "playlistItems.list",
            "part": YOUTUBE_PLAYLIST_PARTS,
            "page_index": page_index,
            "units_per_page": YOUTUBE_PLAYLIST_QUOTA_UNITS_PER_PAGE,
        },
    }
    now = _now()
    return SourceItem(
        id=_source_item_id(source.id, relative_path),
        source_id=source.id,
        relative_path=relative_path,
        absolute_path=video_url,
        sha256=_metadata_digest(digest_payload),
        size_bytes=0,
        mtime_ns=0,
        present=True,
        created_at=now,
        updated_at=now,
        metadata=metadata,
    )


def _urllib_get(url: str, timeout_s: float) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return cast(bytes, response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except (http.client.HTTPException, OSError):
            # A truncated error body must not escape as a raw protocol error:
            # exceptions raised inside this handler bypass the sibling handlers
            # below, and the status code alone still makes a usable domain error.
            body = b""
        raise _youtube_error_from_response(exc.code, body) from exc
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        # http.client raises HTTPException (IncompleteRead and friends) outside the
        # URLError/OSError hierarchy, so a truncated response would otherwise escape
        # as a raw protocol error instead of an AutomationError.
        detail = getattr(exc, "reason", exc)
        raise AutomationError(f"YouTube Data API request failed: {detail}") from exc


def _youtube_error_from_response(status_code: int, body: bytes) -> YouTubeAPIError:
    reason: str | None = None
    message: str | None = None
    try:
        payload_obj: object = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload_obj = None
    if isinstance(payload_obj, dict):
        payload = cast(dict[str, Any], payload_obj)
        error_obj = payload.get("error")
        if isinstance(error_obj, dict):
            error = cast(dict[str, Any], error_obj)
            message = _optional_string(error.get("message"))
            errors_obj = error.get("errors")
            if isinstance(errors_obj, list) and errors_obj:
                errors = cast(list[object], errors_obj)
                first_error = errors[0]
                if isinstance(first_error, dict):
                    reason = _optional_string(cast(dict[str, Any], first_error).get("reason"))
    reason_part = f" {reason}" if reason else ""
    detail = message or "request failed"
    return YouTubeAPIError(
        f"YouTube Data API error ({status_code}{reason_part}): {detail}",
        status_code=status_code,
        reason=reason,
    )


def _object_field(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AutomationError(f"YouTube API response field is not an object: {key}")
    return cast(dict[str, Any], value)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _metadata_from_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        raw_value = row["metadata_json"]
    except (IndexError, KeyError):
        return {}
    raw = cast(str, raw_value)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _metadata_to_json(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _metadata_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_metadata_to_json(payload).encode("utf-8")).hexdigest()


def _bundle_exists_error_matches(exc: IngestError, bundle_id: str) -> bool:
    prefix = "bundle already exists: "
    message = str(exc)
    return message.startswith(prefix) and Path(message.removeprefix(prefix)).name == bundle_id


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _approval_digest_and_media_size(path: Path, *, root: Path | None = None) -> tuple[str, int]:
    media_digest, media_size = _digest_and_size(path)
    sidecar = path.with_suffix(".transcript.txt")
    digest = hashlib.sha256()
    digest.update(b"media")
    digest.update(b"\0")
    digest.update(media_digest.encode("ascii"))
    digest.update(b"\0")
    if sidecar.is_file():
        if root is not None and _transcript_sidecar_escapes_root(path, root):
            raise AutomationError("transcript sidecar must be inside the source root")
        sidecar_digest, _ = _digest_and_size(sidecar)
        digest.update(b"transcript-sidecar")
        digest.update(b"\0")
        digest.update(sidecar_digest.encode("ascii"))
    else:
        digest.update(b"transcript-sidecar-absent")
    return digest.hexdigest(), media_size


def _transcript_sidecar_escapes_root(path: Path, root: Path) -> bool:
    """Report whether a media file's transcript sidecar resolves outside `root`.

    `LocalFolderAdapter.discover` drops such media (the sidecar is content the
    approval digest would cover, so it must stay inside the source), and
    `preflight_local_folder` applies the same rule so its count matches discovery.
    """

    sidecar = path.with_suffix(".transcript.txt")
    if not sidecar.is_file():
        return False
    if sidecar.is_symlink():
        return True
    try:
        sidecar.resolve().relative_to(root)
    except ValueError:
        return True
    return False


def _is_bundle_output_path(root: Path, path: Path) -> bool:
    ancestor = path.parent
    while True:
        if (ancestor / MANIFEST_NAME).is_file() and (ancestor / "source.json").is_file():
            return True
        if ancestor == root:
            return False
        ancestor = ancestor.parent


def _nearest_existing_parent_is_writable(path: Path) -> bool:
    candidate = path.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK)


def _source_id(kind: str, root_path: str) -> str:
    return _stable_id("src", [kind, root_path])


def _source_item_id(source_id: str, relative_path: str) -> str:
    return _stable_id("item", [source_id, relative_path])


def _queue_item_id(source_item_id: str, content_sha256: str) -> str:
    return _stable_id("queue", [source_item_id, content_sha256])


def _stable_id(prefix: str, parts: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return f"{prefix}_{digest.hexdigest()[:16]}"
