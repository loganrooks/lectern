"""CLI entry point."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from lectern import __version__
from lectern.bundle import export_json_schema


def _doctor() -> int:
    ok = True
    print(f"lectern: OK ({__version__})")
    print(
        f"python: OK ({sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro})"
    )

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


def _usage() -> None:
    print(
        "usage: lectern [--version] <command>\n"
        "\n"
        "commands:\n"
        "  doctor         check required local tools\n"
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
    if len(args) >= 2 and args[0] == "schema" and args[1] == "export":
        return _export_schema(args[2:])
    _usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
