from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from pytest import MonkeyPatch

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
MODULE_PATH = TOOLS / "refresh_codex_review_check.py"

sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location("refresh_codex_review_check", MODULE_PATH)
assert SPEC and SPEC.loader
refresh_codex_review_check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = refresh_codex_review_check
SPEC.loader.exec_module(refresh_codex_review_check)

WorkflowRun = refresh_codex_review_check.WorkflowRun
WorkflowJob = refresh_codex_review_check.WorkflowJob
failed_review_jobs = refresh_codex_review_check.failed_review_jobs
rerunnable_runs = refresh_codex_review_check.rerunnable_runs


def run(
    run_id: int,
    *,
    event: str = "pull_request",
    head_sha: str = "abc123",
    status: str = "completed",
    conclusion: str | None = "failure",
    created_at: str = "2026-06-18T00:00:00Z",
) -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        event=event,
        head_sha=head_sha,
        status=status,
        conclusion=conclusion,
        created_at=created_at,
    )


def job(
    job_id: int,
    *,
    name: str = "codex review",
    status: str = "completed",
    conclusion: str | None = "failure",
) -> WorkflowJob:
    return WorkflowJob(
        job_id=job_id,
        name=name,
        status=status,
        conclusion=conclusion,
    )


def test_rerunnable_runs_selects_failed_pr_head_runs_newest_first() -> None:
    candidates = rerunnable_runs(
        [
            run(1, created_at="2026-06-18T00:00:00Z"),
            run(2, event="workflow_dispatch", created_at="2026-06-18T00:02:00Z"),
            run(3, head_sha="old123", created_at="2026-06-18T00:03:00Z"),
            run(4, created_at="2026-06-18T00:04:00Z"),
        ],
        "abc123",
        {"pull_request", "pull_request_review"},
    )

    assert candidates == [
        run(4, created_at="2026-06-18T00:04:00Z"),
        run(1, created_at="2026-06-18T00:00:00Z"),
    ]


def test_rerunnable_runs_ignores_success_pending_and_disallowed_events() -> None:
    candidates = rerunnable_runs(
        [
            run(1, conclusion="success"),
            run(2, status="in_progress", conclusion=None),
            run(3, event="issue_comment"),
            run(4, event="pull_request_review"),
        ],
        "abc123",
        {"pull_request"},
    )

    assert candidates == []


def test_failed_review_jobs_selects_only_failed_target_review_job() -> None:
    assert failed_review_jobs(
        [
            job(1, name="verify"),
            job(2, name="codex review", conclusion="success"),
            job(3, name="codex review", status="in_progress", conclusion=None),
            job(4, name="codex review"),
        ],
        "codex review",
    ) == [job(4)]


def test_main_verifies_review_before_rerunning_failed_review_jobs(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, int | None]] = []

    def fake_check_codex_review() -> int:
        calls.append(("check", "", None))
        return 0

    def fake_pull_request_head_sha(token: str, repository: str, pr_number: int) -> str:
        calls.append((token, repository, pr_number))
        return "abc123"

    def fake_workflow_runs(
        token: str, repository: str, workflow_id: str, head_sha: str
    ) -> list[WorkflowRun]:
        calls.append((token, f"{repository}:{workflow_id}:{head_sha}", None))
        return [run(10), run(11, event="pull_request_review")]

    def fake_workflow_jobs(token: str, repository: str, run_id: int) -> list[WorkflowJob]:
        calls.append((token, f"{repository}:jobs", run_id))
        return [job(run_id * 10, name="verify"), job(run_id * 10 + 1)]

    def fake_rerun_job(token: str, repository: str, job_id: int) -> None:
        calls.append((token, repository, job_id))

    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "loganrooks/lectern")
    monkeypatch.setenv("PR_NUMBER", "4")
    monkeypatch.setattr(
        refresh_codex_review_check.check_codex_review,
        "main",
        fake_check_codex_review,
    )
    monkeypatch.setattr(
        refresh_codex_review_check,
        "pull_request_head_sha",
        fake_pull_request_head_sha,
    )
    monkeypatch.setattr(refresh_codex_review_check, "workflow_runs", fake_workflow_runs)
    monkeypatch.setattr(refresh_codex_review_check, "workflow_jobs", fake_workflow_jobs)
    monkeypatch.setattr(refresh_codex_review_check, "rerun_job", fake_rerun_job)

    assert refresh_codex_review_check.main() == 0
    assert calls == [
        ("check", "", None),
        ("token", "loganrooks/lectern", 4),
        ("token", "loganrooks/lectern:ci.yml:abc123", None),
        ("token", "loganrooks/lectern:jobs", 10),
        ("token", "loganrooks/lectern", 101),
        ("token", "loganrooks/lectern:jobs", 11),
        ("token", "loganrooks/lectern", 111),
    ]


def test_main_does_not_rerun_when_review_gate_fails(monkeypatch: MonkeyPatch) -> None:
    def fake_check_codex_review() -> int:
        return 1

    def unexpected_pull_request_head_sha(token: str, repository: str, pr_number: int) -> str:
        raise AssertionError("should not inspect runs when review evidence fails")

    monkeypatch.setattr(
        refresh_codex_review_check.check_codex_review,
        "main",
        fake_check_codex_review,
    )
    monkeypatch.setattr(
        refresh_codex_review_check,
        "pull_request_head_sha",
        unexpected_pull_request_head_sha,
    )

    assert refresh_codex_review_check.main() == 1


def test_main_passes_without_rerun_when_no_stale_failed_run(monkeypatch: MonkeyPatch) -> None:
    rerun_called = False

    def fake_check_codex_review() -> int:
        return 0

    def fake_pull_request_head_sha(token: str, repository: str, pr_number: int) -> str:
        return "abc123"

    def fake_workflow_runs(
        token: str, repository: str, workflow_id: str, head_sha: str
    ) -> list[WorkflowRun]:
        return [run(10)]

    def fake_workflow_jobs(token: str, repository: str, run_id: int) -> list[WorkflowJob]:
        return [job(10, name="verify")]

    def fake_rerun_job(token: str, repository: str, job_id: int) -> None:
        nonlocal rerun_called
        rerun_called = True

    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "loganrooks/lectern")
    monkeypatch.setenv("PR_NUMBER", "4")
    monkeypatch.setattr(
        refresh_codex_review_check.check_codex_review,
        "main",
        fake_check_codex_review,
    )
    monkeypatch.setattr(
        refresh_codex_review_check,
        "pull_request_head_sha",
        fake_pull_request_head_sha,
    )
    monkeypatch.setattr(refresh_codex_review_check, "workflow_runs", fake_workflow_runs)
    monkeypatch.setattr(refresh_codex_review_check, "workflow_jobs", fake_workflow_jobs)
    monkeypatch.setattr(refresh_codex_review_check, "rerun_job", fake_rerun_job)

    assert refresh_codex_review_check.main() == 0
    assert not rerun_called
