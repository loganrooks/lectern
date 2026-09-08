"""A search-latency datum, recorded with its conditions and asserted against nothing.

The contract asks for a latency datum at O(10^3). It does not ask for a
performance guarantee, and the difference matters: a threshold assertion would
convert a measurement into a claim the milestone has not earned, and would then
fail on whatever machine happened to be slower rather than on any regression in
Lectern.

So this test asserts that the datum exists and carries the conditions needed to
interpret it. The number itself is reported, never bounded.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from lectern import search
from lectern.automation import open_state

CORPUS_BUNDLES = 1000
CORPUS_SEGMENTS = CORPUS_BUNDLES
DATUM_PATH = Path(__file__).resolve().parents[1] / "docs" / "benchmarks" / "m5a-search-latency.json"


def test_latency_datum_is_durably_recorded() -> None:
    datum: dict[str, Any] = json.loads(DATUM_PATH.read_text(encoding="utf-8"))
    assert datum["measurement"] == "library search latency"
    assert float(datum["elapsed_ms"]) >= 0.0
    assert int(datum["corpus_bundles"]) >= 1000
    assert int(datum["corpus_segments"]) >= 1000
    assert datum["conditions"]
    assert datum["claim_limit"]
    assert int(datum["canon_version"]) == search.CANON_VERSION
    assert int(datum["segmenter_version"]) == search.SEGMENTER_VERSION
    reconciliation = datum["open_reconcile_search_measurement"]
    assert int(reconciliation["corpus_bundles"]) == 1000
    assert int(reconciliation["total_segments_bytes"]) >= 100_000_000
    assert float(reconciliation["median_elapsed_ms"]) >= 0.0
    assert reconciliation["conditions"]


def test_latency_measurement_reports_its_conditions(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    with open_state(state_path) as state:
        # The corpus is built by writing index rows directly rather than by
        # scanning a folder. A folder scan would drag in the two redundant
        # `realpath` calls per entry that the RS containment fix added, and the
        # datum would then measure the scanner as much as the search.
        for index in range(CORPUS_SEGMENTS):
            state.index_synthetic_segment(
                f"bundle-{index:04d}",
                0,
                f"synthetic transcript line {index} about phenomenology and experience",
            )
        assert state.indexed_segment_count() == CORPUS_SEGMENTS

        started = time.perf_counter()
        hits = state.search_segments("phenomenology")
        elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert hits, "the corpus must actually contain the queried term"

    datum: dict[str, Any] = {
        "measurement": "library search latency",
        "elapsed_ms": round(elapsed_ms, 3),
        "corpus_bundles": CORPUS_BUNDLES,
        "corpus_segments": CORPUS_SEGMENTS,
        "query": "phenomenology",
        "hits": len(hits),
        "canon_version": search.CANON_VERSION,
        "segmenter_version": search.SEGMENTER_VERSION,
        "conditions": (
            "single process, warm SQLite page cache, temporary filesystem, corpus built by "
            "direct index writes rather than folder scan, unspecified hardware"
        ),
        "claim_limit": (
            "A datum for this corpus on this machine under these conditions. Not a performance "
            "guarantee, not a bound, and not comparable across machines."
        ),
    }
    # What is asserted: the datum is complete enough to interpret later. What is
    # deliberately not asserted: how large the number is.
    assert float(datum["elapsed_ms"]) >= 0.0
    assert int(datum["corpus_bundles"]) >= 1000
    assert int(datum["corpus_segments"]) >= 1000
    assert datum["conditions"]
    assert datum["claim_limit"]


def test_search_returns_from_a_thousand_segment_corpus(tmp_path: Path) -> None:
    """Correctness at scale, kept separate from the timing measurement.

    Folding these together would make a correctness failure look like a
    performance result.
    """

    state_path = tmp_path / "state.sqlite"
    with open_state(state_path) as state:
        for index in range(CORPUS_SEGMENTS):
            state.index_synthetic_segment(
                f"bundle-{index:04d}", 0, f"line {index} distinctive-marker-{index}"
            )
        hits = state.search_segments("distinctive-marker-777")
    assert len(hits) == 1
    assert hits[0].bundle_id == "bundle-0777"
