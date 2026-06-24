from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from lectern import __version__
from lectern.automation import (
    DEFAULT_STATE_PATH,
    DEFAULT_YOUTUBE_API_KEY_ENV,
    AutomationError,
    QueueState,
    SourceKind,
    SourcePolicy,
    YouTubePlaylistAdapter,
    open_state,
    preflight_local_folder,
    preflight_state_store,
    preflight_youtube_playlist,
)
from lectern.bundle import export_json_schema
from lectern.ingest import IngestError

USAGE = """usage: lectern [--version] <command>

commands:
  doctor         check required local tools and state-store access
  ingest         ingest local media into a bundle
  library        list or show ingested bundles from local state
  queue          inspect and update discovery queue items
  schema export  print or write the manifest JSON Schema
  sources        manage local source registry and scans
"""


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
    state = preflight_state_store(DEFAULT_STATE_PATH)
    if state.ok:
        print(f"state: OK ({state.path})")
    else:
        detail = state.error or "state path is not writable"
        print(f"state: ERROR ({detail})")
        ok = False
    youtube_status = (
        f"configured via {DEFAULT_YOUTUBE_API_KEY_ENV}"
        if os.environ.get(DEFAULT_YOUTUBE_API_KEY_ENV)
        else f"not configured; set {DEFAULT_YOUTUBE_API_KEY_ENV} for YouTube discovery"
    )
    print(f"youtube: OPTIONAL ({youtube_status})")
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
        print(
            "usage: lectern ingest SOURCE [--output DIR] [--state PATH] "
            "[--transcriber-command COMMAND] [--json]",
            file=sys.stderr,
        )
        return 2

    source = Path(args[0])
    output_root = Path("bundles")
    state_path = DEFAULT_STATE_PATH
    json_output = False
    transcriber_command: str | None = None
    rest = list(args[1:])
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--output" and index + 1 < len(rest):
            output_root = Path(rest[index + 1])
            index += 2
        elif token == "--state" and index + 1 < len(rest):
            state_path = Path(rest[index + 1])
            index += 2
        elif token == "--transcriber-command" and index + 1 < len(rest):
            transcriber_command = rest[index + 1]
            index += 2
        elif token == "--json":
            json_output = True
            index += 1
        else:
            print(
                "usage: lectern ingest SOURCE [--output DIR] [--state PATH] "
                "[--transcriber-command COMMAND] [--json]",
                file=sys.stderr,
            )
            return 2

    try:
        with open_state(state_path) as state:
            result = state.ingest_one_shot(
                source,
                output_root,
                transcriber_command=transcriber_command,
            )
    except IngestError as exc:
        print(f"ingest: {exc}", file=sys.stderr)
        return 1
    except AutomationError as exc:
        print(f"ingest: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"ingest: {exc}", file=sys.stderr)
        return 1

    if json_output:
        _print_json(
            {
                "bundle_id": result.manifest.bundle_id,
                "bundle_dir": str(result.bundle_dir),
                "state_path": str(state_path),
            }
        )
    else:
        print(result.bundle_dir)
    return 0


def _sources(args: Sequence[str]) -> int:
    if not args:
        _sources_usage()
        return 2
    command = args[0]
    rest, state_path, json_output = _parse_common(args[1:])
    try:
        if command == "add-folder":
            return _sources_add_folder(rest, state_path, json_output)
        if command == "add-youtube-playlist":
            return _sources_add_youtube_playlist(rest, state_path, json_output)
        if command == "list":
            return _sources_list(rest, state_path, json_output)
        if command == "scan":
            return _sources_scan(rest, state_path, json_output)
        if command == "preflight":
            return _sources_preflight(rest, json_output)
        if command == "preflight-youtube":
            return _sources_preflight_youtube(rest, json_output)
    except AutomationError as exc:
        print(f"sources: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"sources: {exc}", file=sys.stderr)
        return 1
    _sources_usage()
    return 2


def _sources_add_folder(args: Sequence[str], state_path: Path, json_output: bool) -> int:
    if len(args) not in (2, 4):
        _sources_usage()
        return 2
    name = args[0]
    root = Path(args[1])
    policy = SourcePolicy.REVIEW
    if len(args) == 4:
        if args[2] != "--policy":
            _sources_usage()
            return 2
        try:
            policy = SourcePolicy(args[3])
        except ValueError:
            print("sources: policy must be disabled, scan-only, or review", file=sys.stderr)
            return 2
    with open_state(state_path) as state:
        source = state.add_local_folder_source(name, root, policy)
    if json_output:
        _print_json(source.to_dict())
    else:
        print(f"{source.id}\t{source.policy.value}\t{source.root_path}")
    return 0


def _sources_add_youtube_playlist(args: Sequence[str], state_path: Path, json_output: bool) -> int:
    if len(args) not in (2, 4):
        _sources_usage()
        return 2
    name = args[0]
    playlist = args[1]
    policy = SourcePolicy.REVIEW
    if len(args) == 4:
        if args[2] != "--policy":
            _sources_usage()
            return 2
        try:
            policy = SourcePolicy(args[3])
        except ValueError:
            print("sources: policy must be disabled, scan-only, or review", file=sys.stderr)
            return 2
    with open_state(state_path) as state:
        source = state.add_youtube_playlist_source(name, playlist, policy)
    if json_output:
        _print_json(source.to_dict())
    else:
        print(f"{source.id}\t{source.policy.value}\t{source.root_path}")
    return 0


def _sources_list(args: Sequence[str], state_path: Path, json_output: bool) -> int:
    if args:
        _sources_usage()
        return 2
    with open_state(state_path) as state:
        sources = state.list_sources()
    if json_output:
        _print_json({"sources": [source.to_dict() for source in sources]})
    else:
        for source in sources:
            print(f"{source.id}\t{source.name}\t{source.policy.value}\t{source.root_path}")
    return 0


def _sources_scan(args: Sequence[str], state_path: Path, json_output: bool) -> int:
    if len(args) not in (1, 3):
        _sources_usage()
        return 2
    api_key_env = DEFAULT_YOUTUBE_API_KEY_ENV
    if len(args) == 3:
        if args[1] != "--api-key-env":
            _sources_usage()
            return 2
        api_key_env = args[2]
    with open_state(state_path) as state:
        source = state.get_source(args[0])
        adapter = (
            YouTubePlaylistAdapter.from_environment(api_key_env=api_key_env)
            if source.kind is SourceKind.YOUTUBE_PLAYLIST
            else None
        )
        delta = state.scan_source(args[0], adapter=adapter)
    payload = delta.to_dict()
    if json_output:
        _print_json(payload)
    else:
        counts = payload["counts"]
        print(
            f"added={counts['added']} changed={counts['changed']} "
            f"removed={counts['removed']} unchanged={counts['unchanged']} queued={counts['queued']}"
        )
    return 0


def _sources_preflight(args: Sequence[str], json_output: bool) -> int:
    if len(args) != 1:
        _sources_usage()
        return 2
    preflight = preflight_local_folder(Path(args[0]))
    if json_output:
        _print_json(preflight.to_dict())
    else:
        status = "OK" if preflight.ok else "FAIL"
        print(f"{status}\tmedia_files={preflight.media_files}\t{preflight.path}")
    return 0 if preflight.ok else 1


def _sources_preflight_youtube(args: Sequence[str], json_output: bool) -> int:
    if len(args) not in (1, 3):
        _sources_usage()
        return 2
    api_key_env = DEFAULT_YOUTUBE_API_KEY_ENV
    if len(args) == 3:
        if args[1] != "--api-key-env":
            _sources_usage()
            return 2
        api_key_env = args[2]
    preflight = preflight_youtube_playlist(args[0], api_key_env=api_key_env)
    if json_output:
        _print_json(preflight.to_dict())
    else:
        status = "OK" if preflight.ok else "FAIL"
        detail = preflight.error or f"estimated_units_consumed={preflight.estimated_units_consumed}"
        print(f"{status}\tplaylist={preflight.playlist_id}\t{detail}")
    return 0 if preflight.ok else 1


def _queue(args: Sequence[str]) -> int:
    if not args:
        _queue_usage()
        return 2
    command = args[0]
    rest, state_path, json_output = _parse_common(args[1:])
    try:
        if command == "list":
            return _queue_list(rest, state_path, json_output)
        if command == "show":
            return _queue_show(rest, state_path, json_output)
        if command == "approve":
            return _queue_transition(rest, state_path, json_output, command)
        if command == "skip":
            return _queue_transition(rest, state_path, json_output, command)
        if command == "retry":
            return _queue_transition(rest, state_path, json_output, command)
        if command == "ingest":
            return _queue_ingest(rest, state_path, json_output)
    except AutomationError as exc:
        print(f"queue: {exc}", file=sys.stderr)
        return 3
    except IngestError as exc:
        print(f"queue: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"queue: {exc}", file=sys.stderr)
        return 1
    _queue_usage()
    return 2


def _queue_list(args: Sequence[str], state_path: Path, json_output: bool) -> int:
    if len(args) not in (0, 2):
        _queue_usage()
        return 2
    state_filter: QueueState | None = None
    if args:
        if args[0] != "--queue-state":
            _queue_usage()
            return 2
        try:
            state_filter = QueueState(args[1])
        except ValueError:
            print("queue: unknown state", file=sys.stderr)
            return 2
    with open_state(state_path) as state:
        items = state.list_queue(state_filter)
    if json_output:
        _print_json({"queue": [item.to_dict() for item in items]})
    else:
        for item in items:
            print(f"{item.id}\t{item.state.value}\t{item.source_item_id}")
    return 0


def _queue_show(args: Sequence[str], state_path: Path, json_output: bool) -> int:
    if len(args) != 1:
        _queue_usage()
        return 2
    with open_state(state_path) as state:
        item = state.get_queue_item(args[0])
    if json_output:
        _print_json(item.to_dict())
    else:
        print(f"{item.id}\t{item.state.value}\t{item.source_item_id}")
    return 0


def _queue_transition(
    args: Sequence[str],
    state_path: Path,
    json_output: bool,
    command: str,
) -> int:
    if len(args) != 1:
        _queue_usage()
        return 2
    with open_state(state_path) as state:
        if command == "approve":
            item = state.approve_queue_item(args[0])
        elif command == "skip":
            item = state.skip_queue_item(args[0])
        else:
            item = state.retry_queue_item(args[0])
    if json_output:
        _print_json(item.to_dict())
    else:
        print(f"{item.id}\t{item.state.value}")
    return 0


def _queue_ingest(args: Sequence[str], state_path: Path, json_output: bool) -> int:
    if not args:
        _queue_usage()
        return 2
    queue_item_id = args[0]
    output_root = Path("bundles")
    transcriber_command: str | None = None
    rest = list(args[1:])
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--output" and index + 1 < len(rest):
            output_root = Path(rest[index + 1])
            index += 2
        elif token == "--transcriber-command" and index + 1 < len(rest):
            transcriber_command = rest[index + 1]
            index += 2
        else:
            _queue_usage()
            return 2
    with open_state(state_path) as state:
        result = state.ingest_queue_item(
            queue_item_id,
            output_root,
            transcriber_command=transcriber_command,
        )
    if json_output:
        _print_json(
            {
                "bundle_id": result.manifest.bundle_id,
                "bundle_dir": str(result.bundle_dir),
                "queue_item_id": queue_item_id,
            }
        )
    else:
        print(result.bundle_dir)
    return 0


def _library(args: Sequence[str]) -> int:
    if not args:
        _library_usage()
        return 2
    command = args[0]
    rest, state_path, json_output = _parse_common(args[1:])
    try:
        if command == "list":
            return _library_list(rest, state_path, json_output)
        if command == "show":
            return _library_show(rest, state_path, json_output)
    except AutomationError as exc:
        print(f"library: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"library: {exc}", file=sys.stderr)
        return 1
    _library_usage()
    return 2


def _library_list(args: Sequence[str], state_path: Path, json_output: bool) -> int:
    if args:
        _library_usage()
        return 2
    with open_state(state_path) as state:
        bundles = state.list_library()
    if json_output:
        _print_json({"bundles": [bundle.to_dict() for bundle in bundles]})
    else:
        for bundle in bundles:
            print(f"{bundle.bundle_id}\t{bundle.bundle_path}")
    return 0


def _library_show(args: Sequence[str], state_path: Path, json_output: bool) -> int:
    if len(args) != 1:
        _library_usage()
        return 2
    with open_state(state_path) as state:
        bundle = state.get_library_bundle(args[0])
    manifest = {}
    manifest_path = Path(bundle.bundle_path) / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = {"bundle": bundle.to_dict(), "manifest": manifest}
    if json_output:
        _print_json(payload)
    else:
        print(f"{bundle.bundle_id}\t{bundle.bundle_path}")
    return 0


def _parse_common(args: Sequence[str]) -> tuple[list[str], Path, bool]:
    state_path = DEFAULT_STATE_PATH
    json_output = False
    rest: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--state" and index + 1 < len(args):
            state_path = Path(args[index + 1])
            index += 2
        elif token == "--json":
            json_output = True
            index += 1
        else:
            rest.append(token)
            index += 1
    return rest, state_path, json_output


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _sources_usage() -> None:
    print(
        "usage: lectern sources "
        "{add-folder NAME PATH [--policy POLICY]|"
        "add-youtube-playlist NAME PLAYLIST [--policy POLICY]|list|"
        "scan SOURCE [--api-key-env ENV]|preflight PATH|"
        "preflight-youtube PLAYLIST [--api-key-env ENV]} "
        "[--state PATH] [--json]",
        file=sys.stderr,
    )


def _queue_usage() -> None:
    print(
        "usage: lectern queue "
        "{list [--queue-state STATE]|show ITEM|approve ITEM|skip ITEM|retry ITEM|"
        "ingest ITEM [--output DIR] [--transcriber-command COMMAND]} "
        "[--state PATH] [--json]",
        file=sys.stderr,
    )


def _library_usage() -> None:
    print("usage: lectern library {list|show BUNDLE_ID} [--state PATH] [--json]", file=sys.stderr)


def _usage() -> None:
    print(USAGE, file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args == ["--version"]:
            print(f"lectern {__version__}")
            return 0
        if args == ["doctor"]:
            return _doctor()
        if args and args[0] == "ingest":
            return _ingest(args[1:])
        if args and args[0] == "library":
            return _library(args[1:])
        if args and args[0] == "queue":
            return _queue(args[1:])
        if len(args) >= 2 and args[0] == "schema" and args[1] == "export":
            return _export_schema(args[2:])
        if args and args[0] == "sources":
            return _sources(args[1:])
    except sqlite3.Error as exc:
        print(f"state: {exc}", file=sys.stderr)
        return 3

    _usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
