from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from lectern import __version__
from lectern.bundle import export_json_schema
from lectern.ingest import IngestError, ingest_local


def _doctor() -> int:
    ok = True
    print(f"lectern: OK ({__version__})")
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"python: OK ({version})")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ffmpeg: MISSING")
        ok = False
    else:
        print(f"ffmpeg: OK ({ffmpeg})")
    return 0 if ok else 1


def _export_schema(args: Sequence[str]) -> int:
    schema = export_json_schema()
    if not args:
        print(schema, end="")
        return 0
    if len(args) == 2 and args[0] == "--output":
        output = Path(args[1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(schema, encoding="utf-8")
        print(f"wrote {output}")
        return 0
    print("usage: lectern schema export [--output PATH]", file=sys.stderr)
    return 2


def _ingest(args: Sequence[str]) -> int:
    if not args or args[0].startswith("-"):
        print("usage: lectern ingest SOURCE [--output DIR]", file=sys.stderr)
        return 2

    source = Path(args[0])
    output_root = Path("bundles")
    rest = args[1:]
    if rest:
        if len(rest) != 2 or rest[0] != "--output":
            print("usage: lectern ingest SOURCE [--output DIR]", file=sys.stderr)
            return 2
        output_root = Path(rest[1])

    try:
        result = ingest_local(source, output_root)
    except IngestError as exc:
        print(f"ingest: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ingest: {exc}", file=sys.stderr)
        return 1

    print(result.bundle_dir)
    return 0


def _usage() -> None:
    print(
        "usage: lectern [--version] <command>\n\n"
        "commands:\n"
        "  doctor         check required local tools\n"
        "  ingest         ingest local media into a bundle\n"
        "  schema export  print or write the manifest JSON Schema",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(f"lectern {__version__}")
        return 0
    if args == ["doctor"]:
        return _doctor()
    if args and args[0] == "ingest":
        return _ingest(args[1:])
    if len(args) >= 2 and args[0] == "schema" and args[1] == "export":
        return _export_schema(args[2:])

    _usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
