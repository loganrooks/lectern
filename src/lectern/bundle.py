"""The bundle schema: the durable contract every consumer reads (ADR-0003).

Stages communicate only through bundle artifacts described by these models.
Breaking schema changes should be deliberate and versioned.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

SCHEMA_VERSION = "0.1.0"

MANIFEST_NAME = "manifest.json"


def atomic_write_text(path: Path, text: str) -> Path:
    """Publish ``text`` at ``path`` via a same-directory temporary file.

    A plain write truncates the destination before the new bytes land, so a
    crash mid-write can leave a committed bundle reference pointing at a
    corrupt artifact. Writing to a unique temporary name and publishing with
    ``os.replace`` makes the swap atomic: readers see either the previous file
    or the new one, never a partial one. The temporary is removed if anything
    fails before the replace.
    """

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    # Preserve the destination's permissions across the replace; a fresh file
    # keeps the umask-derived mode a plain write would have produced.
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


class SourceKind(StrEnum):
    YOUTUBE = "youtube"
    LOCAL = "local"
    URL = "url"


class StageName(StrEnum):
    ACQUIRE = "acquire"
    NORMALIZE = "normalize"
    TRANSCRIBE = "transcribe"
    DIARIZE = "diarize"
    VISUAL = "visual"
    ENRICH = "enrich"
    SITUATE = "situate"
    SYNTHESIZE = "synthesize"


class StageState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class Source(BaseModel):
    """Provenance. `local` sources trigger the privacy hard rule (ADR-0002)."""

    kind: SourceKind
    ref: str  # URL or original file path
    title: str | None = None
    channel: str | None = None
    published: datetime | None = None
    duration_s: float | None = None


class ArtifactRef(BaseModel):
    """A produced file, content-addressed for idempotence checks."""

    path: str  # relative to bundle root
    sha256: str
    bytes: int


def empty_artifact_refs() -> list[ArtifactRef]:
    return []


class StageRecord(BaseModel):
    state: StageState = StageState.PENDING
    started: datetime | None = None
    finished: datetime | None = None
    outputs: list[ArtifactRef] = Field(default_factory=empty_artifact_refs)
    error: str | None = None


class Manifest(BaseModel):
    schema_version: str = SCHEMA_VERSION
    bundle_id: str
    created: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: Source
    stages: dict[StageName, StageRecord] = Field(
        default_factory=lambda: {name: StageRecord() for name in StageName}
    )

    def save(self, bundle_dir: Path) -> Path:
        path = bundle_dir / MANIFEST_NAME
        return atomic_write_text(path, self.model_dump_json(indent=2) + "\n")

    @classmethod
    def load(cls, bundle_dir: Path) -> Manifest:
        return cls.model_validate_json((bundle_dir / MANIFEST_NAME).read_text())


def export_json_schema() -> str:
    """Export the manifest JSON Schema (committed under schemas/ on change)."""
    return json.dumps(Manifest.model_json_schema(), indent=2) + "\n"
