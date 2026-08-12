from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, cast

import pytest

from lectern import migrations
from lectern.bundle import MANIFEST_NAME, Manifest
from lectern.ingest import ingest_local
from lectern.migrations import (
    BACKUP_SUFFIX,
    MARKER_NAME,
    STAGING_SUFFIX,
    MigrationError,
    prepare_bundle_migration,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC_TALK = FIXTURE_DIR / "synthetic_talk.wav"
SYNTHETIC_TRANSCRIPT = FIXTURE_DIR / "synthetic_talk.transcript.txt"


def _digest(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _rewrite_manifest_hashes(bundle: Path, manifest: dict[str, Any]) -> None:
    for stage in manifest["stages"].values():
        for output in stage["outputs"]:
            digest, size = _digest(bundle / output["path"])
            output["sha256"] = digest
            output["bytes"] = size


def _tree_bytes(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _tree_state(root: Path) -> dict[Path, tuple[str, bytes | str | None]]:
    state: dict[Path, tuple[str, bytes | str | None]] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink():
            state[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            state[relative] = ("directory", None)
        else:
            state[relative] = ("file", path.read_bytes())
    return state


def _write_legacy_shape(bundle: Path, media: Path) -> None:
    manifest_path = bundle / MANIFEST_NAME
    source_path = bundle / "source.json"
    metadata_path = bundle / "transcript" / "metadata.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = json.loads(source_path.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "0.1.0"
    manifest["source"]["ref"] = str(media)
    manifest["source"].pop("bytes", None)
    source["source"]["ref"] = str(media)
    source["source"].pop("bytes", None)
    sidecar = source["transcript_sidecar"]
    assert isinstance(sidecar, dict)
    sidecar["path"] = str(media.with_suffix(".transcript.txt"))
    metadata["source_media"]["path"] = str(media)
    metadata["backend"]["path"] = str(media.with_suffix(".transcript.txt"))
    source_path.write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    _rewrite_manifest_hashes(bundle, manifest)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def legacy_bundle(tmp_path: Path) -> Path:
    media = tmp_path / "Private Recordings" / "synthetic_talk.wav"
    media.parent.mkdir(parents=True)
    media.write_bytes(SYNTHETIC_TALK.read_bytes())
    media.with_suffix(".transcript.txt").write_text(
        SYNTHETIC_TRANSCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    bundle = ingest_local(media, tmp_path / "bundles").bundle_dir
    _write_legacy_shape(bundle, media)
    return bundle


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _repair_manifest_after_artifact_change(bundle: Path) -> None:
    manifest_path = bundle / MANIFEST_NAME
    manifest = _read_json(manifest_path)
    _rewrite_manifest_hashes(bundle, manifest)
    _write_json(manifest_path, manifest)


def _staging_role(bundle: Path) -> Path:
    return bundle.with_name(bundle.name + STAGING_SUFFIX)


def _nested_json_object(depth: int) -> dict[str, Any]:
    value: object = "leaf"
    for _ in range(depth):
        value = {"nested": value}
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _read_json_array(path: Path) -> list[dict[str, Any]]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    items = cast(list[object], value)
    assert all(isinstance(item, dict) for item in items)
    return cast(list[dict[str, Any]], value)


def _assert_preflight_refusal(bundle: Path, message: str) -> None:
    before = _tree_state(bundle)
    with pytest.raises(MigrationError, match=message) as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    assert _tree_state(bundle) == before
    assert not os.path.lexists(_staging_role(bundle))


def test_prepare_builds_valid_target_without_mutating_source(tmp_path: Path) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_bytes(bundle)
    prepared = prepare_bundle_migration(bundle)
    assert prepared.source_version == "0.1.0"
    assert prepared.target_version == "1.0.0"
    assert Manifest.load(prepared.staging_dir).schema_version == "1.0.0"
    assert _tree_bytes(bundle) == before


def test_prepare_rejects_hash_mismatch_before_staging(tmp_path: Path) -> None:
    bundle = legacy_bundle(tmp_path)
    (bundle / "source.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="declared artifact integrity"):
        prepare_bundle_migration(bundle)


def test_source_path_normalizes_lexical_parent_without_following_links(
    tmp_path: Path,
) -> None:
    bundle = legacy_bundle(tmp_path)
    (bundle / "child").mkdir()
    before = _tree_state(bundle)
    normalized = migrations._source_path(  # pyright: ignore[reportPrivateUsage]
        bundle / "child" / ".."
    )
    assert normalized == bundle
    assert normalized.with_name(normalized.name + STAGING_SUFFIX).parent == bundle.parent
    assert normalized.with_name(normalized.name + BACKUP_SUFFIX).parent == bundle.parent
    assert _tree_state(bundle) == before
    assert not os.path.lexists(_staging_role(bundle))


def test_prepare_rejects_root_symlink_without_following_it(tmp_path: Path) -> None:
    bundle = legacy_bundle(tmp_path)
    alias = tmp_path / "bundle-alias"
    alias.symlink_to(bundle, target_is_directory=True)
    before = _tree_state(bundle)
    with pytest.raises(
        MigrationError, match="source role must be a real bundle directory"
    ) as error:
        prepare_bundle_migration(alias)
    assert str(alias) not in str(error.value)
    assert _tree_state(bundle) == before
    assert alias.is_symlink()
    assert not os.path.lexists(_staging_role(alias))


def test_prepare_rejects_nested_symlink_before_staging(tmp_path: Path) -> None:
    bundle = legacy_bundle(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (bundle / "nested-link").symlink_to(outside)
    _assert_preflight_refusal(bundle, "source role contains a symlink")


@pytest.mark.parametrize("suffix", [BACKUP_SUFFIX, STAGING_SUFFIX])
@pytest.mark.parametrize("dangling", [False, True])
def test_prepare_preserves_occupied_sibling_roles(
    tmp_path: Path, suffix: str, dangling: bool
) -> None:
    bundle = legacy_bundle(tmp_path)
    collision = bundle.with_name(bundle.name + suffix)
    if dangling:
        collision.symlink_to(tmp_path / "missing-role-target")
    else:
        collision.mkdir()
        (collision / "owner.txt").write_text("unrelated\n", encoding="utf-8")
    bundle_before = _tree_state(bundle)
    collision_before = (
        ("symlink", os.readlink(collision))
        if collision.is_symlink()
        else ("directory", _tree_state(collision))
    )
    role = "backup" if suffix == BACKUP_SUFFIX else "staging"
    with pytest.raises(MigrationError, match=f"{role} role is already occupied") as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    assert _tree_state(bundle) == bundle_before
    collision_after = (
        ("symlink", os.readlink(collision))
        if collision.is_symlink()
        else ("directory", _tree_state(collision))
    )
    assert collision_after == collision_before


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"{", "cannot read valid source manifest"),
        (b"[]\n", "source manifest must contain a JSON object"),
        (b"\xff", "cannot read valid source manifest"),
    ],
)
def test_prepare_rejects_unreadable_manifest_before_staging(
    tmp_path: Path, raw: bytes, message: str
) -> None:
    bundle = legacy_bundle(tmp_path)
    (bundle / MANIFEST_NAME).write_bytes(raw)
    _assert_preflight_refusal(bundle, message)


@pytest.mark.parametrize("version", [None, 1, "0.2.0", "2.0.0"])
def test_prepare_checks_version_before_other_manifest_fields(
    tmp_path: Path, version: object
) -> None:
    bundle = legacy_bundle(tmp_path)
    manifest = _read_json(bundle / MANIFEST_NAME)
    if version is None:
        manifest.pop("schema_version")
    else:
        manifest["schema_version"] = version
    manifest["bundle_id"] = None
    manifest["source"] = None
    manifest["stages"] = None
    _write_json(bundle / MANIFEST_NAME, manifest)
    _assert_preflight_refusal(bundle, "source manifest does not declare schema 0.1.0")


@pytest.mark.parametrize("bundle_id", [None, "", 3, True])
def test_prepare_rejects_malformed_bundle_id_before_staging(
    tmp_path: Path, bundle_id: object
) -> None:
    bundle = legacy_bundle(tmp_path)
    manifest = _read_json(bundle / MANIFEST_NAME)
    manifest["bundle_id"] = bundle_id
    _write_json(bundle / MANIFEST_NAME, manifest)
    _assert_preflight_refusal(bundle, "manifest bundle ID is malformed")


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (None, "manifest source is malformed"),
        ({}, "manifest source kind is malformed"),
        ({"kind": "unknown", "ref": "identity"}, "manifest source kind is malformed"),
        ({"kind": "local"}, "manifest source reference is malformed"),
        ({"kind": "local", "ref": ""}, "manifest source reference is malformed"),
        ({"kind": "local", "ref": 4}, "manifest source reference is malformed"),
    ],
)
def test_prepare_rejects_malformed_manifest_source_before_staging(
    tmp_path: Path, source: object, message: str
) -> None:
    bundle = legacy_bundle(tmp_path)
    manifest = _read_json(bundle / MANIFEST_NAME)
    manifest["source"] = source
    _write_json(bundle / MANIFEST_NAME, manifest)
    _assert_preflight_refusal(bundle, message)


@pytest.mark.parametrize("stages", [None, [], "stages"])
def test_prepare_rejects_malformed_stages_before_staging(tmp_path: Path, stages: object) -> None:
    bundle = legacy_bundle(tmp_path)
    manifest = _read_json(bundle / MANIFEST_NAME)
    manifest["stages"] = stages
    _write_json(bundle / MANIFEST_NAME, manifest)
    _assert_preflight_refusal(bundle, "manifest stages are malformed")


@pytest.mark.parametrize(
    ("stage", "message"),
    [
        (None, "manifest stage outputs are malformed"),
        ({}, "manifest stage outputs are malformed"),
        ({"outputs": {}}, "manifest stage outputs are malformed"),
        ({"outputs": [None]}, "manifest artifact record is malformed"),
    ],
)
def test_prepare_rejects_malformed_stage_output_structure(
    tmp_path: Path, stage: object, message: str
) -> None:
    bundle = legacy_bundle(tmp_path)
    manifest = _read_json(bundle / MANIFEST_NAME)
    first_name = next(iter(manifest["stages"]))
    manifest["stages"][first_name] = stage
    _write_json(bundle / MANIFEST_NAME, manifest)
    _assert_preflight_refusal(bundle, message)


@pytest.mark.parametrize("digest", [None, "bad", "A" * 64, 4])
def test_prepare_rejects_malformed_artifact_digest_before_staging(
    tmp_path: Path, digest: object
) -> None:
    bundle = legacy_bundle(tmp_path)
    manifest = _read_json(bundle / MANIFEST_NAME)
    output = next(output for stage in manifest["stages"].values() for output in stage["outputs"])
    output["sha256"] = digest
    _write_json(bundle / MANIFEST_NAME, manifest)
    _assert_preflight_refusal(bundle, "manifest artifact digest is malformed")


@pytest.mark.parametrize("size", [None, -1, True, "1"])
def test_prepare_rejects_malformed_artifact_size_before_staging(
    tmp_path: Path, size: object
) -> None:
    bundle = legacy_bundle(tmp_path)
    manifest = _read_json(bundle / MANIFEST_NAME)
    output = next(output for stage in manifest["stages"].values() for output in stage["outputs"])
    output["bytes"] = size
    _write_json(bundle / MANIFEST_NAME, manifest)
    _assert_preflight_refusal(bundle, "manifest artifact size is malformed")


@pytest.mark.parametrize(
    ("artifact_path", "message"),
    [
        (None, "declared artifact path must be text"),
        ("", "declared artifact path escapes the bundle"),
        ("../outside", "declared artifact path escapes the bundle"),
        ("/tmp/outside", "declared artifact path escapes the bundle"),
        ("missing.txt", "declared bundle artifact is missing or not a regular file"),
        ("transcript", "declared bundle artifact is missing or not a regular file"),
    ],
)
def test_prepare_rejects_invalid_artifact_paths_before_staging(
    tmp_path: Path, artifact_path: object, message: str
) -> None:
    bundle = legacy_bundle(tmp_path)
    manifest = _read_json(bundle / MANIFEST_NAME)
    output = next(output for stage in manifest["stages"].values() for output in stage["outputs"])
    output.update({"path": artifact_path, "sha256": "a" * 64, "bytes": 1})
    _write_json(bundle / MANIFEST_NAME, manifest)
    _assert_preflight_refusal(bundle, message)


def test_prepare_rejects_hash_invalid_artifact_before_staging(tmp_path: Path) -> None:
    bundle = legacy_bundle(tmp_path)
    manifest = _read_json(bundle / MANIFEST_NAME)
    output = next(output for stage in manifest["stages"].values() for output in stage["outputs"])
    output["sha256"] = "0" * 64
    _write_json(bundle / MANIFEST_NAME, manifest)
    _assert_preflight_refusal(bundle, "declared artifact integrity")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"{", "cannot read valid source metadata"),
        (b"[]\n", "source metadata must contain a JSON object"),
        (b"\xff", "cannot read valid source metadata"),
    ],
)
def test_prepare_rejects_unreadable_source_document_before_staging(
    tmp_path: Path, raw: bytes, message: str
) -> None:
    bundle = legacy_bundle(tmp_path)
    (bundle / "source.json").write_bytes(raw)
    _repair_manifest_after_artifact_change(bundle)
    _assert_preflight_refusal(bundle, message)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sha256", None, "source metadata lacks a valid sha256 identity"),
        ("sha256", "bad", "source metadata lacks a valid sha256 identity"),
        ("sha256", "A" * 64, "source metadata lacks a valid sha256 identity"),
        ("bytes", None, "source metadata lacks a valid byte size"),
        ("bytes", -1, "source metadata lacks a valid byte size"),
        ("bytes", True, "source metadata lacks a valid byte size"),
        ("bytes", "1", "source metadata lacks a valid byte size"),
    ],
)
def test_prepare_rejects_malformed_source_identity_before_staging(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    bundle = legacy_bundle(tmp_path)
    source_path = bundle / "source.json"
    source = _read_json(source_path)
    if value is None:
        source.pop(field, None)
    else:
        source[field] = value
    _write_json(source_path, source)
    _repair_manifest_after_artifact_change(bundle)
    _assert_preflight_refusal(bundle, message)


@pytest.mark.parametrize(
    ("source_fields", "message"),
    [
        (None, "source metadata source is malformed"),
        ({}, "source metadata kind is malformed"),
        ({"kind": "unknown", "ref": "identity"}, "source metadata kind is malformed"),
        ({"kind": "local"}, "source metadata reference is malformed"),
        ({"kind": "local", "ref": 9}, "source metadata reference is malformed"),
    ],
)
def test_prepare_rejects_malformed_source_document_source_before_staging(
    tmp_path: Path, source_fields: object, message: str
) -> None:
    bundle = legacy_bundle(tmp_path)
    source_path = bundle / "source.json"
    source = _read_json(source_path)
    source["source"] = source_fields
    _write_json(source_path, source)
    _repair_manifest_after_artifact_change(bundle)
    _assert_preflight_refusal(bundle, message)


def test_prepare_rejects_cross_document_source_disagreement_before_staging(
    tmp_path: Path,
) -> None:
    bundle = legacy_bundle(tmp_path)
    manifest_path = bundle / MANIFEST_NAME
    source_path = bundle / "source.json"
    manifest = _read_json(manifest_path)
    source = _read_json(source_path)
    manifest["source"].update({"kind": "url", "ref": "https://example.invalid/a"})
    source["source"].update({"kind": "url", "ref": "https://example.invalid/b"})
    _write_json(source_path, source)
    _rewrite_manifest_hashes(bundle, manifest)
    _write_json(manifest_path, manifest)
    _assert_preflight_refusal(bundle, "manifest and source metadata identities disagree")


def test_prepare_rejects_source_marker_before_staging(tmp_path: Path) -> None:
    bundle = legacy_bundle(tmp_path)
    (bundle / MARKER_NAME).write_text("{}\n", encoding="utf-8")
    _assert_preflight_refusal(bundle, "source role contains a migration marker")


def _assert_source_unchanged_and_staging_removed(
    bundle: Path, before: dict[Path, tuple[str, bytes | str | None]]
) -> None:
    assert _tree_state(bundle) == before
    assert not os.path.lexists(_staging_role(bundle))


def test_prepare_rewrites_only_approved_local_identity_fields(tmp_path: Path) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)
    source_before = _read_json(bundle / "source.json")
    prepared = prepare_bundle_migration(bundle)
    staging = prepared.staging_dir
    manifest = _read_json(staging / MANIFEST_NAME)
    source = _read_json(staging / "source.json")
    metadata = _read_json(staging / "transcript" / "metadata.json")
    expected_ref = f"sha256:{source_before['sha256']}"
    assert manifest["schema_version"] == "1.0.0"
    assert manifest["source"]["ref"] == source["source"]["ref"] == expected_ref
    assert manifest["source"]["bytes"] == source["source"]["bytes"] == source_before["bytes"]
    assert "path" not in source["transcript_sidecar"]
    assert "path" not in metadata["source_media"]
    assert "path" not in metadata["backend"]
    assert _tree_state(bundle) == before


