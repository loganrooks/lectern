import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "public_safety_check.py"
SPEC = importlib.util.spec_from_file_location("public_safety_check", MODULE_PATH)
assert SPEC is not None
public_safety_check = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = public_safety_check
SPEC.loader.exec_module(public_safety_check)

content_patterns = public_safety_check.content_patterns
Candidate = public_safety_check.Candidate
forbidden_path_reason = public_safety_check.forbidden_path_reason
scan_content = public_safety_check.scan_content


def write_tmp(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def messages_for(path: Path) -> list[str]:
    candidate = Candidate(path=path, source="worktree")
    return [finding.message for finding in scan_content(candidate, content_patterns())]


def test_forbidden_public_paths_are_blocked() -> None:
    assert forbidden_path_reason(Path("goal/GOAL.md")) is not None
    assert forbidden_path_reason(Path("bundles/example/manifest.json")) is not None
    assert forbidden_path_reason(Path("AGENTS.md")) is not None
    assert forbidden_path_reason(Path("docs/AGENTS.md")) is not None
    assert forbidden_path_reason(Path("src/CLAUDE.md")) is not None
    assert forbidden_path_reason(Path("tools/gate_check.py")) is not None
    assert forbidden_path_reason(Path("tests/fixtures/talk.mp4")) is not None
    assert forbidden_path_reason(Path("tests/fixtures/recording.wav")) is not None


def test_normal_project_vocabulary_is_allowed(tmp_path: Path) -> None:
    path = write_tmp(
        tmp_path / "docs.md",
        "Transcription supports the Opus audio codec and WAV.\n"
        "Owner: maintainer team.\n"
        "A fable in a documentation example is ordinary prose.\n",
    )

    assert messages_for(path) == []


def test_private_governance_terms_are_blocked(tmp_path: Path) -> None:
    path = write_tmp(
        tmp_path / "notes.md",
        "Do not publish GOAL_CONTRACT details, goal/ state, or review-journal output.\n",
    )

    assert messages_for(path) == [
        "private governance term",
        "private governance term",
        "private governance term",
    ]


def test_modern_secret_shapes_are_blocked(tmp_path: Path) -> None:
    path = write_tmp(
        tmp_path / "leak.txt",
        "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA\n"
        "sk-proj-BBBBBBBBBBBBBBBBBBBBBBBB\n"
        "sk-svcacct-CCCCCCCCCCCCCCCCCCCCCCCC\n",
    )

    assert messages_for(path) == ["possible secret", "possible secret", "possible secret"]


def test_placeholder_env_docs_are_allowed(tmp_path: Path) -> None:
    path = write_tmp(
        tmp_path / "onboarding.md",
        "Set OPENAI_API_KEY=your_key_here in your shell.\nExample: PASSWORD=changeme123\n",
    )

    assert messages_for(path) == []


def test_index_content_is_scanned_even_when_worktree_differs(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    staged = tmp_path / "staged.txt"
    staged.write_text("safe working tree text\n", encoding="utf-8")
    candidate = Candidate(path=Path("staged.txt"), source="index")

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        del args, kwargs
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b"sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA\n",
        )

    monkeypatch.setattr(public_safety_check.subprocess, "run", fake_run)

    assert [finding.message for finding in scan_content(candidate, content_patterns())] == [
        "possible secret"
    ]
