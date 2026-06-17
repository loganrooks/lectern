import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "check_codex_review.py"
SPEC = importlib.util.spec_from_file_location("check_codex_review", MODULE_PATH)
assert SPEC is not None
check_codex_review = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = check_codex_review
SPEC.loader.exec_module(check_codex_review)

Review = check_codex_review.Review
has_required_review = check_codex_review.has_required_review
reviews_from_payload = check_codex_review.reviews_from_payload
raise_for_graphql_errors = check_codex_review.raise_for_graphql_errors


def test_review_on_current_head_passes() -> None:
    reviews = [
        Review("chatgpt-codex-connector", "COMMENTED", "abc123"),
    ]

    assert has_required_review(reviews, "chatgpt-codex-connector", "abc123")


def test_review_on_old_head_fails() -> None:
    reviews = [
        Review("chatgpt-codex-connector", "COMMENTED", "old123"),
    ]

    assert not has_required_review(reviews, "chatgpt-codex-connector", "abc123")


def test_non_codex_review_fails() -> None:
    reviews = [
        Review("loganrooks", "APPROVED", "abc123"),
    ]

    assert not has_required_review(reviews, "chatgpt-codex-connector", "abc123")


def test_payload_parser_handles_review_nodes() -> None:
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviews": {
                        "nodes": [
                            {
                                "author": {"login": "chatgpt-codex-connector"},
                                "state": "COMMENTED",
                                "commit": {"oid": "abc123"},
                            }
                        ]
                    }
                }
            }
        }
    }

    assert reviews_from_payload(payload) == [
        Review("chatgpt-codex-connector", "COMMENTED", "abc123")
    ]


def test_has_required_review_accepts_all_submitted_states() -> None:
    for state in check_codex_review.SUBMITTED_STATES:
        reviews = [Review("chatgpt-codex-connector", state, "abc123")]

        assert has_required_review(reviews, "chatgpt-codex-connector", "abc123")


def test_has_required_review_accepts_bot_suffix() -> None:
    reviews = [Review("chatgpt-codex-connector[bot]", "COMMENTED", "abc123")]

    assert has_required_review(reviews, "chatgpt-codex-connector", "abc123")


def test_has_required_review_rejects_unsubmitted_states() -> None:
    for state in ["DISMISSED", "PENDING"]:
        reviews = [Review("chatgpt-codex-connector", state, "abc123")]

        assert not has_required_review(reviews, "chatgpt-codex-connector", "abc123")


def test_reviews_from_payload_accepts_empty_payload() -> None:
    assert reviews_from_payload({}) == []


def test_graphql_errors_are_reported_distinctly() -> None:
    with pytest.raises(RuntimeError, match="rate limit"):
        raise_for_graphql_errors({"errors": [{"message": "rate limit"}]})


def test_main_queries_most_recent_reviews(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_query = ""

    def fake_graphql_request(token: str, query: str, variables: dict[str, object]) -> object:
        nonlocal captured_query
        captured_query = query
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviews": {
                            "nodes": [
                                {
                                    "author": {"login": "chatgpt-codex-connector"},
                                    "state": "COMMENTED",
                                    "commit": {"oid": "abc123"},
                                }
                            ]
                        }
                    }
                }
            }
        }

    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "loganrooks/lectern")
    monkeypatch.setenv("PR_NUMBER", "2")
    monkeypatch.setenv("HEAD_SHA", "abc123")
    monkeypatch.setattr(check_codex_review, "graphql_request", fake_graphql_request)

    assert check_codex_review.main() == 0
    assert "reviews(last: 100, before: $before)" in captured_query


def test_main_pages_through_older_reviews(monkeypatch: pytest.MonkeyPatch) -> None:
    before_values: list[object] = []

    def fake_graphql_request(token: str, query: str, variables: dict[str, object]) -> object:
        before_values.append(variables["before"])
        if variables["before"] is None:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviews": {
                                "pageInfo": {
                                    "hasPreviousPage": True,
                                    "startCursor": "cursor-1",
                                },
                                "nodes": [
                                    {
                                        "author": {"login": "loganrooks"},
                                        "state": "COMMENTED",
                                        "commit": {"oid": "abc123"},
                                    }
                                ],
                            }
                        }
                    }
                }
            }
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviews": {
                            "pageInfo": {
                                "hasPreviousPage": False,
                                "startCursor": "cursor-0",
                            },
                            "nodes": [
                                {
                                    "author": {"login": "chatgpt-codex-connector"},
                                    "state": "COMMENTED",
                                    "commit": {"oid": "abc123"},
                                }
                            ],
                        }
                    }
                }
            }
        }

    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "loganrooks/lectern")
    monkeypatch.setenv("PR_NUMBER", "2")
    monkeypatch.setenv("HEAD_SHA", "abc123")
    monkeypatch.setattr(check_codex_review, "graphql_request", fake_graphql_request)

    assert check_codex_review.main() == 0
    assert before_values == [None, "cursor-1"]
