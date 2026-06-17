"""The bundle schema: the durable contract every consumer reads (ADR-0003).

Stages communicate only through bundle artifacts described by these models.
Breaking schema changes should be deliberate and versioned.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

SCHEMA_VERSION = "0.1.0"

MANIFEST_NAME = "manifest.json"


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
        path.write_text(self.model_dump_json(indent=2) + "\n")
        return path

    @classmethod
    def load(cls, bundle_dir: Path) -> Manifest:
        return cls.model_validate_json((bundle_dir / MANIFEST_NAME).read_text())


def export_json_schema() -> str:
    """Export the manifest JSON Schema (committed under schemas/ on change)."""
    return json.dumps(Manifest.model_json_schema(), indent=2) + "\n"
