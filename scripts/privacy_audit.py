#!/usr/bin/env python3
"""Fail closed when a public tree contains likely secrets or private artifacts."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path


EXCLUDED_DIRS = {".git", ".venv", "node_modules", "__pycache__"}
FORBIDDEN_NAMES = {
    "credentials.json",
    "gmail_token.pickle",
    "market_watchlist.json",
    "market_monitor_state.json",
    "backtest_config.json",
    "openclaw-workspace-state.json",
    "cliproxy-private",
    "auth.json", "auth.json5", "auth.yaml", "auth.yml", "hosts.yml",
    "openclaw.json", "openclaw.json5", "openclaw.yaml", "openclaw.yml",
}
FORBIDDEN_SUFFIXES = {
    ".log", ".pickle", ".p12", ".pem", ".key", ".jsonl", ".har",
    ".db", ".sqlite", ".sqlite3", ".pid", ".session", ".lock", ".pyc", ".pyo", ".pyd",
}
FORBIDDEN_PARTS = {
    "memory", "logs", "cache", "results", "reports", "backtests", ".openclaw",
    "cliproxy-private", "request-logs", "runtime-state", "session-state", "auth-state",
}
PRIVATE_BASENAME = re.compile(
    r"(?i)^(?:\.?client[-_]key|proxy[-_]client|auth[-_]profiles?|sessions?|"
    r"request[-_]history|model[-_]traffic[-_]policy|cliproxy[-_]switch[-_]policy|"
    r"\.?cliproxy[-_]switch[-_]installed)(?:[._-].*)?$"
)
RUNTIME_STATE_BASENAME = re.compile(
    r"(?i)^(?:market_monitor_state\.json|.+\.delivery\.json|.+\.initialized|\.monitor-state-.+\.tmp)(?:[._-].*)?$"
)
CREDENTIAL_NAME = (
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|bot[_-]?token|"
    r"auth[_-]?token|client[_-]?secret|password|token|secret)"
)

PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9]{20,})\b"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Telegram bot token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "email address": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "Chinese mobile number": re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    "personal absolute path": re.compile(
        r"(?:/" + "Users/[^/\\s]+|/" + r"home/[^/\s]+|[A-Za-z]:\\" + r"Users\\[^\\\s]+)"
    ),
    "credential assignment": re.compile(
        r"(?i)(?<![\w-])[\"']?" + CREDENTIAL_NAME
        + r"[\"']?\s*[:=]\s*(?:\"[^\"\r\n]+\"|'[^'\r\n]+')"
    ),
    "shell credential assignment": re.compile(
        r"(?im)^\s*(?:export\s+)?(?:[A-Z][A-Z0-9]*_)*" + CREDENTIAL_NAME
        + r"=(?:\"[^\"\r\n]+\"|'[^'\r\n]+'|[^\s\"'$`#;]+)(?=\s|;|$)"
    ),
}
YAML_CREDENTIAL = re.compile(
    r"(?im)^\s*[\"']?" + CREDENTIAL_NAME
    + r"[\"']?\s*:\s*(?!null\s*(?:#|$)|~\s*(?:#|$))[^\s#\[\]{},\"'$`][^\r\n#]*"
)


def iter_files(root: Path):
    """Do not traverse symlinks; tracked files cannot hide in excluded folders."""
    paths = set()
    def on_error(error):
        raise error
    for directory, folders, filenames in os.walk(root, followlinks=False, onerror=on_error):
        parent = Path(directory)
        for name in list(folders):
            path = parent / name
            if path.is_symlink():
                paths.add(path)
                folders.remove(name)
            elif name in EXCLUDED_DIRS:
                folders.remove(name)
        paths.update(parent / name for name in filenames if name != ".git")
    if (root / ".git").is_symlink():
        raise ValueError("symlinked Git metadata")
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "-z"],
            capture_output=True, timeout=10, check=True,
        )
        for item in result.stdout.split(b"\x00"):
            if not item:
                continue
            relative = Path(os.fsdecode(item))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("invalid tracked path")
            paths.add(root / relative)
    yield from sorted(paths)


def read_regular_file(root: Path, relative: Path) -> bytes:
    """Open every component without following links, including replacement races."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError("safe file opening is unavailable")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, directory_flags)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor,
        )
        with os.fdopen(file_descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError("not a regular file")
            return handle.read()
    finally:
        os.close(descriptor)


def audit(root: Path) -> list[str]:
    root = Path(root).absolute()
    findings: list[str] = []
    if root.is_symlink() or not root.is_dir():
        return ["audit root must be a real directory"]
    try:
        paths = list(iter_files(root))
    except (OSError, ValueError, subprocess.SubprocessError):
        return ["file inventory unavailable"]
    for path in paths:
        relative = path.relative_to(root)
        lowered_parts = {part.lower() for part in relative.parts[:-1]}
        name = path.name.lower()
        if (name in FORBIDDEN_NAMES or PRIVATE_BASENAME.fullmatch(name) or RUNTIME_STATE_BASENAME.fullmatch(name)
                or name == ".env" or (name.startswith(".env.") and name != ".env.example")):
            findings.append(f"forbidden private filename: {relative}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(f"forbidden private file type: {relative}")
        if lowered_parts & FORBIDDEN_PARTS or any(
            PRIVATE_BASENAME.fullmatch(part) or RUNTIME_STATE_BASENAME.fullmatch(part) for part in lowered_parts
        ):
            findings.append(f"forbidden runtime-state directory: {relative}")
        if path.is_symlink():
            findings.append(f"symlink is not scanned: {relative}")
            continue
        try:
            raw = read_regular_file(root, relative)
        except OSError:
            findings.append(f"unreadable or unsafe file: {relative}")
            continue
        texts = [raw.decode("utf-8", errors="replace")]
        # NUL-containing files still carry readable secrets; also handle BOM text.
        if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            texts.append(raw.decode("utf-32", errors="replace"))
        elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            texts.append(raw.decode("utf-16", errors="replace"))
        patterns = dict(PATTERNS)
        if path.suffix.lower() in {".yml", ".yaml"}:
            patterns["YAML credential assignment"] = YAML_CREDENTIAL
        for text in texts:
            for label, pattern in patterns.items():
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    findings.append(f"{label}: {relative}:{line}")
    return findings


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").absolute()
    findings = audit(root)
    if findings:
        print("Privacy audit FAILED:")
        for item in sorted(set(findings)):
            print(f"- {item}")
        return 1
    print(f"Privacy audit passed: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
