#!/usr/bin/env python3
"""Repository safety checks for secrets and tokenized remotes."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

ALLOWED_TRACKED_ENV_FILES = {".env.example", ".env.sample", ".env.template"}
FORBIDDEN_VIRTUALENV_DIR_NAMES = {".venv", "venv", "env"}

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "GitHub token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{30,}\b"),
    ),
    (
        "GitHub fine-grained token",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    (
        "OpenRouter API key",
        re.compile(r"\bsk-or-[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "OpenAI API key",
        re.compile(r"\bsk-(?!or-)[A-Za-z0-9_-]{30,}\b"),
    ),
    (
        "Telegram bot token",
        re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    ),
)


@dataclass(frozen=True)
class SecretFinding:
    file: Path
    line: int
    kind: str


def has_url_credentials(url: str) -> bool:
    """Return True for HTTP(S) remotes with userinfo in the URL."""
    if not url.startswith(("http://", "https://")):
        return False

    parsed = urlsplit(url)
    return "@" in parsed.netloc


def redact_remote_line(line: str) -> str:
    """Redact HTTP(S) remote credentials while preserving useful context."""
    parts = line.split()
    if len(parts) < 2:
        return line

    url = parts[1]
    if not has_url_credentials(url):
        return line

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    redacted_url = urlunsplit((
        parsed.scheme,
        f"<redacted>@{host}",
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))
    redacted_parts = [*parts]
    redacted_parts[1] = redacted_url
    return " ".join(redacted_parts)


def find_forbidden_tracked_paths(paths: list[str]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        normalized = path.strip("/")
        name = Path(normalized).name
        if (
            (name == ".env" or name.startswith(".env."))
            and name not in ALLOWED_TRACKED_ENV_FILES
        ):
            findings.append(path)
            continue
        if _has_forbidden_virtualenv_part(normalized):
            findings.append(path)
    return findings


def _has_forbidden_virtualenv_part(path: str) -> bool:
    return any(
        part in FORBIDDEN_VIRTUALENV_DIR_NAMES
        for part in Path(path).parts
    )


def find_tokenized_remotes(remote_lines: list[str]) -> list[str]:
    findings: list[str] = []
    for line in remote_lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        url = parts[1]
        if has_url_credentials(url):
            findings.append(redact_remote_line(line))
    return findings


def find_secret_findings(files: list[Path]) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for line_number, line in enumerate(text.splitlines(), start=1):
            for kind, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        SecretFinding(file=path, line=line_number, kind=kind)
                    )
    return findings


def run_git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def tracked_files() -> list[str]:
    output = run_git(["ls-files", "-z"])
    return [path for path in output.split("\0") if path]


def candidate_files() -> list[str]:
    output = run_git(["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    return dedupe_paths(path for path in output.split("\0") if path)


def dedupe_paths(paths: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def remote_lines() -> list[str]:
    output = run_git(["remote", "-v"])
    return [line for line in output.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root for resolving tracked file paths.",
    )
    args = parser.parse_args()

    tracked_paths = tracked_files()
    scan_paths = candidate_files()
    failures: list[str] = []

    forbidden_paths = find_forbidden_tracked_paths(tracked_paths)
    for path in forbidden_paths:
        failures.append(f"Forbidden tracked path: {path}")

    tokenized_remotes = find_tokenized_remotes(remote_lines())
    for line in tokenized_remotes:
        failures.append(f"Remote URL contains credentials: {line}")

    secret_findings = find_secret_findings([args.root / path for path in scan_paths])
    for finding in secret_findings:
        rel_path = finding.file.relative_to(args.root)
        failures.append(f"{finding.kind}: {rel_path}:{finding.line}")

    if failures:
        print("Repository safety check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Repository safety check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
