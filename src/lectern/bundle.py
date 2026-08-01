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

from pydantic import BaseModel, Field, RootModel

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


class RemoteServices(BaseModel):
    """The network posture recorded alongside a transcript.

    Modelled rather than left as free-form JSON because it is the field a
    privacy review reads first, and an unmodelled field is one no schema
    constrains.
    """

    allowed: bool
    scope: str
    lectern_invoked: bool
    requires_explicit_per_item_consent: bool
    transcriber_network_posture: str


class TranscriptPointer(BaseModel):
    method: str
    metadata: str
    segments: str
    transcript: str
    evidence_limit: str
    remote_services: RemoteServices


class ContentIdentity(BaseModel):
    """Digest and size, with no field for a location.

    The absence is the design: after LW-11 a bundle records what its media was,
    not where it sat, and a schema that still admitted a path would leave the
    door open for one to reappear.
    """

    sha256: str
    bytes: int | None = None

    # Unknown keys are REJECTED here, and the exported schema says so. Pydantic
    # ignores extras by default, which meant `{"sha256": ..., "path": "/Users/..."}`
    # validated cleanly and the committed schema admitted it -- so the privacy
    # boundary this model exists to state was advisory. Asserting that `path` is
    # not declared was never the same as asserting it is refused.
    model_config = {"extra": "forbid"}


class SourceDocument(BaseModel):
    """`source.json`."""

    source: Source
    sha256: str
    bytes: int
    transcript: TranscriptPointer
    transcript_sidecar: ContentIdentity | None = None


class TranscriptSegmentRecord(BaseModel):
    """One row of `transcript/segments.json`, the unit a citation anchors to."""

    id: int
    start_s: float
    end_s: float | None = None
    text: str
    source: str


class NormalizedAudio(BaseModel):
    path: str
    sha256: str
    bytes: int


class TranscriptBackend(BaseModel):
    kind: str
    sha256: str | None = None
    command: str | None = None


class TranscriptArtifacts(BaseModel):
    segments: str
    transcript: str
    summary: str


class SchemaContract(BaseModel):
    manifest_schema_versioned: bool
    note: str


class TranscriptMetadataDocument(BaseModel):
    """`transcript/metadata.json`."""

    schema_: str = Field(alias="schema")
    generated_at: datetime
    method: str
    backend: TranscriptBackend
    remote_services: RemoteServices
    evidence_limit: str
    source_media: ContentIdentity
    normalized_audio: NormalizedAudio
    artifacts: TranscriptArtifacts
    schema_contract: SchemaContract

    model_config = {"populate_by_name": True}


# Every artifact type Lectern writes as JSON, and the model that describes it.
# Enumerated in one place so "every written artifact type has a schema" is a
# statement a test can check rather than a claim someone has to audit by hand.
class TranscriptSegmentsDocument(RootModel[list[TranscriptSegmentRecord]]):
    """`transcript/segments.json`, which is written as an ARRAY.

    Exporting the element model gave the committed schema an object root, so
    validating a real artifact against it failed immediately. A round-trip test
    over elements cannot catch that: it exercises the model and never the
    artifact.
    """

    root: list[TranscriptSegmentRecord]


ARTIFACT_MODELS: dict[str, type[BaseModel]] = {
    "manifest": Manifest,
    "source": SourceDocument,
    "transcript-segments": TranscriptSegmentsDocument,
    "transcript-metadata": TranscriptMetadataDocument,
}


def export_artifact_schemas() -> dict[str, str]:
    """Every artifact schema, keyed by the name its file takes under `schemas/`."""

    return {
        name: json.dumps(model.model_json_schema(by_alias=True), indent=2) + "\n"
        for name, model in ARTIFACT_MODELS.items()
    }


def export_json_schema() -> str:
    """Export the manifest JSON Schema (committed under schemas/ on change)."""
    return json.dumps(Manifest.model_json_schema(), indent=2) + "\n"
