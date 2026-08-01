"""Text canonicalization and script segmentation for local retrieval.

A leaf: it depends on nothing else in the package, because the two things it
exports have to be reachable from both the index writer and the query parser
without either importing the other. If those two paths ever disagree about how
text becomes tokens, retrieval returns nothing and raises no error -- the
archive simply looks empty. That silence is why this module is small, pure, and
versioned rather than convenient.

Two versioned rules live here:

`CANON_VERSION` governs `canonical_text` and the digests taken over it. Anchors
carry the version they were made under, so changing the rule re-anchors
citations instead of silently invalidating every one of them.

`SEGMENTER_VERSION` governs `segment_text`. It is persisted beside the index so
a mismatch forces a rebuild; an index built under one rule and queried under
another is the failure this module exists to prevent.
"""

from __future__ import annotations

import hashlib
import unicodedata

CANON_VERSION = 1
SEGMENTER_VERSION = 1

# Code-point ranges written one per script rather than as a single "CJK" range,
# because the shorthand is what caused the mistake this table fixes: kana and
# Hangul syllables are not ideographs, so a rule written for ideographs silently
# served Chinese alone while the user-facing scope claimed Japanese and Korean
# too. Measured before and after: han-only segmentation gave Chinese a hit and
# Japanese and Korean a miss.
_UNSEGMENTED_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),  # CJK unified ideographs extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0xAC00, 0xD7A3),  # Hangul syllables
)


def is_unsegmented_script(character: str) -> bool:
    """Whether a character belongs to a script written without word spaces.

    Public because it is the whole of the segmentation rule, and a rule that is
    not inspectable is a rule nobody can check against the scope Lectern
    publishes.
    """

    code_point = ord(character)
    return any(low <= code_point <= high for low, high in _UNSEGMENTED_RANGES)


def canonical_text(text: str) -> str:
    """Normalize text so that visually identical content compares equal.

    NFC first: macOS filesystems and some editors hand back decomposed forms, so
    `café` written once can arrive as two different byte sequences. Digesting
    the raw text would make a citation break when nothing a reader can see has
    changed.

    Then whitespace is collapsed and stripped, because a re-wrap or a stray
    double space is likewise not a change in what was said.
    """

    return " ".join(unicodedata.normalize("NFC", text).split())


def text_digest(text: str) -> str:
    """The digest an anchor stores, taken over canonical text under CANON_VERSION."""

    return hashlib.sha256(canonical_text(text).encode("utf-8")).hexdigest()


def segment_text(text: str) -> str:
    """Space-separate characters of scripts that are written without word spaces.

    SQLite's `unicode61` tokenizer splits on whitespace and punctuation, which
    means an unsegmented run becomes a single token: the fourteen-character
    Chinese sentence measured during design produced exactly one term, so every
    query shorter than the whole sentence missed. Separating each character
    turns the run into per-character tokens, and a multi-character query becomes
    a phrase over them -- which is what makes a two-character word retrievable,
    the case neither `unicode61` nor `trigram` handles.

    The cost, stated because it is real and not a rounding error: precision
    inside these scripts drops, since a query can match characters that are
    adjacent across a word boundary. For an archive of one's own recordings,
    recall is the right side to err on -- someone is looking for something they
    know is there. It would be the wrong side for a ranked public search.

    Pure by construction: no configuration, no globals, no environment. The
    invariant that matters is that identical input yields identical output
    everywhere and always, so that the index and the query agree.
    """

    pieces: list[str] = []
    for character in canonical_text(text):
        if is_unsegmented_script(character):
            pieces.append(f" {character} ")
        else:
            pieces.append(character)
    return " ".join("".join(pieces).split())


def index_signature() -> dict[str, int]:
    """The versions an index must be rebuilt on when they change.

    Persisted beside the index rather than assumed, because "the code that
    queried is the code that indexed" is true right up until someone upgrades.
    """

    return {"canon_version": CANON_VERSION, "segmenter_version": SEGMENTER_VERSION}
