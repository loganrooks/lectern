"""CLI scaffold behavior for M0."""

from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from lectern import __version__, cli
from lectern.bundle import Manifest, StageName, StageState, export_json_schema


def test_version_flag(capsys: CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"lectern {__version__}\n"
    assert captured.err == ""


def test_doctor_reports_required_local_tools(
    capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    def fake_which(name: str) -> str | None:
        if name == "ffmpeg":
            return "/usr/bin/ffmpeg"
        return None

    monkeypatch.setattr(cli.shutil, "which", fake_which)

    assert cli.main(["doctor"]) == 0
    captured = capsys.readouterr()
    assert "python: OK" in captured.out
    assert "ffmpeg: OK (/usr/bin/ffmpeg)" in captured.out
    assert captured.err == ""


def test_doctor_fails_when_required_tool_is_missing(
    capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    def fake_which(name: str) -> str | None:
        return None

    monkeypatch.setattr(cli.shutil, "which", fake_which)

    assert cli.main(["doctor"]) == 1
    captured = capsys.readouterr()
    assert "ffmpeg: MISSING" in captured.out
    assert captured.err == ""


def test_schema_export_writes_manifest_schema(tmp_path: Path) -> None:
    output = tmp_path / "manifest.schema.json"

    assert cli.main(["schema", "export", "--output", str(output)]) == 0

    assert output.read_text() == export_json_schema()


def test_ingest_command_writes_bundle(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    source = Path("tests/fixtures/synthetic_talk.wav")

    assert cli.main(["ingest", str(source), "--output", str(tmp_path)]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    bundle_dir = Path(captured.out.strip())
    assert bundle_dir.is_dir()
    manifest = Manifest.load(bundle_dir)
    assert manifest.stages[StageName.TRANSCRIBE].state is StageState.DONE
