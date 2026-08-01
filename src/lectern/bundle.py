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


def schema_version_is_compatible(version: str) -> bool:
    """Whether this build can read a manifest declaring `version`.

    Compatibility is keyed on the leading component only, matching SUPPORT.md:
    additive changes raise a later component and stay readable, while a breaking
    change raises the leading one and must not be read under the old meaning.

    The M5a content-identity change to `Source.ref` is breaking — the field
    keeps its type while changing what it denotes, so nothing about the shape
    warns an older reader. The increment itself is an open owner decision; this
    predicate is written so that settling it is a one-line change to
    `SCHEMA_VERSION` and nothing else.
    """

    return version.split(".", 1)[0] == SCHEMA_VERSION.split(".", 1)[0]


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
    # Preserve the destination's permissions across the replace, and restrict
    # the temporary BEFORE any content bytes land in it: a restricted artifact
    # must never transit through a umask-mode file another local user could
    # read, and a killed process must not leave a broad-mode copy behind.
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    descriptor = os.open(
        temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode if mode is not None else 0o666
    )
    try:
        if mode is not None:
            os.chmod(temporary, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
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
    # A *reference* to the source, never a location on the machine that made
    # the bundle. For remote kinds that is the URL, which is the identity. For
    # `local` it is `sha256:<digest>`: a bundle outlives the filesystem it was
    # built on, so a path in it both discloses the account name and dangles as
    # soon as the bundle is copied (LW-11, ratified 2026-08-01).
    ref: str
    # Size travels with the digest because identity alone cannot tell a caller
    # whether this was a short clip or a long lecture.
    bytes: int | None = None
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
        """Load a manifest, refusing one this code cannot claim to understand.

        Without this check the version field is decoration: a newer manifest
        validates against the current model, missing fields take their defaults,
        and repurposed fields are read under their old meaning. That silence is
        what makes a same-shaped change dangerous — the consumer gets no error,
        just a wrong answer. Refusing is the only way the compatibility promise
        in SUPPORT.md means anything at the point of use.
        """

        manifest = cls.model_validate_json((bundle_dir / MANIFEST_NAME).read_text())
        if not schema_version_is_compatible(manifest.schema_version):
            raise ValueError(
                f"unsupported bundle schema version {manifest.schema_version!r}; "
                f"this build reads schema version {SCHEMA_VERSION!r}"
            )
        return manifest


def export_json_schema() -> str:
    """Export the manifest JSON Schema (committed under schemas/ on change)."""
    return json.dumps(Manifest.model_json_schema(), indent=2) + "\n"