def test_prepare_preserves_remote_source_url(tmp_path: Path) -> None:
    bundle = legacy_bundle(tmp_path)
    manifest_path = bundle / MANIFEST_NAME
    source_path = bundle / "source.json"
    manifest = _read_json(manifest_path)
    source = _read_json(source_path)
    url = "https://example.invalid/talk"
    manifest["source"].update({"kind": "url", "ref": url})
    source["source"].update({"kind": "url", "ref": url})
    _write_json(source_path, source)
    _rewrite_manifest_hashes(bundle, manifest)
    _write_json(manifest_path, manifest)
    before = _tree_state(bundle)
    staging = prepare_bundle_migration(bundle).staging_dir
    migrated_manifest = _read_json(staging / MANIFEST_NAME)
    migrated_source = _read_json(staging / "source.json")
    assert migrated_manifest["source"] == manifest["source"]
    assert migrated_source["source"] == source["source"]
    assert migrated_manifest["source"]["ref"] == url
    assert _tree_state(bundle) == before


def test_prepare_refreshes_every_declared_output_identity(tmp_path: Path) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)
    staging = prepare_bundle_migration(bundle).staging_dir
    manifest = _read_json(staging / MANIFEST_NAME)
    records = [output for stage in manifest["stages"].values() for output in stage["outputs"]]
    assert records
    for output in records:
        assert (output["sha256"], output["bytes"]) == _digest(staging / output["path"])
    assert _tree_state(bundle) == before


