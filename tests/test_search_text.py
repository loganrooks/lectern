"""Canonicalization and segmentation: the two rules retrieval silently depends on.

If the index and the query ever disagree about how text becomes tokens, search
returns nothing and raises nothing. The design's first revision guarded that
with an object-identity test -- assert both paths call the same function object
-- which checks *which function was imported* rather than *what transformation
ran*. A single callable reading mutable configuration passes that test and still
diverges.

So the property asserted here is behavioral: the transformation is pure, and the
tokens it produces match hand-authored goldens for every script the published
scope names.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from lectern import search

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "multiscript_segments.json"
CASES: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
IDS = [case["script"] for case in CASES]


def fts5_terms(text: str) -> set[str]:
    """The terms SQLite actually indexes for `text`, read back via fts5vocab.

    Goes through the real tokenizer rather than approximating it, because the
    thing under test is agreement with SQLite, not agreement with a
    reimplementation of SQLite.
    """

    connection = sqlite3.connect(":memory:")
    connection.execute("create virtual table d using fts5(body, tokenize='unicode61')")
    connection.execute("create virtual table v using fts5vocab(d, 'row')")
    connection.execute("insert into d values (?)", (text,))
    return {row[0] for row in connection.execute("select term from v")}


def test_canonicalization_makes_nfc_and_nfd_equal() -> None:
    # The failure this prevents is a citation breaking because an editor saved
    # the file, with nothing a reader can see having changed.
    assert search.text_digest("café") == search.text_digest("café")


def test_canonicalization_collapses_whitespace() -> None:
    assert search.text_digest("alpha  beta") == search.text_digest("alpha beta")
    assert search.text_digest("  alpha beta  ") == search.text_digest("alpha beta")


def test_canonicalization_still_distinguishes_different_text() -> None:
    # A normalizer that collapses too much would make every anchor resolve,
    # which is the same failure as one that resolves none -- just quieter.
    assert search.text_digest("alpha beta") != search.text_digest("alpha gamma")


def test_canon_version_is_declared() -> None:
    signature = search.index_signature()
    assert signature["canon_version"] == search.CANON_VERSION
    assert signature["segmenter_version"] == search.SEGMENTER_VERSION


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_golden_token_streams(case: dict[str, Any]) -> None:
    assert fts5_terms(search.segment_text(case["text"])) == set(case["expected_terms"])


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_query_is_retrievable_in_its_own_script(case: dict[str, Any]) -> None:
    """The property the golden streams exist to support.

    Includes the two-character CJK queries that no available tokenizer handles
    without segmentation -- the case that made character segmentation the
    choice over `trigram`.
    """

    connection = sqlite3.connect(":memory:")
    connection.execute("create virtual table d using fts5(body, tokenize='unicode61')")
    connection.execute("insert into d values (?)", (search.segment_text(case["text"]),))
    phrase = " ".join(search.segment_text(case["query"]).split())
    matched = connection.execute("select 1 from d where d match ?", (f'"{phrase}"',)).fetchall()
    assert matched, f"{case['script']}: {case['query']!r} did not retrieve its own document"


def test_segmentation_is_pure_under_mutated_environment(monkeypatch: MonkeyPatch) -> None:
    """Purity, which is the invariant object identity was standing in for.

    Same callable, different ambient state: if segmentation consulted an
    environment variable or a module global, this would diverge while an
    identity assertion stayed green.
    """

    sample = "现象学 and Phenomenology テスト 현상학"
    before = search.segment_text(sample)

    monkeypatch.setenv("LECTERN_SEGMENTER_MODE", "disabled")
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setattr(os, "environ", dict(os.environ), raising=False)

    assert search.segment_text(sample) == before
    assert search.segment_text(sample) == search.segment_text(sample)


def test_unsegmented_scripts_are_enumerated_not_guessed() -> None:
    """Kana and Hangul are not ideographs, and the rule must say so explicitly.

    This is the assertion that would have caught a published scope covering
    Japanese and Korean against a rule that separated Han only.
    """

    for character in "现象学":
        assert search.is_unsegmented_script(character)
    for character in "これは":
        assert search.is_unsegmented_script(character)
    for character in "テスト":
        assert search.is_unsegmented_script(character)
    for character in "현상학":
        assert search.is_unsegmented_script(character)
    for character in "Phenomenology Φαινομενολογία الفلسفة":
        assert not search.is_unsegmented_script(character)


def test_segmentation_casefolds_spaced_scripts_without_splitting_words() -> None:
    # Literal casefolding must not introduce boundaries inside spaced words.
    assert search.segment_text("Phenomenology examines experience") == (
        "phenomenology examines experience"
    )
    assert search.segment_text("الفلسفة دراسة الوجود") == "الفلسفة دراسة الوجود"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ꞵeta", "ꞵeta"),
        ("Ϳota", "ϳota"),
        ("Ԩame", "ԩame"),
        ("Straße", "strasse"),
        ("İota", "i\u0307ota"),
        ("ΟΣ", "οσ"),
        ("ﬃ", "ffi"),
        ("現象", "現 象"),
    ],
)
def test_literal_index_and_query_use_the_same_folded_stream(text: str, expected: str) -> None:
    assert search.segment_text(text) == expected
    for query in (text, text.casefold()):
        assert search.literal_match_expression(query) == '"' + expected + '"'
