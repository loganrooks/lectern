"""Refresh stale PR-head Codex review CI after connector review comments."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import NoReturn, cast

import check_codex_review

DEFAULT_RERUN_EVENTS = frozenset({"pull_request", "pull_request_review"})


@dataclass(frozen=True)
class WorkflowRun:
    run_id: int
    event: str
    head_sha: str
    status: str
    conclusion: str | None
    created_at: str


@dataclass(frozen=True)
class WorkflowJob:
    job_id: int
    name: str
    status: str
    conclusion: str | None


def fail(message: str) -> NoReturn:
    print(f"codex review refresh: {message}", file=sys.stderr)
    raise SystemExit(1)


def as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    raise RuntimeError(f"expected object, got {type(value).__name__}")


def as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return cast(list[object], value)
    raise RuntimeError(f"expected list, got {type(value).__name__}")


def as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def as_int(value: object) -> int:
    if isinstance(value, int):
        return value
    raise RuntimeError(f"expected int, got {type(value).__name__}")


def repository_path(repository: str) -> str:
    try:
        owner, repo = repository.split("/", 1)
    except ValueError as error:
        raise RuntimeError("GITHUB_REPOSITORY must be OWNER/REPO") from error
    return "/repos/{}/{}".format(
        urllib.parse.quote(owner, safe=""),
        urllib.parse.quote(repo, safe=""),
    )


def github_request(
    token: str,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> object:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response_body = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub REST request failed: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"GitHub REST request failed: {error}") from error

    if not response_body:
        return {}
    return json.loads(response_body.decode("utf-8"))


def pull_request_head_sha(token: str, repository: str, pr_number: int) -> str:
    payload = github_request(token, "GET", f"{repository_path(repository)}/pulls/{pr_number}")
    head_sha = as_str(as_dict(as_dict(payload).get("head", {})).get("sha", ""))
    if not head_sha:
        raise RuntimeError("pull request head SHA was missing")
    return head_sha


def workflow_runs(
    token: str,
    repository: str,
    workflow_id: str,
    head_sha: str,
) -> list[WorkflowRun]:
    query = urllib.parse.urlencode(
        {
            "head_sha": head_sha,
            "status": "failure",
            "per_page": "20",
        }
    )
    workflow = urllib.parse.quote(workflow_id, safe="")
    payload = github_request(
        token,
        "GET",
        f"{repository_path(repository)}/actions/workflows/{workflow}/runs?{query}",
    )

    runs: list[WorkflowRun] = []
    for raw_run in as_list(as_dict(payload).get("workflow_runs", [])):
        run = as_dict(raw_run)
        runs.append(
            WorkflowRun(
                run_id=as_int(run.get("id")),
                event=as_str(run.get("event", "")),
                head_sha=as_str(run.get("head_sha", "")),
                status=as_str(run.get("status", "")),
                conclusion=as_str(run.get("conclusion"))
                if run.get("conclusion") is not None
                else None,
                created_at=as_str(run.get("created_at", "")),
            )
        )
    return runs


def workflow_jobs(token: str, repository: str, run_id: int) -> list[WorkflowJob]:
    query = urllib.parse.urlencode({"filter": "latest", "per_page": "100"})
    payload = github_request(
        token,
        "GET",
        f"{repository_path(repository)}/actions/runs/{run_id}/jobs?{query}",
    )

    jobs: list[WorkflowJob] = []
    for raw_job in as_list(as_dict(payload).get("jobs", [])):
        job = as_dict(raw_job)
        jobs.append(
            WorkflowJob(
                job_id=as_int(job.get("id")),
                name=as_str(job.get("name", "")),
                status=as_str(job.get("status", "")),
                conclusion=as_str(job.get("conclusion"))
                if job.get("conclusion") is not None
                else None,
            )
        )
    return jobs


def rerunnable_runs(
    runs: list[WorkflowRun],
    head_sha: str,
    allowed_events: set[str],
) -> list[WorkflowRun]:
    candidates = [
        run
        for run in runs
        if run.head_sha == head_sha
        and run.status == "completed"
        and run.conclusion == "failure"
        and run.event in allowed_events
    ]
    return sorted(candidates, key=lambda run: run.created_at, reverse=True)


def failed_review_jobs(jobs: list[WorkflowJob], job_name: str) -> list[WorkflowJob]:
    return [
        job
        for job in jobs
        if job.name == job_name and job.status == "completed" and job.conclusion == "failure"
    ]


def rerun_job(token: str, repository: str, job_id: int) -> None:
    github_request(
        token,
        "POST",
        f"{repository_path(repository)}/actions/jobs/{job_id}/rerun",
        {},
    )


def allowed_events_from_env() -> set[str]:
    raw = os.environ.get("CODEX_REVIEW_RERUN_EVENTS", "")
    if not raw:
        return set(DEFAULT_RERUN_EVENTS)
    return {event.strip() for event in raw.split(",") if event.strip()}


def main() -> int:
    review_result = check_codex_review.main()
    if review_result != 0:
        return review_result

    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    workflow_id = os.environ.get("CODEX_REVIEW_WORKFLOW_ID", "ci.yml")
    job_name = os.environ.get("CODEX_REVIEW_JOB_NAME", "codex review")
    missing = [
        name
        for name, value in {
            "GITHUB_TOKEN": token,
            "GITHUB_REPOSITORY": repository,
            "PR_NUMBER": pr_number,
            "CODEX_REVIEW_WORKFLOW_ID": workflow_id,
            "CODEX_REVIEW_JOB_NAME": job_name,
        }.items()
        if not value
    ]
    if missing:
        fail(f"missing {', '.join(missing)}")

    try:
        number = int(pr_number)
        head_sha = pull_request_head_sha(token, repository, number)
        runs = workflow_runs(token, repository, workflow_id, head_sha)
        candidates = rerunnable_runs(runs, head_sha, allowed_events_from_env())
        if not candidates:
            print(f"codex review refresh: no stale failed CI run found for {head_sha}")
            return 0

        rerun_count = 0
        for candidate in candidates:
            jobs = workflow_jobs(token, repository, candidate.run_id)
            review_jobs = failed_review_jobs(jobs, job_name)
            if not review_jobs:
                print(
                    f"codex review refresh: run {candidate.run_id} has no failed {job_name!r} job"
                )
                continue
            for job in review_jobs:
                rerun_job(token, repository, job.job_id)
                rerun_count += 1
    except ValueError:
        print("codex review refresh: invalid PR_NUMBER", file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(f"codex review refresh: {error}", file=sys.stderr)
        return 1

    if rerun_count == 0:
        print(f"codex review refresh: no failed {job_name!r} jobs found for {head_sha}")
        return 0

    print(f"codex review refresh: reran {rerun_count} failed {job_name!r} job(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
