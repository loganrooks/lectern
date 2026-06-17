"""Check that the tracked public tree excludes private/local artifacts."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SELF = Path("tools/public_safety_check.py")
CONTENT_EXEMPT_PATHS = {
    SELF,
    Path("tests/test_public_safety_check.py"),
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None
    message: str

    def format(self) -> str:
        if self.line is None:
            return f"{self.path}: {self.message}"
        return f"{self.path}:{self.line}: {self.message}"


def candidate_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
    )
    names = result.stdout.decode().split("\0")
    return [Path(name) for name in names if name]


def forbidden_path_reason(path: Path) -> str | None:
    text = path.as_posix()
    prefixes = (
        "goal/",
        "bundles/",
        ".venv/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".mypy_cache/",
        ".pyright/",
        "pytest-cache-files-",
        "review-artifacts/",
        "tools/review-journal/",
        ".serena/",
    )
    exact = {
        ".DS_Store",
        ".review-journal.json",
        ".review-journal.version",
        "AGENTS.md",
        "CLAUDE.md",
        "gates.toml",
        "docs/GOALFLOW_TRACK.md",
        "docs/ORCHESTRATION.md",
        "docs/REVIEWERS.md",
        "docs/REVIEW_GATES.md",
        "tools/capture_usage.py",
        "tools/gate_check.py",
    }
    if text in exact or path.name in {".DS_Store"}:
        return "forbidden public path"
    if any(text.startswith(prefix) for prefix in prefixes):
        return "forbidden public path prefix"
    return None


def content_patterns() -> list[tuple[re.Pattern[str], str]]:
    private_terms = [
        "GOAL" + "_CONTRACT",
        "goal" + "/",
        "goalflow",
        "claude -p",
        "g2-approved",
        "REVIEW" + "_GATES",
        "gates.toml",
        "tools/gate" + "_check",
        "review-journal",
        ".serena",
        "PhD student",
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


def scan_content(path: Path, patterns: list[tuple[re.Pattern[str], str]]) -> list[Finding]:
    if path in CONTENT_EXEMPT_PATHS:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
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

    for path in files:
        reason = forbidden_path_reason(path)
        if reason is not None:
            findings.append(Finding(path.as_posix(), None, reason))
        if path.is_file():
            findings.extend(scan_content(path, patterns))

    if findings:
        print("public safety check failed:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding.format()}", file=sys.stderr)
        return 1

    print("public safety check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
