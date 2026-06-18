"""Check that the tracked public tree excludes private/local artifacts."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SELF = Path("tools/public_safety_check.py")
PUBLIC_AGENT_GUIDANCE = Path("AGENTS.md")


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
    "CLAUDE.md",
}

FORBIDDEN_AGENT_DOC_BASENAMES = {
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

ALLOWED_SYNTHETIC_MEDIA_FIXTURES = {
    Path("tests/fixtures/synthetic_talk.wav"),
}

ALLOWED_SYNTHETIC_MEDIA_FIXTURE_SHA256 = {
    Path("tests/fixtures/synthetic_talk.wav"): (
        "cc3abf150b1768dfa87f75ae7d7b06308b005cbaf09631e818cdf41c5cadfb2e"
    ),
}

FORBIDDEN_DIR_NAMES = {
    ".lectern",
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

LOCAL_ONLY_PATHS = (
    Path(".lectern"),
    Path("goal"),
    Path("." + "serena"),
    Path("bundles"),
    Path("review-artifacts"),
    Path("tools") / ("review" + "-journal"),
    Path("." + "review" + "-journal.json"),
    Path("." + "review" + "-journal.version"),
)


def git_paths(*args: str) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", *args],
        check=True,
        capture_output=True,
    )
    names = result.stdout.decode().split("\0")
    return [Path(name) for name in names if name]


def git_reports_ignored(path: Path) -> bool:
    probe = path if path.suffix else path / ".public-safety-probe"
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", probe.as_posix()],
        check=False,
    )
    return result.returncode == 0


def tracked_paths_under(path: Path) -> list[Path]:
    return git_paths("--cached", "--", path.as_posix())


def path_exists(path: Path) -> bool:
    return path.exists()


def candidate_files() -> list[Candidate]:
    cached_paths = git_paths("--cached")
    modified_paths = git_paths("--modified")
    other_paths = git_paths("--others", "--exclude-standard")
    candidates = [Candidate(path, "index") for path in cached_paths]
    cached = set(cached_paths)
    candidates.extend(Candidate(path, "worktree") for path in modified_paths)
    candidates.extend(Candidate(path, "worktree") for path in other_paths if path not in cached)
    return candidates


def local_only_boundary_findings() -> list[Finding]:
    findings: list[Finding] = []
    for path in LOCAL_ONLY_PATHS:
        if tracked_paths_under(path):
            findings.append(
                Finding(
                    path.as_posix(),
                    None,
                    "local-only path is tracked in the public repository",
                )
            )
        if path_exists(path) and not git_reports_ignored(path):
            findings.append(
                Finding(
                    path.as_posix(),
                    None,
                    "local-only path exists but is not ignored/excluded",
                )
            )
    return findings


def forbidden_path_reason(path: Path) -> str | None:
    if path == PUBLIC_AGENT_GUIDANCE:
        return None

    text = path.as_posix()
    parts = set(path.parts)
    normalized_name = path.name.casefold()
    normalized_parts = {part.casefold() for part in path.parts}
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
    if (
        text in exact
        or path.name in FORBIDDEN_BASENAMES
        or normalized_name in {name.casefold() for name in FORBIDDEN_AGENT_DOC_BASENAMES}
    ):
        return "forbidden public path"
    if normalized_parts & {name.casefold() for name in FORBIDDEN_DIR_NAMES} or any(
        part.startswith("pytest-cache-files-") for part in parts
    ):
        return "forbidden public path component"
    if any(text.startswith(prefix) for prefix in prefixes):
        return "forbidden public path prefix"
    if (
        path.suffix.lower() in FORBIDDEN_MEDIA_SUFFIXES
        and path not in ALLOWED_SYNTHETIC_MEDIA_FIXTURES
    ):
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


def read_candidate_bytes(candidate: Candidate) -> bytes | None:
    if candidate.source == "index":
        result = subprocess.run(
            ["git", "show", f":{candidate.path.as_posix()}"],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    if not candidate.path.is_file():
        return None
    return candidate.path.read_bytes()


def read_candidate_text(candidate: Candidate) -> str | None:
    data = read_candidate_bytes(candidate)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def synthetic_media_fixture_reason(candidate: Candidate) -> str | None:
    expected = ALLOWED_SYNTHETIC_MEDIA_FIXTURE_SHA256.get(candidate.path)
    if expected is None:
        return None
    data = read_candidate_bytes(candidate)
    if data is None:
        return "allowed synthetic media fixture is not readable"
    if hashlib.sha256(data).hexdigest() != expected:
        return "allowed synthetic media fixture content does not match generated fixture"
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
    findings = local_only_boundary_findings()

    for candidate in files:
        path = candidate.path
        reason = forbidden_path_reason(path)
        if reason is not None:
            findings.append(Finding(path.as_posix(), None, reason))
        media_reason = synthetic_media_fixture_reason(candidate)
        if media_reason is not None:
            findings.append(Finding(path.as_posix(), None, media_reason))
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
