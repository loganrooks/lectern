"""The automation spine's shared vocabulary: errors, records, and identities.

Everything here is inert: enums, frozen records, the queue's legal-transition
table, the discovery protocols, and the pure functions that mint the identities
and digests those records carry. It imports nothing else from `lectern`, which
is what lets the state store, the source adapters, and the provenance writer all
depend on it without depending on each other.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable


def _empty_metadata() -> dict[str, Any]:
    return {}


class AutomationError(RuntimeError):
    """Raised when the local automation spine cannot complete a requested action."""


class SourceKind(StrEnum):
    LOCAL_FOLDER = "local-folder"
    ONE_SHOT = "one-shot"
    YOUTUBE_PLAYLIST = "youtube-playlist"


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

# Stable ordering so the guarded-UPDATE placeholders bind the same values every run.
TERMINAL_QUEUE_STATE_VALUES = tuple(sorted(state.value for state in TERMINAL_QUEUE_STATES))

# The queue FSM, as an explicit table: for each user verb, the states an item may
# legally be transitioned *from*. Every guarded verb checks this table, so the
# legality of a transition can be read here rather than inferred from call sites.
#
# - `approve` and `skip` stay legal from every non-terminal state; re-approving a
#   COMPLETED item is what the rerun/replay/restore path depends on.
# - `retry` is legal from FAILED only. Retry exists to re-arm a failed attempt;
#   from COMPLETED it would silently discard a finished bundle's queue linkage,
#   and from DISCOVERED/APPROVED/SKIPPED it is a no-op dressed as a state change.
# - UNSUPPORTED appears in no set, so terminal items refuse every verb.
LEGAL_QUEUE_TRANSITION_SOURCES: Mapping[str, frozenset[QueueState]] = MappingProxyType(
    {
        "approve": frozenset(QueueState) - TERMINAL_QUEUE_STATES,
        "skip": frozenset(QueueState) - TERMINAL_QUEUE_STATES,
        "retry": frozenset({QueueState.FAILED}),
    }
)

# Stable ordering, as above, so each verb's guarded-UPDATE placeholders bind the
# same values every run.
LEGAL_QUEUE_TRANSITION_SOURCE_VALUES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        action: tuple(sorted(state.value for state in states))
        for action, states in LEGAL_QUEUE_TRANSITION_SOURCES.items()
    }
)


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
class LibraryRecordOutcome:
    """What a staged library record changed, so the undo path can reverse it.

    A rerun into a different output root can produce the same deterministic bundle
    id, in which case the row is updated rather than inserted; reversing that needs
    the prior row, not just the knowledge that nothing was inserted.
    """

    inserted: bool
    previous: LibraryBundle | None


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


def now_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def digest_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def metadata_to_json(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def make_source_id(kind: str, root_path: str) -> str:
    return _stable_id("src", [kind, root_path])


def make_source_item_id(source_id: str, relative_path: str) -> str:
    return _stable_id("item", [source_id, relative_path])


def make_queue_item_id(source_item_id: str, content_sha256: str) -> str:
    return _stable_id("queue", [source_item_id, content_sha256])


def _stable_id(prefix: str, parts: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return f"{prefix}_{digest.hexdigest()[:16]}"
