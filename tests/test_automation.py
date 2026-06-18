from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from lectern.automation import (
    STATE_SCHEMA_VERSION,
    AutomationError,
    QueueState,
    SourcePolicy,
    open_state,
)
from lectern.bundle import Manifest, StageName
from lectern.ingest import IngestError

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC_TALK = FIXTURE_DIR / "synthetic_talk.wav"
SYNTHETIC_TRANSCRIPT = FIXTURE_DIR / "synthetic_talk.transcript.txt"


def copy_fixture(directory: Path, name: str = "synthetic_talk.wav") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    media = directory / name
    media.write_bytes(SYNTHETIC_TALK.read_bytes())
    media.with_suffix(".transcript.txt").write_text(
        SYNTHETIC_TRANSCRIPT.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return media


def test_state_store_initializes_with_schema_version(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"

    with open_state(state_path):
        pass

    with sqlite3.connect(state_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert version == STATE_SCHEMA_VERSION


def test_state_store_rejects_unknown_future_schema(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    with sqlite3.connect(state_path) as connection:
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(AutomationError, match="unsupported automation state schema"):
        open_state(state_path)


def test_source_scan_reports_delta_and_rescan_is_idempotent(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    media = copy_fixture(source_dir)
    state_path = tmp_path / "state.sqlite"

    with open_state(state_path) as state:
        source = state.add_local_folder_source("talks", source_dir)
        duplicate = state.add_local_folder_source("talks", source_dir)
        first = state.scan_source(source.id)
        second = state.scan_source(source.id)

        media.write_bytes(SYNTHETIC_TALK.read_bytes() + b"changed")
        changed = state.scan_source(source.id)

        media.unlink()
        removed = state.scan_source(source.id)

    assert duplicate == source
    assert [item.relative_path for item in first.added] == ["synthetic_talk.wav"]
    assert len(first.queued) == 1
    assert first.queued[0].state is QueueState.DISCOVERED
    assert second.added == []
    assert second.changed == []
    assert second.removed == []
    assert [item.relative_path for item in second.unchanged] == ["synthetic_talk.wav"]
    assert [item.relative_path for item in changed.changed] == ["synthetic_talk.wav"]
    assert [item.relative_path for item in removed.removed] == ["synthetic_talk.wav"]


def test_source_scan_excludes_local_state_and_bundle_output_dirs(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    copy_fixture(source_dir)
    copy_fixture(source_dir / ".lectern", "state-audio.wav")

    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_local_folder_source("talks", source_dir)
        first = state.scan_source(source.id)
        approved = state.approve_queue_item(first.queued[0].id)
        state.ingest_queue_item(approved.id, source_dir / "bundles")
        second = state.scan_source(source.id)

    assert [item.relative_path for item in first.added] == ["synthetic_talk.wav"]
    assert second.added == []
    assert second.changed == []
    assert [item.relative_path for item in second.unchanged] == ["synthetic_talk.wav"]
    assert second.queued == []


def test_source_scan_does_not_skip_user_directory_named_bundles(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    copy_fixture(source_dir / "archive" / "bundles")

    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_local_folder_source("talks", source_dir)
        delta = state.scan_source(source.id)

    assert [item.relative_path for item in delta.added] == ["archive/bundles/synthetic_talk.wav"]


def test_source_scan_discovers_common_video_container_extensions(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    copy_fixture(source_dir, "synthetic_talk.mov")

    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_local_folder_source("talks", source_dir)
        delta = state.scan_source(source.id)

    assert [item.relative_path for item in delta.added] == ["synthetic_talk.mov"]


def test_source_scan_skips_symlinks_that_escape_source_root(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    outside_media = copy_fixture(tmp_path / "outside")
    symlink = source_dir / "linked.wav"
    source_dir.mkdir()
    try:
        symlink.symlink_to(outside_media)
    except OSError as exc:
        pytest.skip(f"symlink creation is not supported here: {exc}")

    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_local_folder_source("talks", source_dir)
        delta = state.scan_source(source.id)

    assert delta.added == []
    assert delta.queued == []


def test_policy_states_control_queueing(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    disabled_dir = tmp_path / "disabled"
    copy_fixture(source_dir)
    copy_fixture(disabled_dir)

    with open_state(tmp_path / "state.sqlite") as state:
        scan_only = state.add_local_folder_source(
            "scan-only",
            source_dir,
            SourcePolicy.SCAN_ONLY,
        )
        disabled = state.add_local_folder_source(
            "disabled",
            disabled_dir,
            SourcePolicy.DISABLED,
        )

        scan_only_delta = state.scan_source(scan_only.id)
        disabled_delta = state.scan_source(disabled.id)

    assert len(scan_only_delta.added) == 1
    assert scan_only_delta.queued == []
    assert disabled_delta.added == []
    assert disabled_delta.queued == []


def test_queue_approval_ingests_bundle_with_provenance_and_library_record(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    copy_fixture(source_dir)
    output_root = tmp_path / "bundles"

    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_local_folder_source("talks", source_dir)
        delta = state.scan_source(source.id)
        queue_item = delta.queued[0]

        with pytest.raises(AutomationError, match="requires explicit approval"):
            state.ingest_queue_item(queue_item.id, output_root)

        approved = state.approve_queue_item(queue_item.id)
        result = state.ingest_queue_item(approved.id, output_root)
        completed = state.get_queue_item(approved.id)
        library = state.get_library_bundle(result.manifest.bundle_id)

    source_json = json.loads((result.bundle_dir / "source.json").read_text(encoding="utf-8"))
    source_json_hash = hashlib.sha256((result.bundle_dir / "source.json").read_bytes()).hexdigest()
    provenance = source_json["provenance"]
    manifest = Manifest.load(result.bundle_dir)

    assert completed.state is QueueState.COMPLETED
    assert completed.bundle_id == result.manifest.bundle_id
    assert library.queue_item_id == completed.id
    assert provenance["source_id"] == source.id
    assert provenance["source_item_id"] == queue_item.source_item_id
    assert provenance["queue_item_id"] == queue_item.id
    assert provenance["consent"] == "explicit_queue_approval"
    assert provenance["remote_services"]["allowed"] is False
    assert manifest.stages[StageName.ACQUIRE].outputs[0].path == "source.json"
    assert manifest.stages[StageName.ACQUIRE].outputs[0].sha256 == source_json_hash


def test_queue_ingest_rejects_file_changed_after_approval(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    media = copy_fixture(source_dir)

    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_local_folder_source("talks", source_dir)
        queue_item = state.scan_source(source.id).queued[0]
        approved = state.approve_queue_item(queue_item.id)
        media.write_bytes(SYNTHETIC_TALK.read_bytes() + b"changed")

        with pytest.raises(AutomationError, match="changed since queue approval"):
            state.ingest_queue_item(approved.id, tmp_path / "bundles")
        failed = state.get_queue_item(approved.id)

    assert failed.state is QueueState.FAILED


def test_queue_ingest_rejects_transcript_sidecar_changed_after_approval(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    media = copy_fixture(source_dir)
    sidecar = media.with_suffix(".transcript.txt")

    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_local_folder_source("talks", source_dir)
        queue_item = state.scan_source(source.id).queued[0]
        approved = state.approve_queue_item(queue_item.id)
        sidecar.write_text("changed transcript\n", encoding="utf-8")

        with pytest.raises(AutomationError, match="changed since queue approval"):
            state.ingest_queue_item(approved.id, tmp_path / "bundles")


def test_one_shot_ingest_expands_user_home_before_state_setup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    source = copy_fixture(home / "source")
    monkeypatch.setenv("HOME", str(home))

    with open_state(tmp_path / "state.sqlite") as state:
        result = state.ingest_one_shot(
            Path("~/source") / source.name,
            tmp_path / "bundles",
        )
        queue_item = state.list_queue()[0]

    assert result.bundle_dir.is_dir()
    assert queue_item.state is QueueState.COMPLETED


def test_one_shot_ingest_validates_source_before_recording_state(tmp_path: Path) -> None:
    with open_state(tmp_path / "state.sqlite") as state:
        with pytest.raises(IngestError, match="source file does not exist"):
            state.ingest_one_shot(tmp_path / "missing.wav", tmp_path / "bundles")

        assert state.list_sources() == []
        assert state.list_queue() == []


def test_one_shot_ingest_records_source_and_queue_provenance(tmp_path: Path) -> None:
    source = copy_fixture(tmp_path / "source")

    with open_state(tmp_path / "state.sqlite") as state:
        result = state.ingest_one_shot(source, tmp_path / "bundles")
        queue_items = state.list_queue()
        library = state.list_library()

    source_json = json.loads((result.bundle_dir / "source.json").read_text(encoding="utf-8"))
    provenance = source_json["provenance"]

    assert len(queue_items) == 1
    assert queue_items[0].state is QueueState.COMPLETED
    assert provenance["consent"] == "explicit_cli_invocation"
    assert provenance["queue_item_id"] == queue_items[0].id
    assert [bundle.bundle_id for bundle in library] == [result.manifest.bundle_id]


def test_duplicate_content_different_sources_do_not_overwrite_provenance(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    copy_fixture(first_dir)
    copy_fixture(second_dir)

    with open_state(tmp_path / "state.sqlite") as state:
        first_source = state.add_local_folder_source("first", first_dir)
        first_queue = state.scan_source(first_source.id).queued[0]
        state.approve_queue_item(first_queue.id)
        first_result = state.ingest_queue_item(first_queue.id, tmp_path / "bundles")

        second_source = state.add_local_folder_source("second", second_dir)
        second_queue = state.scan_source(second_source.id).queued[0]
        state.approve_queue_item(second_queue.id)
        with pytest.raises(AutomationError, match="duplicate-content multi-source"):
            state.ingest_queue_item(second_queue.id, tmp_path / "bundles")
        failed = state.get_queue_item(second_queue.id)

    source_json = json.loads((first_result.bundle_dir / "source.json").read_text(encoding="utf-8"))
    assert source_json["provenance"]["queue_item_id"] == first_queue.id
    assert failed.state is QueueState.FAILED


def test_queue_skip_and_retry_are_inspectable(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    copy_fixture(source_dir)

    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_local_folder_source("talks", source_dir)
        queue_item = state.scan_source(source.id).queued[0]
        skipped = state.skip_queue_item(queue_item.id)
        retried = state.retry_queue_item(queue_item.id)

    assert skipped.state is QueueState.SKIPPED
    assert retried.state is QueueState.DISCOVERED


def test_local_folder_scan_does_not_open_network_socket(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_dir = tmp_path / "source"
    copy_fixture(source_dir)

    def fail_socket(*args: object, **kwargs: object) -> socket.socket:
        raise AssertionError("local source scan attempted network access")

    monkeypatch.setattr(socket, "socket", fail_socket)

    with open_state(tmp_path / "state.sqlite") as state:
        source = state.add_local_folder_source("talks", source_dir)
        delta = state.scan_source(source.id)

    assert len(delta.added) == 1
