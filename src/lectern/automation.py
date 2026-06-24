"""Source registry, discovery queue, and library state.

The automation spine records source discovery policy, queues reviewable items,
and bridges approved local items into the existing bundle ingest path. External
adapters may discover metadata, but local-source media artifacts stay local
unless a future stage adds explicit per-item consent.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, Self, cast, runtime_checkable

from lectern.bundle import MANIFEST_NAME, ArtifactRef, Manifest, StageName
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


class LocalFolderAdapter:
    """Discover media files under a local directory without network access."""

    def discover(self, source: SourceRecord) -> list[SourceItem]:
        root = Path(source.root_path)
        if not root.is_dir():
            raise AutomationError(f"local folder source is not a directory: {root}")

        root_resolved = root.resolve()
        items: list[SourceItem] = []
        for path in sorted(root.rglob("*")):
            relative_parts = path.parent.relative_to(root).parts
            is_excluded_dir = any(part in EXCLUDED_SCAN_DIR_NAMES for part in relative_parts)
            is_excluded_temp_dir = any(
                part.startswith(EXCLUDED_SCAN_DIR_PREFIXES) for part in relative_parts
            )
            if is_excluded_dir or is_excluded_temp_dir or _is_bundle_output_path(root, path):
                continue
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix.lower() not in MEDIA_EXTENSIONS
            ):
                continue
            absolute = path.resolve()
            try:
                absolute.relative_to(root_resolved)
            except ValueError:
                continue
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
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._transport = transport or _urllib_get
        self._max_pages = max_pages
        self._max_results = max_results
        self._timeout_s = timeout_s
        self._scan_metadata: dict[str, Any] = {}

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

    def discover(self, source: SourceRecord) -> list[SourceItem]:
        if source.kind is not SourceKind.YOUTUBE_PLAYLIST:
            raise AutomationError(
                f"YouTube playlist adapter cannot scan source kind: {source.kind.value}"
            )
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
        body = self._transport(url, self._timeout_s)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise AutomationError("YouTube API response was not valid JSON") from exc
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
        current_items = list(adapter_for_scan.discover(source))
        scan_metadata = _scan_metadata_from_adapter(adapter_for_scan)
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

        for old in previous.values():
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
        return self._set_queue_state(queue_item_id, QueueState.APPROVED, clear_error=True)

    def skip_queue_item(self, queue_item_id: str) -> QueueItem:
        return self._set_queue_state(queue_item_id, QueueState.SKIPPED, clear_error=True)

    def retry_queue_item(self, queue_item_id: str) -> QueueItem:
        return self._set_queue_state(queue_item_id, QueueState.DISCOVERED, clear_error=True)

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
            self._record_failed_queue_item(queue_item.id, YOUTUBE_METADATA_ONLY_ERROR)
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
                        return completed_result
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
                    return completed_result
            self._record_failed_queue_item(queue_item.id, str(exc))
            raise

        attach_provenance_to_bundle(
            result.bundle_dir,
            source=source,
            source_item=source_item,
            queue_item=queue_item,
            consent="explicit_queue_approval",
        )
        completed = self._set_queue_state(
            queue_item.id,
            QueueState.COMPLETED,
            bundle_id=result.manifest.bundle_id,
            clear_error=True,
        )
        self._record_library_bundle(result.bundle_dir, source, source_item, completed)
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
                return completed_result
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
                    return completed_result
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

        attach_provenance_to_bundle(
            result.bundle_dir,
            source=source,
            source_item=source_item,
            queue_item=queue_item,
            consent="explicit_cli_invocation",
        )
        completed = self._set_queue_state(
            queue_item.id,
            QueueState.COMPLETED,
            bundle_id=result.manifest.bundle_id,
            clear_error=True,
        )
        self._record_library_bundle(result.bundle_dir, source, source_item, completed)
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
        self._connection.executescript(
            """
            ALTER TABLE source_items
                ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}';
            ALTER TABLE queue_items
                ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}';
            PRAGMA user_version = 2;
            """
        )
        self._connection.commit()

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
        self._connection.commit()
        return self.get_queue_item(existing.id)

    def _record_failed_queue_item(self, queue_item_id: str, message: str) -> None:
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
    ) -> None:
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
        elif existing.queue_item_id == queue_item.id:
            self._connection.execute(
                """
                UPDATE library_bundles
                SET bundle_path = ?, source_id = ?, source_item_id = ?, queue_item_id = ?
                WHERE bundle_id = ?
                """,
                (*values, manifest.bundle_id),
            )
        else:
            raise AutomationError(
                "bundle id already exists for different source provenance; "
                "duplicate-content multi-source provenance is not implemented"
            )
        self._connection.commit()

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
                for child in resolved.rglob("*")
                if child.is_file() and child.suffix.lower() in MEDIA_EXTENSIONS
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
    try:
        playlist_id = normalize_youtube_playlist_id(playlist)
    except AutomationError as exc:
        return YouTubePreflight(
            playlist_id=playlist,
            api_key_env=api_key_env,
            credential_present=False,
            reachable=False,
            pages_checked=0,
            estimated_units_consumed=0,
            error=str(exc),
        )

    env = environ if environ is not None else os.environ
    resolved_api_key = api_key if api_key is not None else env.get(api_key_env, "")
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
        return YouTubePreflight(
            playlist_id=playlist_id,
            api_key_env=api_key_env,
            credential_present=True,
            reachable=False,
            pages_checked=0,
            estimated_units_consumed=0,
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
        "queue_state": QueueState.COMPLETED.value,
        "policy": queue_item.policy.value,
        "consent": consent,
        "remote_services": {
            "allowed": False,
            "scope": "lectern_core",
            "lectern_invoked": False,
            "requires_explicit_per_item_consent": True,
            "transcriber_network_posture": source_payload.get("transcript", {})
            .get("remote_services", {})
            .get("transcriber_network_posture", "not_recorded"),
        },
    }
    source_path.write_text(json.dumps(source_payload, indent=2) + "\n", encoding="utf-8")

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
    parsed = urllib.parse.urlparse(value)
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
    identity = playlist_item_id or video_id
    relative_path = f"{playlist_id}/{identity}"
    playlist_url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
    video_url = (
        "https://www.youtube.com/watch?"
        f"{urllib.parse.urlencode({'v': video_id, 'list': playlist_id})}"
    )
    title = _optional_string(snippet.get("title"))
    channel_id = _optional_string(snippet.get("channelId"))
    channel_title = _optional_string(snippet.get("channelTitle"))
    published_at = _optional_string(snippet.get("publishedAt"))
    video_owner_channel_id = _optional_string(content_details.get("videoOwnerChannelId"))
    video_owner_channel_title = _optional_string(content_details.get("videoOwnerChannelTitle"))
    position = _optional_int(snippet.get("position"))
    digest_payload = {
        "playlist_id": playlist_id,
        "playlist_item_id": playlist_item_id,
        "video_id": video_id,
        "title": title,
        "channel_id": channel_id,
        "channel_title": channel_title,
        "published_at": published_at,
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
        body = exc.read()
        raise _youtube_error_from_response(exc.code, body) from exc
    except urllib.error.URLError as exc:
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
        if root is not None:
            if sidecar.is_symlink():
                raise AutomationError("transcript sidecar must be inside the source root")
            try:
                sidecar.resolve().relative_to(root)
            except ValueError as exc:
                raise AutomationError("transcript sidecar must be inside the source root") from exc
        sidecar_digest, _ = _digest_and_size(sidecar)
        digest.update(b"transcript-sidecar")
        digest.update(b"\0")
        digest.update(sidecar_digest.encode("ascii"))
    else:
        digest.update(b"transcript-sidecar-absent")
    return digest.hexdigest(), media_size


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
