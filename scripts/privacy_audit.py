#!/usr/bin/env python3
"""Fail closed when a public tree contains likely secrets or private artifacts."""

from __future__ import annotations

import re
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
}
FORBIDDEN_SUFFIXES = {".log", ".pickle", ".p12", ".pem", ".key"}
FORBIDDEN_PARTS = {"memory", "logs", "cache", "results", "reports", "backtests"}

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
        r"(?i)\b(?:api[_-]?key|bot[_-]?token|password|client[_-]?secret)\b\s*[:=]\s*[\"'][^\"']{8,}[\"']"
    ),
}


def iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        yield path


def audit(root: Path) -> list[str]:
    findings: list[str] = []
    for path in iter_files(root):
        relative = path.relative_to(root)
        lowered_parts = {part.lower() for part in relative.parts[:-1]}
        if path.name.lower() in FORBIDDEN_NAMES:
            findings.append(f"forbidden private filename: {relative}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(f"forbidden private file type: {relative}")
        if lowered_parts & FORBIDDEN_PARTS:
            findings.append(f"forbidden runtime-state directory: {relative}")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            findings.append(f"unreadable file: {relative}: {exc}")
            continue
        if b"\x00" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{label}: {relative}:{line}")
    return findings


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
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