def test_prepare_preserves_every_non_target_entry_and_records_snapshots(
    tmp_path: Path,
) -> None:
    bundle = legacy_bundle(tmp_path)
    (bundle / "empty-extra").mkdir()
    (bundle / "analysis" / "user-note.txt").write_bytes(b"user bytes\x00\xff")
    before = _tree_state(bundle)
    prepared = prepare_bundle_migration(bundle)
    after = _tree_state(prepared.staging_dir)
    target_paths = {
        Path(MANIFEST_NAME),
        Path("source.json"),
        Path("transcript/metadata.json"),
        Path(MARKER_NAME),
    }
    assert {path: value for path, value in after.items() if path not in target_paths} == {
        path: value for path, value in before.items() if path not in target_paths
    }
    assert {entry.path.as_posix() for entry in prepared.source_snapshot} == {
        path.as_posix() for path in before
    }
    assert {entry.path.as_posix() for entry in prepared.target_snapshot} == {
        path.as_posix() for path in after
    }
    assert _tree_state(bundle) == before


def test_prepare_rejects_copy_drift_and_removes_owned_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)
    real_copytree = migrations.shutil.copytree

    def copy_then_drift(source: Any, destination: Any, *args: Any, **kwargs: Any) -> Any:
        copied = real_copytree(source, destination, *args, **kwargs)
        if Path(source) == bundle:
            (Path(copied) / "transcript" / "transcript.md").write_text(
                "copy drift\n", encoding="utf-8"
            )
        return copied

    monkeypatch.setattr(migrations.shutil, "copytree", copy_then_drift)
    with pytest.raises(MigrationError, match="copied staging tree differs") as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    _assert_source_unchanged_and_staging_removed(bundle, before)


