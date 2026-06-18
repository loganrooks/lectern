from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "check_codex_review.py"
SPEC = importlib.util.spec_from_file_location("check_codex_review", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
check_codex_review = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_codex_review
SPEC.loader.exec_module(check_codex_review)

Review = check_codex_review.Review
ReviewComment = check_codex_review.ReviewComment
comment_reviews_head = check_codex_review.comment_reviews_head
comments_from_payload = check_codex_review.comments_from_payload
has_required_review = check_codex_review.has_required_review
has_required_review_comment = check_codex_review.has_required_review_comment
reviews_from_payload = check_codex_review.reviews_from_payload


def payload(
    review_nodes: list[dict[str, Any]],
    comment_nodes: list[dict[str, Any]] | None = None,
    *,
    has_previous_page: bool = False,
    start_cursor: str | None = "cursor-0",
) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviews": {
                        "pageInfo": {
                            "hasPreviousPage": has_previous_page,
                            "startCursor": start_cursor,
                        },
                        "nodes": review_nodes,
                    },
                    "comments": {"nodes": comment_nodes or []},
                }
            }
        }
    }


def review_node(
    author: str = "chatgpt-codex-connector",
    state: str = "COMMENTED",
    commit_oid: str | None = "abc123",
) -> dict[str, Any]:
    return {
        "author": {"login": author},
        "state": state,
        "commit": {"oid": commit_oid} if commit_oid is not None else None,
    }


def comment_node(
    body: str,
    author: str = "chatgpt-codex-connector[bot]",
) -> dict[str, Any]:
    return {"author": {"login": author}, "body": body}


def clean_review_body(commit: str) -> str:
    return f"Codex Review: Didn't find any major issues.\n\nReviewed commit: `{commit}`"


def test_review_on_current_head_passes() -> None:
    reviews = [Review("chatgpt-codex-connector", "COMMENTED", "abc123")]

    assert has_required_review(reviews, "chatgpt-codex-connector", "abc123")


def test_review_on_old_head_fails() -> None:
    reviews = [Review("chatgpt-codex-connector", "COMMENTED", "old123")]

    assert not has_required_review(reviews, "chatgpt-codex-connector", "abc123")


def test_review_from_bot_suffix_passes() -> None:
    reviews = [Review("chatgpt-codex-connector[bot]", "COMMENTED", "abc123")]

    assert has_required_review(reviews, "chatgpt-codex-connector", "abc123")


def test_changes_requested_does_not_pass() -> None:
    reviews = [Review("chatgpt-codex-connector", "CHANGES_REQUESTED", "abc123")]

    assert not has_required_review(reviews, "chatgpt-codex-connector", "abc123")


def test_clean_review_comment_on_current_head_passes() -> None:
    comments = [ReviewComment("chatgpt-codex-connector[bot]", clean_review_body("abc1234"))]

    assert has_required_review_comment(comments, "chatgpt-codex-connector", "abc123456789")


def test_clean_review_comment_on_old_head_fails() -> None:
    comments = [ReviewComment("chatgpt-codex-connector[bot]", clean_review_body("old1234"))]

    assert not has_required_review_comment(comments, "chatgpt-codex-connector", "abc123456789")


def test_clean_review_comment_requires_connector_author() -> None:
    comments = [ReviewComment("loganrooks", clean_review_body("abc1234"))]

    assert not has_required_review_comment(comments, "chatgpt-codex-connector", "abc123456789")


def test_clean_review_comment_requires_reviewed_commit_line() -> None:
    assert not comment_reviews_head("Codex Review: all clear", "abc123456789")


def test_reviews_from_payload_parses_review_nodes() -> None:
    reviews = reviews_from_payload(payload([review_node()]))

    assert reviews == [Review("chatgpt-codex-connector", "COMMENTED", "abc123")]


def test_comments_from_payload_parses_comment_nodes() -> None:
    comments = comments_from_payload(payload([], [comment_node(clean_review_body("abc1234"))]))

    assert comments == [ReviewComment("chatgpt-codex-connector[bot]", clean_review_body("abc1234"))]


def test_main_accepts_paginated_review(monkeypatch: MonkeyPatch) -> None:
    before_values: list[object] = []

    def fake_graphql_request(token: str, query: str, variables: dict[str, object]) -> object:
        del token, query
        before_values.append(variables["before"])
        if variables["before"] is None:
            return payload([], has_previous_page=True, start_cursor="cursor-1")
        return payload([review_node()])

    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "loganrooks/lectern")
    monkeypatch.setenv("PR_NUMBER", "3")
    monkeypatch.setenv("HEAD_SHA", "abc123")
    monkeypatch.setattr(check_codex_review, "graphql_request", fake_graphql_request)

    assert check_codex_review.main() == 0
    assert before_values == [None, "cursor-1"]


def test_main_accepts_clean_review_comment(monkeypatch: MonkeyPatch) -> None:
    def fake_graphql_request(token: str, query: str, variables: dict[str, object]) -> object:
        del token, query, variables
        return payload([], [comment_node(clean_review_body("abc1234"))])

    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "loganrooks/lectern")
    monkeypatch.setenv("PR_NUMBER", "3")
    monkeypatch.setenv("HEAD_SHA", "abc123456789")
    monkeypatch.setattr(check_codex_review, "graphql_request", fake_graphql_request)

    assert check_codex_review.main() == 0


def test_main_fails_without_current_head_evidence(monkeypatch: MonkeyPatch) -> None:
    def fake_graphql_request(token: str, query: str, variables: dict[str, object]) -> object:
        del token, query, variables
        return payload([], [comment_node(clean_review_body("old1234"))])

    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "loganrooks/lectern")
    monkeypatch.setenv("PR_NUMBER", "3")
    monkeypatch.setenv("HEAD_SHA", "abc123456789")
    monkeypatch.setattr(check_codex_review, "graphql_request", fake_graphql_request)

    assert check_codex_review.main() == 1
