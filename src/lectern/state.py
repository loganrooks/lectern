"""The SQLite state store: sources, discovery queue, and library index.

This module owns persistence and the queue FSM. It deliberately does not own the
ingest pipeline: `lectern.automation.AutomationState` composes that on top, so
the layer that writes bundles is separable from the layer that records them.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Self, cast

from lectern.bundle import Manifest
from lectern.records import (
    LEGAL_QUEUE_TRANSITION_SOURCE_VALUES,
    LEGAL_QUEUE_TRANSITION_SOURCES,
    TERMINAL_QUEUE_STATES,
    AutomationError,
    LibraryBundle,
    LibraryRecordOutcome,
    QueueItem,
    QueueState,
    ScanDelta,
    ScanMetadataProvider,
    SourceAdapter,
    SourceItem,
    SourceKind,
    SourcePolicy,
    SourceRecord,
    make_queue_item_id,
    make_source_id,
    make_source_item_id,
    metadata_to_json,
    now_timestamp,
)
from lectern.sources import default_source_adapter
from lectern.sources.local import approval_digest_and_media_size
from lectern.sources.youtube import normalize_youtube_playlist_id


class AutomationStateStore:
    """SQLite-backed local automation state store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._migrate()
        except BaseException:
            # A store that never finished initializing is never returned, so no
            # caller holds it to close. Releasing the handle here is the only
            # opportunity; `BaseException` because an interrupt between connect
            # and migrate leaks exactly as a migration error does.
            self._connection.close()
            raise

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
        source_id = make_source_id(SourceKind.LOCAL_FOLDER.value, str(root))
        now = now_timestamp()
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
        source_id = make_source_id(SourceKind.YOUTUBE_PLAYLIST.value, playlist_id)
        now = now_timestamp()
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

        adapter_for_scan = adapter or default_source_adapter(source)
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
                    updated_at=now_timestamp(),
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
        self._reject_illegal_transition(queue_item_id, "approve")
        return self._set_queue_state(
            queue_item_id, QueueState.APPROVED, clear_error=True, guarded_action="approve"
        )

    def skip_queue_item(self, queue_item_id: str) -> QueueItem:
        self._reject_illegal_transition(queue_item_id, "skip")
        return self._set_queue_state(
            queue_item_id, QueueState.SKIPPED, clear_error=True, guarded_action="skip"
        )

    def retry_queue_item(self, queue_item_id: str) -> QueueItem:
        self._reject_illegal_transition(queue_item_id, "retry")
        return self._set_queue_state(
            queue_item_id, QueueState.DISCOVERED, clear_error=True, guarded_action="retry"
        )

    def _reject_illegal_transition(self, queue_item_id: str, action: str) -> None:
        """Refuse a verb the FSM table does not allow out of the item's current state.

        Terminal states keep their own wording; other illegal sources name the verb
        and the states it is legal from. The item is read exactly once, so a caller
        racing a concurrent writer decides against a single observed row.
        """

        queue_item = self.get_queue_item(queue_item_id)
        if queue_item.state in TERMINAL_QUEUE_STATES:
            raise AutomationError(
                f"queue item {queue_item.id} is in terminal state "
                f"{queue_item.state.value}; {action} is not a legal transition"
            )
        if queue_item.state not in LEGAL_QUEUE_TRANSITION_SOURCES[action]:
            raise AutomationError(
                f"queue item {queue_item.id} is in state {queue_item.state.value}; "
                f"{action} is only legal from state "
                f"{_join_states(LEGAL_QUEUE_TRANSITION_SOURCE_VALUES[action])}"
            )

    def _reject_guarded_transition_conflict(self, queue_item_id: str, action: str) -> NoReturn:
        """Explain a guarded update that matched no row, from the row as it stands now.

        The pre-check read and the update are separate statements, so another writer
        can move the item into a state the verb is illegal from in between; the
        update's own FSM guard is what refuses that, and this re-read turns the empty
        result into the same error the pre-check would have raised.
        """

        # The zero-row UPDATE still opened an implicit write transaction; release
        # it before raising so a caller that catches the expected rejection does
        # not leave the connection holding a write lock.
        self._connection.rollback()
        self._reject_illegal_transition(queue_item_id, action)
        queue_item = self.get_queue_item(queue_item_id)
        raise AutomationError(f"queue item {queue_item.id} could not be transitioned by {action}")

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
            try:
                self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{{}}'"
                )
            except sqlite3.OperationalError as exc:
                # A concurrent migration can add the column between our check
                # and this statement; that outcome is the migration's goal, so
                # tolerate it rather than failing the second process.
                if "duplicate column name" not in str(exc):
                    raise
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
        # above would later trust as a complete pre-migration copy. The name carries
        # a per-attempt unique component (as atomic_write_text does): a fixed name is
        # shared by concurrent migrations of the same store, where one attempt's
        # cleanup would unlink the other's in-progress copy.
        temporary = self.path.with_name(f".{backup.name}.{uuid.uuid4().hex}.tmp")
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
            # Publish only if no backup exists yet: os.link fails on an
            # existing destination, unlike os.replace, so a backup that a
            # concurrent migration published after our existence check is
            # never overwritten with a possibly post-migration snapshot.
            with contextlib.suppress(FileExistsError):
                os.link(temporary, backup)
            temporary.unlink(missing_ok=True)
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
                metadata_to_json(item.metadata),
            ),
        )

    def _enqueue_if_review(self, source: SourceRecord, item: SourceItem) -> QueueItem | None:
        if source.policy is not SourcePolicy.REVIEW:
            return None
        if _is_placeholder_item(item):
            return None
        queue_item_id = make_queue_item_id(item.id, item.sha256)
        now = now_timestamp()
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
                metadata_to_json(item.metadata),
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
        guarded_action: str | None = None,
    ) -> QueueItem:
        queue_item = self._apply_queue_state(
            queue_item_id,
            state,
            bundle_id=bundle_id,
            clear_error=clear_error,
            guarded_action=guarded_action,
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
        guarded_action: str | None = None,
    ) -> QueueItem:
        """Stage a queue-state transition without committing; the caller commits.

        `guarded_action` names a user verb whose legal source states the FSM table
        pins. The guard lives in the UPDATE's WHERE clause so the check and the write
        are one statement: a read-then-write pair lets a concurrent writer slip an
        illegal source state in between and have it overwritten.
        """

        existing = self.get_queue_item(queue_item_id)
        fsm_guard = ""
        parameters: list[object] = [
            state.value,
            bundle_id,
            1 if clear_error else 0,
            now_timestamp(),
            existing.id,
        ]
        if guarded_action is not None:
            legal_sources = LEGAL_QUEUE_TRANSITION_SOURCE_VALUES[guarded_action]
            placeholders = ", ".join("?" for _ in legal_sources)
            fsm_guard = f" AND state IN ({placeholders})"
            parameters.extend(legal_sources)
        cursor = self._connection.execute(
            f"""
            UPDATE queue_items
            SET state = ?, bundle_id = COALESCE(?, bundle_id),
                last_error = CASE WHEN ? THEN NULL ELSE last_error END,
                updated_at = ?
            WHERE id = ?{fsm_guard}
            """,
            parameters,
        )
        if guarded_action is not None and cursor.rowcount == 0:
            self._reject_guarded_transition_conflict(queue_item_id, guarded_action)
        return existing

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
                now_timestamp(),
                queue_item.id,
            ),
        )
        self._connection.commit()

    def _record_unsupported_queue_item(self, queue_item_id: str, message: str) -> None:
        """Record a terminal unsupported-by-design outcome without spending an attempt.

        The write is conditioned on the row still being APPROVED: a concurrent
        skip or retry that committed after our approved read must not be
        overwritten with an irreversible terminal state. On a zero-row match the
        newer operator transition stands and no terminal state is recorded.
        """

        queue_item = self.get_queue_item(queue_item_id)
        cursor = self._connection.execute(
            """
            UPDATE queue_items
            SET state = ?, last_error = ?, updated_at = ?
            WHERE id = ? AND state = ?
            """,
            (
                QueueState.UNSUPPORTED.value,
                message,
                now_timestamp(),
                queue_item.id,
                QueueState.APPROVED.value,
            ),
        )
        if cursor.rowcount == 0:
            self._connection.rollback()
            return
        self._connection.commit()

    def _record_library_bundle(
        self,
        bundle_dir: Path,
        source: SourceRecord,
        source_item: SourceItem,
        queue_item: QueueItem,
    ) -> LibraryRecordOutcome:
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
                    now_timestamp(),
                ),
            )
            return LibraryRecordOutcome(inserted=True, previous=None)
        if existing.queue_item_id == queue_item.id:
            self._connection.execute(
                """
                UPDATE library_bundles
                SET bundle_path = ?, source_id = ?, source_item_id = ?, queue_item_id = ?
                WHERE bundle_id = ?
                """,
                (*values, manifest.bundle_id),
            )
            return LibraryRecordOutcome(inserted=False, previous=existing)
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

    def _ensure_one_shot_source(self, source_path: Path) -> SourceRecord:
        source = source_path.resolve()
        source_id = make_source_id(SourceKind.ONE_SHOT.value, str(source))
        now = now_timestamp()
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
        digest, size = approval_digest_and_media_size(path)
        stat = path.stat()
        now = now_timestamp()
        item = SourceItem(
            id=make_source_item_id(source.id, path.name),
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
        queue_item_id = make_queue_item_id(item.id, item.sha256)
        existing = self._connection.execute(
            "SELECT * FROM queue_items WHERE id = ?",
            (queue_item_id,),
        ).fetchone()
        if existing is not None:
            queue_item = _queue_from_row(existing)
            if queue_item.state is QueueState.COMPLETED:
                return queue_item
            return self._set_queue_state(queue_item.id, QueueState.APPROVED, clear_error=True)

        now = now_timestamp()
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
                metadata_to_json(item.metadata),
            ),
        )
        self._connection.commit()
        return self.get_queue_item(queue_item_id)


STATE_SCHEMA_VERSION = 2

DEFAULT_STATE_PATH = Path(".lectern") / "state.sqlite"

UPGRADABLE_STATE_SCHEMA_VERSIONS = frozenset({0, 1, STATE_SCHEMA_VERSION})


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


def preflight_state_store(path: Path = DEFAULT_STATE_PATH) -> StateStorePreflight:
    resolved = path.resolve()
    writable_location = _nearest_existing_parent_is_writable(resolved)
    exists = resolved.exists()
    schema_version: int | None = None
    error: str | None = None
    if exists:
        writable_location = writable_location and os.access(resolved, os.W_OK)
        try:
            # A `sqlite3.Connection` context manager commits or rolls back; it
            # does not close. Preflight is a read-only probe, so the handle has
            # to be released explicitly rather than left to refcounting.
            connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
            try:
                schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            finally:
                connection.close()
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


def _join_states(values: Sequence[str]) -> str:
    """Render a legal-source set for an error message: `failed`, `a or b`, `a, b, or c`."""

    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} or {values[1]}"
    return f"{', '.join(values[:-1])}, or {values[-1]}"


def _nearest_existing_parent_is_writable(path: Path) -> bool:
    candidate = path.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK)
