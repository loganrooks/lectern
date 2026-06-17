"""Check that the tracked public tree excludes private/local artifacts."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SELF = Path("tools/public_safety_check.py")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None
    message: str

    def format(self) -> str:
        if self.line is None:
            return f"{self.path}: {self.message}"
        return f"{self.path}:{self.line}: {self.message}"


@dataclass(frozen=True)
class Candidate:
    path: Path
    source: str


FORBIDDEN_BASENAMES = {
    ".DS_Store",
    "AGENTS.md",
    "CLAUDE.md",
}

FORBIDDEN_MEDIA_SUFFIXES = {
    ".aac",
    ".avi",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".wav",
    ".webm",
}

FORBIDDEN_DIR_NAMES = {
    ".mypy_cache",
    ".pytest_cache",
    ".pyright",
    ".ruff_cache",
    "." + "serena",
    ".venv",
    "bundles",
    "review-artifacts",
    "review" + "-journal",
}


def git_paths(*args: str) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", *args],
        check=True,
        capture_output=True,
    )
    names = result.stdout.decode().split("\0")
    return [Path(name) for name in names if name]


def candidate_files() -> list[Candidate]:
    cached_paths = git_paths("--cached")
    modified_paths = git_paths("--modified")
    other_paths = git_paths("--others", "--exclude-standard")
    candidates = [Candidate(path, "index") for path in cached_paths]
    cached = set(cached_paths)
    candidates.extend(Candidate(path, "worktree") for path in modified_paths)
    candidates.extend(Candidate(path, "worktree") for path in other_paths if path not in cached)
    return candidates


def forbidden_path_reason(path: Path) -> str | None:
    text = path.as_posix()
    parts = set(path.parts)
    prefixes = (
        "goal" + "/",
        "bundles/",
        ".venv/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".mypy_cache/",
        ".pyright/",
        "pytest-cache-files-",
        "review-artifacts/",
        "tools/" + "review" + "-journal/",
        "." + "serena/",
    )
    exact = {
        ".review" + "-journal.json",
        ".review" + "-journal.version",
        "gates" + ".toml",
        "docs/GOALFLOW_TRACK.md",
        "docs/ORCHESTRATION.md",
        "docs/REVIEWERS.md",
        "docs/REVIEW" + "_GATES.md",
        "tools/capture_usage.py",
        "tools/gate" + "_check.py",
    }
    if text in exact or path.name in FORBIDDEN_BASENAMES:
        return "forbidden public path"
    if parts & FORBIDDEN_DIR_NAMES or any(part.startswith("pytest-cache-files-") for part in parts):
        return "forbidden public path component"
    if any(text.startswith(prefix) for prefix in prefixes):
        return "forbidden public path prefix"
    if path.suffix.lower() in FORBIDDEN_MEDIA_SUFFIXES:
        return "audio/video media files are not allowed in the public tree"
    return None


def content_patterns() -> list[tuple[re.Pattern[str], str]]:
    private_terms = [
        "GOAL" + "_CONTRACT",
        "goal" + "/",
        "goal" + "flow",
        "claude" + " -p",
        "g2" + "-approved",
        "REVIEW" + "_GATES",
        "gates" + ".toml",
        "tools/gate" + "_check",
        "review" + "-journal",
        "." + "serena",
        "PhD" + " student",
    ]
    secret_patterns = [
        r"ghp_[A-Za-z0-9_]{30,}",
        r"github_pat_[A-Za-z0-9_]+",
        r"sk-[A-Za-z0-9_-]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"AIza[0-9A-Za-z_-]{35}",
        r"xox[baprs]-[0-9A-Za-z-]+",
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    ]
    patterns: list[tuple[re.Pattern[str], str]] = []
    patterns.extend(
        (re.compile(re.escape(term)), "private governance term") for term in private_terms
    )
    patterns.extend((re.compile(pattern), "possible secret") for pattern in secret_patterns)
    return patterns


def read_candidate_text(candidate: Candidate) -> str | None:
    if candidate.source == "index":
        result = subprocess.run(
            ["git", "show", f":{candidate.path.as_posix()}"],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            return None
        data = result.stdout
    else:
        data = candidate.path.read_bytes()

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan_content(
    candidate: Candidate, patterns: list[tuple[re.Pattern[str], str]]
) -> list[Finding]:
    path = candidate.path
    text = read_candidate_text(candidate)
    if text is None:
        return []

    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pattern, message in patterns:
            if pattern.search(line):
                findings.append(Finding(path.as_posix(), line_number, message))
    return findings


def main() -> int:
    files = candidate_files()
    patterns = content_patterns()
    findings: list[Finding] = []

    for candidate in files:
        path = candidate.path
        reason = forbidden_path_reason(path)
        if reason is not None:
            findings.append(Finding(path.as_posix(), None, reason))
        if path.is_file() or candidate.source == "index":
            findings.extend(scan_content(candidate, patterns))

    if findings:
        print("public safety check failed:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding.format()}", file=sys.stderr)
        return 1

    print("public safety check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