def test_prepare_rejects_transformation_drift_and_removes_owned_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)
    real_refresh = getattr(migrations, "_refresh_output_records", None)

    def drift_then_refresh(staging: Path, manifest: dict[str, Any]) -> None:
        (staging / "transcript" / "transcript.md").write_text(
            "transformation drift\n", encoding="utf-8"
        )
        assert real_refresh is not None
        real_refresh(staging, manifest)

    monkeypatch.setattr(migrations, "_refresh_output_records", drift_then_refresh, raising=False)
    with pytest.raises(MigrationError, match="changed a non-target source entry") as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    _assert_source_unchanged_and_staging_removed(bundle, before)


def test_prepare_detects_source_mutation_and_removes_owned_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)
    real_copytree = migrations.shutil.copytree

    def copy_then_mutate_source(source: Any, destination: Any, *args: Any, **kwargs: Any) -> Any:
        copied = real_copytree(source, destination, *args, **kwargs)
        if Path(source) == bundle:
            (Path(source) / "transcript" / "transcript.md").write_text(
                "concurrent source change\n", encoding="utf-8"
            )
        return copied

    monkeypatch.setattr(migrations.shutil, "copytree", copy_then_mutate_source)
    with pytest.raises(MigrationError, match="source tree changed") as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    expected = dict(before)
    expected[Path("transcript/transcript.md")] = (
        "file",
        b"concurrent source change\n",
    )
    assert _tree_state(bundle) == expected
    assert not os.path.lexists(_staging_role(bundle))


