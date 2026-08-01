"""Local-folder discovery: what a scan of a directory tree finds, and what it skips.

Content addressing lives here too, because what a local scan considers "the same
item" is the media bytes plus any transcript sidecar beside them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lectern.bundle import MANIFEST_NAME
from lectern.records import (
    AutomationError,
    SourceItem,
    SourceRecord,
    digest_and_size,
    make_source_item_id,
    now_timestamp,
)

MEDIA_EXTENSIONS = frozenset(
    {".aac", ".avi", ".flac", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".wav", ".webm"}
)

EXCLUDED_SCAN_DIR_NAMES = frozenset({".lectern"})

EXCLUDED_SCAN_DIR_PREFIXES = (".lectern-ingest.",)


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
        if is_excluded_dir or is_excluded_temp_dir or is_bundle_output_path(root, path):
            continue
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        try:
            path.resolve().relative_to(root_resolved)
        except ValueError:
            continue
        yield path


def is_bundle_output_path(root: Path, path: Path) -> bool:
    """Report whether `path` sits inside a Lectern bundle under `root`.

    Public because it states half of what a local scan excludes, alongside the
    excluded-directory rules `iter_local_media_files` applies.

    Containment is checked first, before any bundle marker is inspected. A path
    outside `root` can still sit inside some *other* Lectern bundle, and the
    marker check would otherwise report that foreign bundle as this root's
    output — the walk only discovers it never reached `root` afterwards.
    Comparison is lexical, matching how `iter_local_media_files` builds the
    paths it passes (from `root.rglob`), so no caller's result changes.
    """

    try:
        path.relative_to(root)
    except ValueError:
        return False

    ancestor = path.parent
    while True:
        if (ancestor / MANIFEST_NAME).is_file() and (ancestor / "source.json").is_file():
            return True
        if ancestor == root:
            return False
        parent = ancestor.parent
        if parent == ancestor:
            # Reaching the filesystem root means the walk never met `root`. The
            # containment check above makes that unreachable for the documented
            # contract; it is kept because the filesystem root is its own parent,
            # so without it this loop has no termination condition at all, and a
            # future caller reaching the walk by another route would hang rather
            # than return.
            return False
        ancestor = parent


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


def approval_digest_and_media_size(path: Path, *, root: Path | None = None) -> tuple[str, int]:
    media_digest, media_size = digest_and_size(path)
    sidecar = path.with_suffix(".transcript.txt")
    digest = hashlib.sha256()
    digest.update(b"media")
    digest.update(b"\0")
    digest.update(media_digest.encode("ascii"))
    digest.update(b"\0")
    if sidecar.is_file():
        if root is not None and _transcript_sidecar_escapes_root(path, root):
            raise AutomationError("transcript sidecar must be inside the source root")
        sidecar_digest, _ = digest_and_size(sidecar)
        digest.update(b"transcript-sidecar")
        digest.update(b"\0")
        digest.update(sidecar_digest.encode("ascii"))
    else:
        digest.update(b"transcript-sidecar-absent")
    return digest.hexdigest(), media_size


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
                digest, size = approval_digest_and_media_size(path, root=root_resolved)
            except AutomationError:
                continue
            stat = path.stat()
            now = now_timestamp()
            items.append(
                SourceItem(
                    id=make_source_item_id(source.id, relative),
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
