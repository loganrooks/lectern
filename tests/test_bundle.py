"""Bundle schema seed tests: round-trip and schema export (M0 acceptance basis)."""

from pathlib import Path

from lectern.bundle import (
    SCHEMA_VERSION,
    Manifest,
    Source,
    SourceKind,
    StageName,
    StageRecord,
    StageState,
    export_json_schema,
)


def make_manifest() -> Manifest:
    return Manifest(
        bundle_id="test-0001",
        source=Source(kind=SourceKind.LOCAL, ref="/tmp/talk.wav", title="Fixture Talk"),
    )


def test_manifest_round_trip(tmp_path: Path) -> None:
    m = make_manifest()
    m.stages[StageName.NORMALIZE] = StageRecord(state=StageState.DONE)
    m.save(tmp_path)
    loaded = Manifest.load(tmp_path)
    assert loaded == m
    assert loaded.schema_version == SCHEMA_VERSION


def test_all_stages_present_by_default() -> None:
    m = make_manifest()
    assert set(m.stages) == set(StageName)
    assert all(r.state is StageState.PENDING for r in m.stages.values())


def test_json_schema_exports() -> None:
    schema = export_json_schema()
    assert '"Manifest"' in schema or '"title": "Manifest"' in schema


def test_committed_json_schema_matches_model() -> None:
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "manifest.schema.json"
    assert schema_path.read_text() == export_json_schema()
