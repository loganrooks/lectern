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

`SEGMENTER_VERSION` governs the literal and operator token streams. It is
persisted beside the index so a mismatch forces a rebuild; an index built under
one rule and queried under another is the failure this module exists to prevent.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

CANON_VERSION = 1
SEGMENTER_VERSION = 9

# Code-point ranges written one per script rather than as a single "CJK" range,
# because the shorthand is what caused the mistake this table fixes: kana,
# Hangul jamo, and Hangul syllables are not ideographs, so a rule written for
# ideographs silently served Chinese alone while the user-facing scope claimed
# Japanese and Korean too. Measured before and after: han-only segmentation gave
# Chinese a hit and Japanese and Korean a miss.
_UNSEGMENTED_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),  # CJK unified ideographs extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0x20000, 0x2A6DF),  # CJK unified ideographs extension B
    (0x2A700, 0x2B73F),  # CJK unified ideographs extension C
    (0x2B740, 0x2B81F),  # CJK unified ideographs extension D
    (0x2B820, 0x2CEAF),  # CJK unified ideographs extension E/F
    (0x2CEB0, 0x2EBEF),  # CJK unified ideographs extensions F/G
    (0x2EBF0, 0x2EE5F),  # CJK unified ideographs extension I
    (0x2F800, 0x2FA1F),  # CJK compatibility ideographs supplement
    (0x30000, 0x323AF),  # CJK unified ideographs extensions G/H
    (0x323B0, 0x3347F),  # CJK unified ideographs extension J
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0xFF65, 0xFF9F),  # Half-width Katakana and marks
    (0x1100, 0x11FF),  # Hangul Jamo
    (0x3130, 0x318F),  # Hangul Compatibility Jamo
    (0xA960, 0xA97F),  # Hangul Jamo Extended-A
    (0xAC00, 0xD7A3),  # Hangul syllables
    (0xD7B0, 0xD7FF),  # Hangul Jamo Extended-B
    (0xFFA0, 0xFFDC),  # Half-width Hangul Jamo
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


def operator_segment_text(text: str) -> str:
    """Build the original-only token stream exposed to FTS operator syntax."""

    return _space_unsegmented_scripts(canonical_text(text))


