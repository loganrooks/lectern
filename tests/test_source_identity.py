"""A bundle records *what* its media was, never *where* it sat.

LW-11, ratified 2026-08-01. A bundle is a durable artifact that outlives the
filesystem it was made on: an absolute path in it carries the account name on
macOS, and it dangles the moment the bundle is copied anywhere else. Content
identity survives both.

The assertion that matters here is `test_no_bundle_file_contains_an_absolute_path`,
which scans every artifact rather than the fields anyone thought to name. An
earlier inventory of this leak listed three fields and missed two, one of them
under a key called `backend.path` -- which no rule phrased as "no absolute media
path" would lead a reviewer to check.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from lectern import cli
from lectern.bundle import Manifest

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC_TALK = FIXTURE_DIR / "synthetic_talk.wav"
SYNTHETIC_TRANSCRIPT = FIXTURE_DIR / "synthetic_talk.transcript.txt"

CONTENT_REF = re.compile(r"^sha256:[0-9a-f]{64}$")


def _ingest(tmp_path: Path) -> Path:
    """Ingest the synthetic fixture through an absolute path, as a user would."""

    media_dir = tmp_path / "Recordings"
    media_dir.mkdir(parents=True, exist_ok=True)
    media = media_dir / "synthetic_talk.wav"
    media.write_bytes(SYNTHETIC_TALK.read_bytes())
    media.with_suffix(".transcript.txt").write_text(
        SYNTHETIC_TRANSCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    out = tmp_path / "bundles"
    assert (
        cli.main(["ingest", str(media), "--output", str(out), "--state", str(tmp_path / "s.db")])
        == 0
    )
    bundles = [path for path in out.iterdir() if path.is_dir()]
    assert len(bundles) == 1
    return bundles[0]


def test_local_source_ref_is_content_identity(tmp_path: Path) -> None:
    bundle = _ingest(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert CONTENT_REF.match(manifest["source"]["ref"]), manifest["source"]["ref"]


def test_source_bytes_recorded(tmp_path: Path) -> None:
    # Size travels with the digest because identity alone cannot tell a caller
    # whether the referenced media is a 30-second clip or a three-hour lecture.
    bundle = _ingest(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"]["bytes"] == SYNTHETIC_TALK.stat().st_size


def test_no_bundle_file_contains_an_absolute_path(tmp_path: Path) -> None:
    """The whole-artifact scan, not a field checklist.

    Written as a sweep on purpose: the failure this guards against is not "a
    known field still holds a path", it is "a path is somewhere nobody looked".
    """

    bundle = _ingest(tmp_path)
    offenders: list[tuple[str, str]] = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.suffix not in {".json", ".md"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r'"([^"]*(?:/Users/|/private/|/tmp/|/var/)[^"]*)"', text):
            offenders.append((path.relative_to(bundle).as_posix(), match.group(1)))
    assert offenders == []


def test_sidecar_and_backend_paths_are_gone(tmp_path: Path) -> None:
    """The two fields the upstream finding missed, named individually.

    Kept alongside the sweep rather than folded into it: the sweep proves no
    path is present in *this* run's artifacts, while these assert that the
    specific keys which used to carry one no longer do, which is what a future
    reader needs in order to understand why the shape changed.
    """

    bundle = _ingest(tmp_path)
    source_json: dict[str, Any] = json.loads((bundle / "source.json").read_text(encoding="utf-8"))
    metadata: dict[str, Any] = json.loads(
        (bundle / "transcript" / "metadata.json").read_text(encoding="utf-8")
    )
    sidecar: dict[str, Any] = source_json["transcript_sidecar"]

    assert "path" not in sidecar
    assert "path" not in metadata["backend"]
    assert "path" not in metadata["source_media"]
    # Identity survives where the path was; dropping both would lose the
    # sidecar's contribution to the approval digest.
    assert sidecar["sha256"]
    assert metadata["source_media"]["sha256"]


def test_manifest_load_rejects_an_incompatible_schema_version(tmp_path: Path) -> None:
    """A consumer must be told, not silently handed a shape it cannot read.

    This is what made repurposing `source.ref` dangerous rather than merely
    breaking: the field kept its type, so an old consumer got no structural
    error, and `Manifest.load` accepted any version without complaint. The
    assertion is that the exact legacy version is refused at the settled major
    compatibility boundary.
    """

    bundle = _ingest(tmp_path)
    manifest_path = bundle / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "0.1.0"
    payload["source"]["ref"] = "/tmp/legacy.wav"
    payload["source"].pop("bytes", None)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported bundle schema version"):
        Manifest.load(bundle)
