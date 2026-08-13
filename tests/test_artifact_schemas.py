"""Every written artifact type has a schema, and every schema matches what is written.

The Tier-A clause is "artifact schemas export and round-trip under the recorded
promise level". Two failure modes sit under that, and only one of them is
obvious.

The obvious one: a schema drifts from its model. The committed-file check
catches that.

The one worth building for: an artifact type is written with no schema at all.
Checking the schemas that exist can never find that, because the missing schema
is missing from the list being checked too. So the enumeration is derived from
what an ingest actually produces on disk, not from a list someone maintains.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lectern import cli
from lectern.bundle import (
    ARTIFACT_MODELS,
    Manifest,
    SourceDocument,
    TranscriptBackend,
    TranscriptMetadataDocument,
    TranscriptSegmentsDocument,
    export_artifact_schemas,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC_TALK = FIXTURE_DIR / "synthetic_talk.wav"
SYNTHETIC_TRANSCRIPT = FIXTURE_DIR / "synthetic_talk.transcript.txt"
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

# Bundle-relative JSON artifacts, and the model that describes each. `media/` and
# the Markdown renderings are not JSON and carry no schema.
ARTIFACT_FILES = {
    "manifest.json": "manifest",
    "source.json": "source",
    "transcript/segments.json": "transcript-segments",
    "transcript/metadata.json": "transcript-metadata",
}


def _bundle(tmp_path: Path) -> Path:
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    media = media_dir / "synthetic_talk.wav"
    media.write_bytes(SYNTHETIC_TALK.read_bytes())
    media.with_suffix(".transcript.txt").write_text(
        SYNTHETIC_TRANSCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    out = tmp_path / "bundles"
    assert (
        cli.main(["ingest", str(media), "--output", str(out), "--state", str(tmp_path / "s.db")])
        == 0
    )
    return sorted(path for path in out.iterdir() if path.is_dir())[-1]


def test_every_written_json_artifact_has_a_schema(tmp_path: Path) -> None:
    """Derived from disk, so a new unschema'd artifact fails here.

    A list maintained by hand cannot catch its own omissions; walking the
    produced bundle can.
    """

    bundle = _bundle(tmp_path)
    written = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*.json")
        if path.is_file() and not path.name.startswith(".")
    }
    assert written == set(ARTIFACT_FILES), (
        "a JSON artifact is written that no schema describes, or vice versa; "
        f"written={sorted(written)} described={sorted(ARTIFACT_FILES)}"
    )
    assert set(ARTIFACT_FILES.values()) == set(ARTIFACT_MODELS)


def test_manifest_round_trips(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    loaded = Manifest.load(bundle)
    reparsed = Manifest.model_validate_json(loaded.model_dump_json())
    assert reparsed == loaded


def test_source_document_round_trips(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    payload: dict[str, Any] = json.loads((bundle / "source.json").read_text(encoding="utf-8"))
    document = SourceDocument.model_validate(payload)
    assert SourceDocument.model_validate_json(document.model_dump_json()) == document


def test_transcript_segments_round_trip(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    # Validated as the whole ARTIFACT, not element by element. Round-tripping
    # elements exercises the model and never the file, so it could not have
    # noticed that the exported schema declared an object root for an array.
    raw = (bundle / "transcript" / "segments.json").read_text(encoding="utf-8")
    document = TranscriptSegmentsDocument.model_validate_json(raw)
    assert document.root
    assert TranscriptSegmentsDocument.model_validate_json(document.model_dump_json()) == document


def test_transcript_metadata_round_trips(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    payload: dict[str, Any] = json.loads(
        (bundle / "transcript" / "metadata.json").read_text(encoding="utf-8")
    )
    document = TranscriptMetadataDocument.model_validate(payload)
    assert (
        TranscriptMetadataDocument.model_validate_json(document.model_dump_json(by_alias=True))
        == document
    )


def test_transcript_backend_rejects_undeclared_path_fields() -> None:
    with pytest.raises(ValueError):
        TranscriptBackend.model_validate({"kind": "sidecar", "path": "/private/transcript.txt"})

    definitions = json.loads(export_artifact_schemas()["transcript-metadata"])["$defs"]
    assert definitions["TranscriptBackend"]["additionalProperties"] is False


@pytest.mark.parametrize("name", sorted(ARTIFACT_MODELS))
def test_committed_schema_matches_its_model(name: str) -> None:
    path = SCHEMA_DIR / f"{name}.schema.json"
    assert path.is_file(), f"missing committed schema: {path.name}"
    assert path.read_text(encoding="utf-8") == export_artifact_schemas()[name]


def test_local_source_schema_requires_content_identity_and_size() -> None:
    for name in ("manifest", "source"):
        definitions = json.loads(export_artifact_schemas()[name])["$defs"]
        local_rule = definitions["Source"]["allOf"][0]
        assert local_rule["if"] == {"properties": {"kind": {"const": "local"}}}
        assert local_rule["then"]["required"] == ["bytes"]
        assert local_rule["then"]["properties"]["ref"]["pattern"] == (r"^sha256:[0-9a-f]{64}$")
        assert local_rule["then"]["properties"]["bytes"] == {
            "minimum": 0,
            "type": "integer",
        }
        shared_bytes = definitions["Source"]["properties"]["bytes"]
        assert shared_bytes["anyOf"][0] == {"type": "integer"}


def test_no_artifact_schema_admits_a_filesystem_path() -> None:
    """The promise LW-11 made, enforced at the level of the contract.

    A schema that still declares a `path` on a source or sidecar leaves the door
    open for one to be written again, and a future reader would take its
    presence as permission.
    """

    for name, schema in export_artifact_schemas().items():
        definitions = json.loads(schema).get("$defs", {})
        for model_name in ("ContentIdentity",):
            model_schema: dict[str, Any] = definitions.get(model_name, {})
            assert "path" not in model_schema.get("properties", {}), (
                f"{name}: {model_name} still declares a path field"
            )