def test_marker_write_failure_removes_only_empty_reserved_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)
    real_write_json = getattr(migrations, "_write_json", None)

    def fail_marker(path: Path, value: object, role: str) -> None:
        if path.name == MARKER_NAME:
            raise MigrationError("cannot write migration marker")
        assert real_write_json is not None
        real_write_json(path, value, role)

    monkeypatch.setattr(migrations, "_write_json", fail_marker, raising=False)
    with pytest.raises(MigrationError, match="initialize the staging role") as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    _assert_source_unchanged_and_staging_removed(bundle, before)


def test_partial_copy_failure_removes_exactly_marked_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)

    def fail_copy(source: Path, destination: Path, **kwargs: Any) -> Path:
        del source, kwargs
        (destination / "partial.txt").write_text("partial\n", encoding="utf-8")
        raise OSError("synthetic copy failure")

    monkeypatch.setattr(migrations.shutil, "copytree", fail_copy)
    with pytest.raises(MigrationError, match="copy the source role") as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    _assert_source_unchanged_and_staging_removed(bundle, before)


def test_mismatched_marker_cleanup_refuses_and_preserves_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)

    def replace_marker_then_fail(source: Path, destination: Path, **kwargs: Any) -> Path:
        del source, kwargs
        _write_json(
            destination / MARKER_NAME,
            {
                "bundle_id": "different-owner",
                "source_version": "0.1.0",
                "target_version": "1.0.0",
            },
        )
        (destination / "owner.txt").write_text("retain\n", encoding="utf-8")
        raise OSError("synthetic copy failure")

    monkeypatch.setattr(migrations.shutil, "copytree", replace_marker_then_fail)
    with pytest.raises(
        MigrationError, match="cannot copy the source role and cannot remove owned staging"
    ) as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    staging = _staging_role(bundle)
    assert _tree_state(bundle) == before
    assert _read_json(staging / MARKER_NAME)["bundle_id"] == "different-owner"
    assert (staging / "owner.txt").read_text(encoding="utf-8") == "retain\n"


