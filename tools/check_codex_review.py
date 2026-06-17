"""Require a submitted Codex connector review on the current PR head."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import cast

SUBMITTED_STATES = {"APPROVED", "COMMENTED"}


@dataclass(frozen=True)
class Review:
    author: str
    state: str
    commit_oid: str | None


@dataclass(frozen=True)
class ReviewPage:
    reviews: list[Review]
    has_previous_page: bool
    start_cursor: str | None


def as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return {}


def as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return cast(list[object], value)
    return []


def as_str(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def raise_for_graphql_errors(payload: object) -> None:
    errors = as_list(as_dict(payload).get("errors"))
    if not errors:
        return

    messages: list[str] = []
    for error in errors:
        error_message = as_str(as_dict(error).get("message"))
        messages.append(error_message or json.dumps(error, sort_keys=True))

    raise RuntimeError("GitHub GraphQL error: " + "; ".join(messages))


def pull_request_from_payload(payload: object) -> dict[str, object]:
    root = as_dict(payload)
    data = as_dict(root.get("data"))
    repository = as_dict(data.get("repository"))
    return as_dict(repository.get("pullRequest"))


def reviews_from_payload(payload: object) -> list[Review]:
    pull_request = pull_request_from_payload(payload)
    reviews = as_dict(pull_request.get("reviews"))
    nodes = as_list(reviews.get("nodes"))

    parsed: list[Review] = []
    for node_value in nodes:
        node = as_dict(node_value)
        author = as_dict(node.get("author"))
        commit = as_dict(node.get("commit"))
        parsed.append(
            Review(
                author=as_str(author.get("login")),
                state=as_str(node.get("state")),
                commit_oid=as_str(commit.get("oid")) or None,
            )
        )
    return parsed


def review_page_from_payload(payload: object) -> ReviewPage:
    pull_request = pull_request_from_payload(payload)
    reviews = as_dict(pull_request.get("reviews"))
    page_info = as_dict(reviews.get("pageInfo"))
    return ReviewPage(
        reviews=reviews_from_payload(payload),
        has_previous_page=page_info.get("hasPreviousPage") is True,
        start_cursor=as_str(page_info.get("startCursor")) or None,
    )


def normalize_login(login: str) -> str:
    return login.removesuffix("[bot]")


def reviewer_login_matches(author: str, reviewer_login: str) -> bool:
    return normalize_login(author) == normalize_login(reviewer_login)


def has_required_review(reviews: list[Review], reviewer_login: str, head_sha: str) -> bool:
    return any(
        reviewer_login_matches(review.author, reviewer_login)
        and review.state in SUBMITTED_STATES
        and review.commit_oid == head_sha
        for review in reviews
    )


def graphql_request(token: str, query: str, variables: dict[str, object]) -> object:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
            raise_for_graphql_errors(payload)
            return payload
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub GraphQL request failed: {error.code} {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"GitHub GraphQL request failed: {error}") from error


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    head_sha = os.environ.get("HEAD_SHA", "")
    reviewer_login = os.environ.get("CODEX_REVIEWER_LOGIN", "chatgpt-codex-connector")

    missing = [
        name
        for name, value in {
            "GITHUB_TOKEN": token,
            "GITHUB_REPOSITORY": repository,
            "PR_NUMBER": pr_number,
            "HEAD_SHA": head_sha,
        }.items()
        if not value
    ]
    if missing:
        print(f"missing required environment: {', '.join(missing)}", file=sys.stderr)
        return 1

    try:
        owner, repo = repository.split("/", 1)
        number = int(pr_number)
    except ValueError:
        print("invalid GITHUB_REPOSITORY or PR_NUMBER", file=sys.stderr)
        return 1

    query = """
    query($owner: String!, $repo: String!, $number: Int!, $before: String) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          reviews(last: 100, before: $before) {
            pageInfo {
              hasPreviousPage
              startCursor
            }
            nodes {
              author { login }
              state
              commit { oid }
            }
          }
        }
      }
    }
    """
    before: str | None = None
    try:
        while True:
            payload = graphql_request(
                token,
                query,
                {"owner": owner, "repo": repo, "number": number, "before": before},
            )
            review_page = review_page_from_payload(payload)
            if has_required_review(review_page.reviews, reviewer_login, head_sha):
                print(f"codex review: found {reviewer_login} review for {head_sha}")
                return 0
            if not review_page.has_previous_page:
                break
            before = review_page.start_cursor
            if before is None:
                break
    except RuntimeError as error:
        print(f"codex review: {error}", file=sys.stderr)
        return 1

    print(
        f"codex review: missing {reviewer_login} review for current head {head_sha}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
