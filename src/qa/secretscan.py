"""Secret-scan gate.

Scans the project for hard-coded secrets using regex patterns and
Shannon-entropy heuristics. Returns a :class:`~plugins.registry.GateResult`.

This gate is intentionally conservative: it reports findings as failures so
that secrets are caught before they reach a reviewer or are committed.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from plugins.registry import GateResult


# ---------------------------------------------------------------------------
# Regex patterns for well-known secret formats
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub PAT", re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    ("GitHub OAuth", re.compile(r"gho_[a-zA-Z0-9]{36}")),
    ("GitHub Actions token", re.compile(r"ghs_[a-zA-Z0-9]{36}")),
    ("Private key header", re.compile(r"-----BEGIN\s+(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("Slack token", re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}")),
    ("Stripe secret key", re.compile(r"sk_live_[0-9a-zA-Z]{24,}")),
    ("Generic API key assignment", re.compile(r'(?i)(?:api[_\-]?key|secret[_\-]?key|access[_\-]?token)\s*[=:]\s*["\']?[A-Za-z0-9/+_\-]{20,}["\']?')),
]

# Files / directories to skip.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".tox",
})
_SKIP_EXTENSIONS: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".bin",
    ".jpg", ".jpeg", ".png", ".gif", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".bz2", ".whl",
    ".lock",  # lock files contain hashes, not secrets
})

# Entropy threshold for high-entropy string detection.
_ENTROPY_THRESHOLD = 4.5
_MIN_ENTROPY_LEN = 20


def _shannon_entropy(text: str) -> float:
    """Compute Shannon entropy (bits per character) of *text*."""
    if not text:
        return 0.0
    freq: dict[str, int] = {}
    for ch in text:
        freq[ch] = freq.get(ch, 0) + 1
    length = len(text)
    return -sum((c / length) * math.log2(c / length) for c in freq.values())


def _high_entropy_strings(content: str) -> list[str]:
    """Return substrings that look like high-entropy secrets."""
    # Look for quoted strings or assignment RHS values.
    candidates = re.findall(r'["\']([A-Za-z0-9/+_\-=]{20,})["\']', content)
    return [c for c in candidates if _shannon_entropy(c) >= _ENTROPY_THRESHOLD]


async def run_secretscan(
    cwd: Path,
    paths: list[Path] | None = None,
) -> GateResult:
    """Scan *cwd* for hard-coded secrets.

    v0.13.0: when *paths* is non-None, restrict the scan to the listed
    files (resolved relative to *cwd*). Non-existent paths are silently
    skipped. Files in the legacy ``_SKIP_EXTENSIONS`` set are still
    skipped even when explicitly listed (binary blobs are never useful
    secret carriers).

    When *paths* is None (legacy), walk the whole tree under *cwd*.

    Returns ``GateResult(passed=False, ...)`` if any secrets are found,
    ``GateResult(passed=True, ...)`` otherwise.
    """
    findings: list[str] = []

    for path in _iter_files(cwd, paths=paths):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        try:
            rel = path.relative_to(cwd)
        except ValueError:
            # Caller passed an absolute path outside cwd. Use the path
            # as-is for reporting; never let this crash the gate.
            rel = path

        # Regex pattern scan.
        for label, pattern in _SECRET_PATTERNS:
            if pattern.search(content):
                findings.append(f"{rel}: {label}")

        # Entropy scan.
        for suspect in _high_entropy_strings(content):
            findings.append(f"{rel}: high-entropy string ({_shannon_entropy(suspect):.2f} bits) — {suspect[:8]}…")

    if findings:
        detail = "potential secrets found:\n" + "\n".join(findings[:20])
        if len(findings) > 20:
            detail += f"\n… and {len(findings) - 20} more"
        return GateResult(passed=False, details=detail)
    return GateResult(passed=True, details="no secrets detected")


def _iter_files(cwd: Path, paths: list[Path] | None = None):
    """Yield scannable files under *cwd*.

    Two modes:

    * ``paths=None`` (legacy): recursive ``cwd.rglob("*")`` walk, skipping
      known noise directories (``.git``, ``.venv``, ``node_modules``…) and
      file extensions in ``_SKIP_EXTENSIONS``.
    * ``paths=[...]`` (v0.13.0 diff-scope): yield only the files in the
      list. Each entry is resolved relative to *cwd* if not absolute.
      Non-existent paths and ``_SKIP_EXTENSIONS`` matches are skipped
      silently. Skip-dir filter is *not* applied — caller is expected to
      have curated the list (typically from a developer's diff, which is
      already scoped to changes the executor introduced).
    """
    if paths is None:
        for item in cwd.rglob("*"):
            if not item.is_file():
                continue
            # Skip noise directories.
            if any(part in _SKIP_DIRS for part in item.parts):
                continue
            if item.suffix in _SKIP_EXTENSIONS:
                continue
            yield item
        return

    seen: set[Path] = set()
    for raw in paths:
        if not raw.is_absolute():
            candidate = cwd / raw
        else:
            candidate = raw
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.is_file():
            continue
        if resolved.suffix in _SKIP_EXTENSIONS:
            continue
        yield resolved


__all__ = ["run_secretscan"]