def test_exact_payload_marker_symlink_never_authorizes_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)
    bundle_id = _read_json(bundle / MANIFEST_NAME)["bundle_id"]
    external_marker = tmp_path / "external-marker.json"
    _write_json(
        external_marker,
        {
            "bundle_id": bundle_id,
            "source_version": "0.1.0",
            "target_version": "1.0.0",
        },
    )

    def replace_marker_with_symlink_then_fail(
        source: Path, destination: Path, **kwargs: Any
    ) -> Path:
        del source, kwargs
        marker = destination / MARKER_NAME
        marker.unlink()
        marker.symlink_to(external_marker)
        (destination / "foreign.txt").write_text("retain\n", encoding="utf-8")
        raise OSError("synthetic copy failure")

    monkeypatch.setattr(
        migrations.shutil,
        "copytree",
        replace_marker_with_symlink_then_fail,
    )
    with pytest.raises(
        MigrationError, match="cannot copy the source role and cannot remove owned staging"
    ) as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    staging = _staging_role(bundle)
    assert _tree_state(bundle) == before
    assert staging.is_dir()
    assert (staging / MARKER_NAME).is_symlink()
    assert (staging / "foreign.txt").read_text(encoding="utf-8") == "retain\n"
    assert external_marker.is_file()


def test_post_copy_marker_drift_is_not_blessed_or_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)
    real_derive = migrations._derive_target_documents  # pyright: ignore[reportPrivateUsage]

    def derive_then_drift_marker(
        source: Path,
        manifest: dict[str, Any],
        source_sha256: str,
        source_bytes: int,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        documents = real_derive(source, manifest, source_sha256, source_bytes)
        marker_path = _staging_role(bundle) / MARKER_NAME
        marker = _read_json(marker_path)
        marker["bundle_id"] = "different-owner"
        _write_json(marker_path, marker)
        return documents

    monkeypatch.setattr(migrations, "_derive_target_documents", derive_then_drift_marker)
    with pytest.raises(
        MigrationError, match="target preparation failed and owned staging cleanup also failed"
    ) as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    staging = _staging_role(bundle)
    assert _tree_state(bundle) == before
    assert staging.is_dir()
    assert _read_json(staging / MARKER_NAME)["bundle_id"] == "different-owner"


def test_target_model_failure_removes_only_owned_staging(tmp_path: Path) -> None:
    bundle = legacy_bundle(tmp_path)
    manifest_path = bundle / MANIFEST_NAME
    manifest = _read_json(manifest_path)
    manifest["created"] = "not-a-datetime"
    _write_json(manifest_path, manifest)
    before = _tree_state(bundle)
    with pytest.raises(MigrationError, match="current artifact models") as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    _assert_source_unchanged_and_staging_removed(bundle, before)


def test_target_cross_document_failure_is_detected_standalone(tmp_path: Path) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)
    prepared = prepare_bundle_migration(bundle)
    source_path = prepared.staging_dir / "source.json"
    source = _read_json(source_path)
    source["source"]["ref"] = f"sha256:{'f' * 64}"
    _write_json(source_path, source)
    manifest = _read_json(prepared.staging_dir / MANIFEST_NAME)
    _rewrite_manifest_hashes(prepared.staging_dir, manifest)
    _write_json(prepared.staging_dir / MANIFEST_NAME, manifest)
    with pytest.raises(MigrationError, match="manifest and source metadata disagree") as error:
        migrations._validate_target(  # pyright: ignore[reportPrivateUsage]
            prepared.staging_dir,
            expected_bundle_id=prepared.bundle_id,
            source_sha256=prepared.source_sha256,
            source_bytes=prepared.source_bytes,
        )
    assert str(bundle) not in str(error.value)
    assert _tree_state(bundle) == before
    assert prepared.staging_dir.is_dir()


def test_late_target_document_mutation_cannot_enter_the_final_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)
    real_assert_preserved = (
        migrations._assert_preserved_tree  # pyright: ignore[reportPrivateUsage]
    )
    calls = 0

    def mutate_after_second_preservation_check(
        target: Path, source_snapshot: migrations.TreeSnapshot
    ) -> None:
        nonlocal calls
        real_assert_preserved(target, source_snapshot)
        calls += 1
        if calls == 2:
            (target / "source.json").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(
        migrations,
        "_assert_preserved_tree",
        mutate_after_second_preservation_check,
    )
    with pytest.raises(
        MigrationError, match="target source metadata differs from approved transformation"
    ) as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    assert calls >= 2
    _assert_source_unchanged_and_staging_removed(bundle, before)


def test_malformed_transcript_metadata_removes_owned_staging(tmp_path: Path) -> None:
    bundle = legacy_bundle(tmp_path)
    metadata_path = bundle / "transcript" / "metadata.json"
    metadata_path.write_text("{", encoding="utf-8")
    _repair_manifest_after_artifact_change(bundle)
    before = _tree_state(bundle)
    with pytest.raises(MigrationError, match="cannot read valid transcript metadata") as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    _assert_source_unchanged_and_staging_removed(bundle, before)


def test_invalid_utf8_target_document_removes_owned_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)
    real_write_json = getattr(migrations, "_write_json", None)

    def write_then_corrupt(path: Path, value: object, role: str) -> None:
        assert real_write_json is not None
        real_write_json(path, value, role)
        if path.name == "source.json":
            path.write_bytes(b"\xff")

    monkeypatch.setattr(migrations, "_write_json", write_then_corrupt, raising=False)
    with pytest.raises(MigrationError, match="cannot read target source metadata") as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    _assert_source_unchanged_and_staging_removed(bundle, before)


