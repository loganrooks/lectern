"""Require Codex connector review evidence on the current PR head."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import cast

SUBMITTED_STATES = {"APPROVED", "COMMENTED"}
REVIEWED_COMMIT_PATTERN = re.compile(r"Reviewed commit:\s*`([0-9a-fA-F]{7,40})`")


@dataclass(frozen=True)
class Review:
    author: str
    state: str
    commit_oid: str | None


@dataclass(frozen=True)
class ReviewComment:
    author: str
    body: str


@dataclass(frozen=True)
class ReviewPage:
    reviews: list[Review]
    comments: list[ReviewComment]
    has_previous_page: bool
    start_cursor: str | None


def as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    raise RuntimeError(f"expected object, got {type(value).__name__}")


def as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return cast(list[object], value)
    raise RuntimeError(f"expected list, got {type(value).__name__}")


def as_str(value: object) -> str:
    if isinstance(value, str):
        return value
    raise RuntimeError(f"expected string, got {type(value).__name__}")


def raise_for_graphql_errors(payload: object) -> None:
    root = as_dict(payload)
    errors = root.get("errors")
    if errors:
        raise RuntimeError(f"GitHub GraphQL errors: {errors}")


def pull_request_from_payload(payload: object) -> dict[str, object]:
    root = as_dict(payload)
    data = as_dict(root.get("data", {}))
    repository = as_dict(data.get("repository", {}))
    return as_dict(repository.get("pullRequest", {}))


def reviews_from_pull_request(pull_request: dict[str, object]) -> list[Review]:
    reviews_payload = as_dict(pull_request.get("reviews", {}))
    nodes = as_list(reviews_payload.get("nodes", []))
    reviews: list[Review] = []
    for node in nodes:
        review = as_dict(node)
        author = as_dict(review.get("author", {}))
        commit = review.get("commit")
        commit_oid = as_str(as_dict(commit).get("oid", "")) if commit is not None else None
        reviews.append(
            Review(
                author=as_str(author.get("login", "")),
                state=as_str(review.get("state", "")),
                commit_oid=commit_oid,
            )
        )
    return reviews


def reviews_from_payload(payload: object) -> list[Review]:
    return reviews_from_pull_request(pull_request_from_payload(payload))


def comments_from_pull_request(pull_request: dict[str, object]) -> list[ReviewComment]:
    comments_payload = as_dict(pull_request.get("comments", {}))
    nodes = as_list(comments_payload.get("nodes", []))
    comments: list[ReviewComment] = []
    for node in nodes:
        comment = as_dict(node)
        author = as_dict(comment.get("author", {}))
        comments.append(
            ReviewComment(
                author=as_str(author.get("login", "")),
                body=as_str(comment.get("body", "")),
            )
        )
    return comments


def comments_from_payload(payload: object) -> list[ReviewComment]:
    return comments_from_pull_request(pull_request_from_payload(payload))


def review_page_from_payload(payload: object) -> ReviewPage:
    pull_request = pull_request_from_payload(payload)
    reviews_payload = as_dict(pull_request.get("reviews", {}))
    page_info = as_dict(reviews_payload.get("pageInfo", {}))
    return ReviewPage(
        reviews=reviews_from_pull_request(pull_request),
        comments=comments_from_pull_request(pull_request),
        has_previous_page=bool(page_info.get("hasPreviousPage")),
        start_cursor=(as_str(page_info["startCursor"]) if page_info.get("startCursor") else None),
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


def comment_reviews_head(body: str, head_sha: str) -> bool:
    head = head_sha.casefold()
    return any(
        head.startswith(match.group(1).casefold())
        for match in REVIEWED_COMMIT_PATTERN.finditer(body)
    )


def has_required_review_comment(
    comments: list[ReviewComment], reviewer_login: str, head_sha: str
) -> bool:
    return any(
        reviewer_login_matches(comment.author, reviewer_login)
        and comment_reviews_head(comment.body, head_sha)
        for comment in comments
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
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
            raise_for_graphql_errors(payload)
            return payload
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub GraphQL request failed: {detail}") from error
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
        print(f"missing required env vars: {', '.join(missing)}", file=sys.stderr)
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
          comments(last: 100) {
            nodes {
              author { login }
              body
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
            if has_required_review_comment(review_page.comments, reviewer_login, head_sha):
                print(f"codex review: found {reviewer_login} clean-review comment for {head_sha}")
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
