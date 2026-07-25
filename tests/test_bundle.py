"""Bundle schema seed tests: round-trip and schema export (M0 acceptance basis)."""

import os
import stat
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from lectern.bundle import (
    MANIFEST_NAME,
    SCHEMA_VERSION,
    Manifest,
    Source,
    SourceKind,
    StageName,
    StageRecord,
    StageState,
    atomic_write_text,
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


def test_manifest_save_leaves_no_temporary_files(tmp_path: Path) -> None:
    m = make_manifest()
    m.save(tmp_path)
    m.save(tmp_path)

    assert sorted(path.name for path in tmp_path.iterdir()) == [MANIFEST_NAME]


def test_interrupted_manifest_save_keeps_previous_manifest(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    m = make_manifest()
    m.save(tmp_path)
    original = (tmp_path / MANIFEST_NAME).read_bytes()

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("synthetic manifest publish failure")

    m.stages[StageName.NORMALIZE] = StageRecord(state=StageState.DONE)
    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="synthetic manifest publish failure"):
        m.save(tmp_path)

    monkeypatch.undo()

    # A crash mid-publish must not leave a committed bundle pointing at a
    # truncated manifest: the previous bytes stay in place and stay parseable.
    assert (tmp_path / MANIFEST_NAME).read_bytes() == original
    assert Manifest.load(tmp_path).stages[StageName.NORMALIZE].state is StageState.PENDING
    assert sorted(path.name for path in tmp_path.iterdir()) == [MANIFEST_NAME]


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


def test_atomic_write_temporary_is_restricted_before_content(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    target = tmp_path / "manifest.json"
    target.write_text("{}\n", encoding="utf-8")
    os.chmod(target, 0o600)
    previous_umask = os.umask(0o022)
    observed: list[int] = []
    real_fdopen = os.fdopen

    def spy_fdopen(fd: int, *args: object, **kwargs: object) -> object:
        # Sample the temp file's mode before any content bytes are written.
        observed.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_fdopen(fd, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "fdopen", spy_fdopen)
    try:
        atomic_write_text(target, '{"a": 1}\n')
    finally:
        os.umask(previous_umask)

    assert observed == [0o600]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text(encoding="utf-8") == '{"a": 1}\n'