def test_cleanup_failure_retains_exactly_marked_private_staging_mode_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = legacy_bundle(tmp_path)
    bundle.chmod(0o700)
    before = _tree_state(bundle)

    def fail_copy(source: Path, destination: Path, **kwargs: Any) -> Path:
        del source, destination, kwargs
        raise OSError("synthetic copy failure")

    def fail_cleanup(path: Path) -> None:
        del path
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(migrations.shutil, "copytree", fail_copy)
    monkeypatch.setattr(migrations.shutil, "rmtree", fail_cleanup)
    previous_umask = os.umask(0o022)
    try:
        with pytest.raises(MigrationError, match="cannot remove owned staging") as error:
            prepare_bundle_migration(bundle)
    finally:
        os.umask(previous_umask)
    assert str(bundle) not in str(error.value)
    staging = _staging_role(bundle)
    assert _tree_state(bundle) == before
    assert staging.is_dir()
    assert _read_json(staging / MARKER_NAME)["source_version"] == "0.1.0"
    source_mode = stat.S_IMODE(bundle.stat().st_mode)
    staging_mode = stat.S_IMODE(staging.stat().st_mode)
    assert staging_mode == 0o700
    assert staging_mode & 0o077 == 0
    assert staging_mode & ~source_mode == 0


def test_prepare_reserves_private_staging_mode_at_copy_entry_under_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = legacy_bundle(tmp_path)
    bundle.chmod(0o700)
    before = _tree_state(bundle)
    observed_modes: list[tuple[int, int]] = []
    real_copytree = migrations.shutil.copytree

    def record_modes(source: Any, destination: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(source) == bundle:
            observed_modes.append(
                (
                    stat.S_IMODE(Path(source).stat().st_mode),
                    stat.S_IMODE(Path(destination).stat().st_mode),
                )
            )
        return real_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(migrations.shutil, "copytree", record_modes)
    previous_umask = os.umask(0o022)
    try:
        prepared = prepare_bundle_migration(bundle)
    finally:
        os.umask(previous_umask)

    assert observed_modes == [(0o700, 0o700)]
    assert stat.S_IMODE(prepared.staging_dir.stat().st_mode) == 0o700
    assert _tree_state(bundle) == before


def test_prepare_rejects_deep_json_manifest_before_staging(tmp_path: Path) -> None:
    bundle = legacy_bundle(tmp_path)
    manifest_path = bundle / MANIFEST_NAME
    manifest = _read_json(manifest_path)
    manifest["deep_json_probe"] = _nested_json_object(129)
    _write_json(manifest_path, manifest)
    _assert_preflight_refusal(bundle, "cannot read valid source manifest")


def test_declared_artifact_status_error_is_path_free_before_staging(tmp_path: Path) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)
    restricted = bundle / "analysis"
    original_mode = stat.S_IMODE(restricted.stat().st_mode)
    restricted.chmod(0o000)
    try:
        with pytest.raises(
            MigrationError, match="cannot inspect a declared bundle artifact"
        ) as error:
            prepare_bundle_migration(bundle)
    finally:
        restricted.chmod(original_mode)
    assert str(bundle) not in str(error.value)
    assert _tree_state(bundle) == before
    assert not os.path.lexists(_staging_role(bundle))