def segment_text(text: str) -> str:
    """Build the canonical-casefolded token stream used for literal candidates.

    Index and query use the same Unicode casefolding, including one-character
    folds that SQLite's tokenizer does not recognize. Characters in scripts
    written without word spaces are separated so shorter phrases remain
    retrievable. Operator syntax uses the separate original-only stream from
    `operator_segment_text`.

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

    return _space_unsegmented_scripts(canonical_text(text).casefold())


def _space_unsegmented_scripts(text: str) -> str:
    pieces: list[str] = []
    for character in text:
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


def literal_match_expression(query: str) -> str:
    """Turn user text into an FTS5 MATCH expression that means exactly itself.

    FTS5's query language is not a superset of ordinary prose: `C++ discussion`
    is a syntax error, `OR` is an operator, `*` is a prefix marker, and an
    unmatched quote is fatal. A person searching their own archive is typing
    what they remember hearing, not composing a query, so the default has to be
    that their text is data.

    Quoting the whole segmented string makes it one phrase, with embedded double
    quotes doubled per FTS5's own escaping rule. Operator mode stays reachable
    for anyone who wants the grammar; it just is not what an unqualified search
    means.
    """

    segmented = segment_text(query)
    escaped = segmented.replace('"', '""')
    return f'"{escaped}"'


class AnchorResolution(StrEnum):
    """What became of the thing a citation pointed at.

    Four states, not two. Collapsing them into resolved/unresolved treats an
    integrity validator as though it were every consumer: a validator should
    reject changed text, a player should navigate to a relocated segment while
    disclosing the move, and a correction interface needs both the prior and the
    current wording. A boolean can express none of that, and its failure mode is
    to report `missing` for content that is still there.
    """

    EXACT = "exact"
    RELOCATED = "relocated"
    AMBIGUOUS = "ambiguous"
    MODIFIED = "modified"
    MISSING = "missing"
    UNSUPPORTED_VERSION = "unsupported-version"


@dataclass(frozen=True)
class Anchor:
    """A citation's durable reference to one moment in one recording.

    `bundle_id` is what makes it injective across an archive rather than within
    a single transcript -- `segment_id` restarts at zero for every bundle, so an
    anchor without it names no particular recording once it is stored anywhere
    outside the response that produced it.

    `start_s` is carried for display and human recognition and is never identity:
    the rendered `[t=MM:SS]` form truncates to whole seconds, so two segments in
    the same second render the same string.
    """

    bundle_id: str
    segment_id: int
    start_s: float
    text_sha256: str
    canon_version: int

    def rendered(self) -> str:
        """The human-facing form. What is shown, never what is stored."""

        total = max(0, int(self.start_s))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"[t={hours:d}:{minutes:02d}:{seconds:02d}]"
        return f"[t={minutes:02d}:{seconds:02d}]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "segment_id": self.segment_id,
            "start_s": self.start_s,
            "text_sha256": self.text_sha256,
            "canon_version": self.canon_version,
        }


@dataclass(frozen=True)
class ResolvedAnchor:
    """The answer to "does this citation still point at what it cited?"."""

    outcome: AnchorResolution
    segment_id: int | None = None
    start_s: float | None = None
    current_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "segment_id": self.segment_id,
            "start_s": self.start_s,
            "current_text": self.current_text,
        }


def make_anchor(bundle_id: str, segment_id: int, start_s: float, text: str) -> Anchor:
    return Anchor(
        bundle_id=bundle_id,
        segment_id=segment_id,
        start_s=start_s,
        text_sha256=text_digest(text),
        canon_version=CANON_VERSION,
    )


def resolve_against_segments(anchor: Anchor, segments: list[dict[str, Any]]) -> ResolvedAnchor:
    """Decide which of the four states an anchor is in, given a transcript.

    Order matters. The identity check comes first at the cited index, then a
    search by digest across the transcript, and only then a report of change or
    absence -- because a deletion that renumbers a later segment leaves the
    cited words present and findable, and answering `missing` there would be a
    confident wrong answer rather than a cautious one.
    """

    if anchor.canon_version != CANON_VERSION:
        # The version is recorded precisely so it can be consulted. Comparing a
        # digest taken under one rule against text canonicalized by another
        # reports unchanged words as modified, which is the failure the field
        # exists to prevent -- so an unsupported version is refused rather than
        # answered wrongly.
        return ResolvedAnchor(outcome=AnchorResolution.UNSUPPORTED_VERSION)

    by_id: dict[int, dict[str, Any]] = {}
    for segment in segments:
        identifier = segment.get("id")
        if isinstance(identifier, int):
            by_id[identifier] = segment

    at_index = by_id.get(anchor.segment_id)
    if at_index is not None and text_digest(str(at_index.get("text", ""))) == anchor.text_sha256:
        return ResolvedAnchor(
            outcome=AnchorResolution.EXACT,
            segment_id=anchor.segment_id,
            start_s=_as_float(at_index.get("start_s")),
            current_text=str(at_index.get("text", "")),
        )

    # Every digest match, not the first. A repeated phrase -- "Thank you", a
    # recurring refrain -- otherwise redirects the citation to a different
    # occurrence and calls it `relocated`, which is a wrong answer wearing a
    # confident label. Fixing "fails when it should resolve" is no improvement if
    # it introduces "resolves to the wrong thing".
    matches = [
        (segment_id, segment)
        for segment_id, segment in sorted(by_id.items())
        if text_digest(str(segment.get("text", ""))) == anchor.text_sha256
    ]
    if len(matches) == 1:
        segment_id, segment = matches[0]
        return ResolvedAnchor(
            outcome=AnchorResolution.RELOCATED,
            segment_id=segment_id,
            start_s=_as_float(segment.get("start_s")),
            current_text=str(segment.get("text", "")),
        )
    if len(matches) > 1:
        # Nearest by timestamp is the best available disambiguation, and it is
        # reported as ambiguous rather than relocated so a consumer can decide
        # whether that guess is good enough for its purpose.
        segment_id, segment = min(
            matches,
            key=lambda item: abs((_as_float(item[1].get("start_s")) or 0.0) - anchor.start_s),
        )
        return ResolvedAnchor(
            outcome=AnchorResolution.AMBIGUOUS,
            segment_id=segment_id,
            start_s=_as_float(segment.get("start_s")),
            current_text=str(segment.get("text", "")),
        )

    if at_index is not None:
        return ResolvedAnchor(
            outcome=AnchorResolution.MODIFIED,
            segment_id=anchor.segment_id,
            start_s=_as_float(at_index.get("start_s")),
            current_text=str(at_index.get("text", "")),
        )

    return ResolvedAnchor(outcome=AnchorResolution.MISSING)


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


class AnchorIssue(StrEnum):
    """Ways a segment fails to name a moment that exists."""

    OUT_OF_BOUNDS = "out-of-bounds"
    OUT_OF_ORDER = "out-of-order"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class SamplerIssue:
    """One located defect. Locating it is the point: an issue nobody can find is
    an issue nobody can fix."""

    kind: AnchorIssue
    bundle_id: str
    segment_id: int | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "bundle_id": self.bundle_id,
            "segment_id": self.segment_id,
            "detail": self.detail,
        }


# A recording's declared duration and its last timestamp legitimately disagree by
# a little: normalization resamples, and a transcriber may round. The tolerance
# exists so the sampler reports transcripts that are wrong rather than transcripts
# that are merely imprecise.
DURATION_TOLERANCE_S = 2.0


def sample_segment_timings(
    bundle_id: str, segments: list[dict[str, Any]], duration_s: float | None
) -> list[SamplerIssue]:
    """Check that each segment names a moment the recording actually contains.

    `duration_s` may be `None`, and absence is not evidence of a bad timestamp:
    reporting issues there would make the sampler loudest on the bundles whose
    provenance is weakest, which is where a real signal would be hardest to see.
    """

    issues: list[SamplerIssue] = []
    previous_start: float | None = None
    for segment in segments:
        segment_id = segment.get("id")
        located = segment_id if isinstance(segment_id, int) else None
        start = _as_float(segment.get("start_s"))
        if start is None:
            continue

        if start < 0:
            issues.append(
                SamplerIssue(
                    kind=AnchorIssue.OUT_OF_BOUNDS,
                    bundle_id=bundle_id,
                    segment_id=located,
                    detail=f"start_s {start} precedes the recording",
                )
            )
        elif duration_s is not None and start > duration_s + DURATION_TOLERANCE_S:
            issues.append(
                SamplerIssue(
                    kind=AnchorIssue.OUT_OF_BOUNDS,
                    bundle_id=bundle_id,
                    segment_id=located,
                    detail=f"start_s {start} exceeds duration {duration_s}",
                )
            )

        end = _as_float(segment.get("end_s"))
        if end is not None and duration_s is not None and end > duration_s + DURATION_TOLERANCE_S:
            issues.append(
                SamplerIssue(
                    kind=AnchorIssue.OUT_OF_BOUNDS,
                    bundle_id=bundle_id,
                    segment_id=located,
                    detail=f"end_s {end} exceeds duration {duration_s}",
                )
            )
        if end is not None and end < start:
            issues.append(
                SamplerIssue(
                    kind=AnchorIssue.OUT_OF_ORDER,
                    bundle_id=bundle_id,
                    segment_id=located,
                    detail=f"end_s {end} precedes start_s {start}",
                )
            )

        if previous_start is not None and start < previous_start:
            issues.append(
                SamplerIssue(
                    kind=AnchorIssue.OUT_OF_ORDER,
                    bundle_id=bundle_id,
                    segment_id=located,
                    detail=f"start_s {start} precedes the previous segment at {previous_start}",
                )
            )
        previous_start = start
    return issues


def text_contains_literal(display_text: str, query: str) -> bool:
    """Whether `display_text` really contains `query`, punctuation included.

    FTS5's `unicode61` discards punctuation from the index and the query alike,
    so a phrase search for `C++ discussion` also matches `C discussion`, and
    `a-b` is indistinguishable from `a b`. The index is the right instrument for
    finding candidates and the wrong one for deciding whether the literal
    promise holds, so candidates are confirmed against the text as written.

    Case and Unicode form are still normalized, because those are not what a
    person means by "exactly".
    """

    return canonical_text(query).casefold() in canonical_text(display_text).casefold()