@pytest.mark.parametrize(
    ("document", "message"),
    [
        pytest.param(
            "transcript-metadata",
            "cannot read valid transcript metadata",
            id="transcript-metadata",
        ),
        pytest.param(
            "transcript-segments",
            "cannot read valid target transcript segments",
            id="transcript-segments",
        ),
    ],
)
def test_prepare_rejects_deep_json_document_and_removes_owned_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: str,
    message: str,
) -> None:
    bundle = legacy_bundle(tmp_path)
    if document == "transcript-metadata":
        path = bundle / "transcript" / "metadata.json"
        value = _read_json(path)
        value["deep_json_probe"] = _nested_json_object(129)
        _write_json(path, value)
    else:
        path = bundle / "transcript" / "segments.json"
        segments = _read_json_array(path)
        segments[0]["deep_json_probe"] = _nested_json_object(129)
        _write_json(path, segments)
    _repair_manifest_after_artifact_change(bundle)
    before = _tree_state(bundle)
    copy_entries = 0
    real_copytree = migrations.shutil.copytree

    def record_copy(source: Any, destination: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal copy_entries
        if Path(source) == bundle:
            copy_entries += 1
        return real_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(migrations.shutil, "copytree", record_copy)
    with pytest.raises(MigrationError, match=message) as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    assert copy_entries == 1
    _assert_source_unchanged_and_staging_removed(bundle, before)


@pytest.mark.parametrize(
    ("document", "message", "post_marker"),
    [
        pytest.param("manifest", "cannot read valid source manifest", False, id="manifest"),
        pytest.param(
            "source-metadata",
            "cannot read valid source metadata",
            False,
            id="source-metadata",
        ),
        pytest.param(
            "transcript-metadata",
            "cannot read valid transcript metadata",
            True,
            id="transcript-metadata",
        ),
        pytest.param(
            "transcript-segments",
            "cannot read valid target transcript segments",
            True,
            id="transcript-segments",
        ),
    ],
)
@pytest.mark.parametrize(
    "constant",
    [
        pytest.param(float("nan"), id="NaN"),
        pytest.param(float("inf"), id="Infinity"),
        pytest.param(float("-inf"), id="minus-Infinity"),
    ],
)
def test_prepare_rejects_non_finite_json_across_real_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: str,
    message: str,
    post_marker: bool,
    constant: float,
) -> None:
    bundle = legacy_bundle(tmp_path)
    if document == "manifest":
        path = bundle / MANIFEST_NAME
        value = _read_json(path)
        value["non_finite_probe"] = constant
        _write_json(path, value)
    elif document == "source-metadata":
        path = bundle / "source.json"
        value = _read_json(path)
        value["non_finite_probe"] = constant
        _write_json(path, value)
        _repair_manifest_after_artifact_change(bundle)
    elif document == "transcript-metadata":
        path = bundle / "transcript" / "metadata.json"
        value = _read_json(path)
        value["non_finite_probe"] = constant
        _write_json(path, value)
        _repair_manifest_after_artifact_change(bundle)
    else:
        path = bundle / "transcript" / "segments.json"
        segments = _read_json_array(path)
        segments[0]["non_finite_probe"] = constant
        _write_json(path, segments)
        _repair_manifest_after_artifact_change(bundle)
    before = _tree_state(bundle)
    copy_entries = 0
    real_copytree = migrations.shutil.copytree

    def record_copy(source: Any, destination: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal copy_entries
        if Path(source) == bundle:
            copy_entries += 1
        return real_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(migrations.shutil, "copytree", record_copy)
    with pytest.raises(MigrationError, match=message) as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    assert bool(copy_entries) is post_marker
    _assert_source_unchanged_and_staging_removed(bundle, before)


def test_prepare_rejects_non_finite_json_created_during_transformation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = legacy_bundle(tmp_path)
    before = _tree_state(bundle)
    real_derive = migrations._derive_target_documents  # pyright: ignore[reportPrivateUsage]

    def inject_non_finite(
        source: Path,
        manifest: dict[str, Any],
        source_sha256: str,
        source_bytes: int,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        documents = real_derive(source, manifest, source_sha256, source_bytes)
        documents[1]["non_finite_probe"] = float("nan")
        return documents

    monkeypatch.setattr(migrations, "_derive_target_documents", inject_non_finite)
    with pytest.raises(MigrationError, match="serialize migration JSON") as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    _assert_source_unchanged_and_staging_removed(bundle, before)


def test_prepare_rejects_matching_strict_raw_duration_strings(
    tmp_path: Path,
) -> None:
    bundle = legacy_bundle(tmp_path)
    manifest_path = bundle / MANIFEST_NAME
    source_path = bundle / "source.json"
    manifest = _read_json(manifest_path)
    source = _read_json(source_path)
    manifest["source"]["duration_s"] = "1.25"
    source["source"]["duration_s"] = "1.25"
    _write_json(source_path, source)
    _rewrite_manifest_hashes(bundle, manifest)
    _write_json(manifest_path, manifest)
    before = _tree_state(bundle)
    with pytest.raises(MigrationError, match="current artifact models") as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    _assert_source_unchanged_and_staging_removed(bundle, before)


def test_prepare_rejects_strict_raw_transcript_metadata_numeric_string(
    tmp_path: Path,
) -> None:
    bundle = legacy_bundle(tmp_path)
    metadata_path = bundle / "transcript" / "metadata.json"
    metadata = _read_json(metadata_path)
    metadata["normalized_audio"]["bytes"] = str(metadata["normalized_audio"]["bytes"])
    _write_json(metadata_path, metadata)
    _repair_manifest_after_artifact_change(bundle)
    before = _tree_state(bundle)
    with pytest.raises(MigrationError, match="current artifact models") as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    _assert_source_unchanged_and_staging_removed(bundle, before)


def test_prepare_rejects_strict_raw_transcript_segment_numeric_string(
    tmp_path: Path,
) -> None:
    bundle = legacy_bundle(tmp_path)
    segments_path = bundle / "transcript" / "segments.json"
    segments = _read_json_array(segments_path)
    segments[0]["start_s"] = str(segments[0]["start_s"])
    _write_json(segments_path, segments)
    _repair_manifest_after_artifact_change(bundle)
    before = _tree_state(bundle)
    with pytest.raises(MigrationError, match="current artifact models") as error:
        prepare_bundle_migration(bundle)
    assert str(bundle) not in str(error.value)
    _assert_source_unchanged_and_staging_removed(bundle, before)
