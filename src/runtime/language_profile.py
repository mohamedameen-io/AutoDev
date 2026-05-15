"""Codebase language profile -- weighted extension scan with caching.

v0.31.0 (Phase 5.3): scans the repo and returns a normalised
``{language: percentage}`` mapping. Used by:

* :mod:`adapters.fitness` to score how well an adapter matches the
  codebase (Phase 5.4).
* :func:`adapters.detect.detect_platform` for opt-in language-weighted
  platform selection via ``AUTODEV_LANG_WEIGHT`` (Phase 5.5).
* :func:`cli.commands.doctor.doctor` to surface the top languages in
  the doctor report (Phase 5.6).

Caching: the profile is persisted to ``.autodev/language_profile.json``
and recomputed only when a tracked source file's mtime is newer than the
cache mtime. ``autodev init`` should call :func:`compute_language_profile`
with ``force_recompute=True`` to refresh on each new project.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from runtime.repo_probe import iter_repo_files


# Per-extension (language, weight). The weight nudges the percentages so
# a single .py file outvotes a single .h file (Python source is denser
# signal than a generic C/C++ header). Extracted from the recovery plan.
EXTENSION_WEIGHTS: dict[str, tuple[str, int]] = {
    ".ts": ("typescript", 50),
    ".tsx": ("typescript", 50),
    ".js": ("javascript", 40),
    ".jsx": ("javascript", 40),
    ".mjs": ("javascript", 30),
    ".py": ("python", 100),
    ".cpp": ("cpp", 80),
    ".cc": ("cpp", 80),
    ".cxx": ("cpp", 80),
    ".h": ("cpp", 60),
    ".hpp": ("cpp", 60),
    ".c": ("c", 80),
    ".java": ("java", 70),
    ".go": ("go", 50),
    ".rs": ("rust", 50),
}

# Threshold used by :func:`get_dominant_language`: when no language
# clears this fraction the codebase is reported as ``"mixed"``.
DOMINANT_THRESHOLD = 0.40

# Cache file lives under ``.autodev/`` -- recomputed on init or when any
# tracked source file is newer than the cache.
_CACHE_FILENAME = "language_profile.json"


def _extension_set() -> frozenset[str]:
    return frozenset(EXTENSION_WEIGHTS.keys())


def _scan(cwd: Path) -> dict[str, float]:
    """Single-pass weighted extension scan; returns normalised percentages."""
    totals: dict[str, float] = defaultdict(float)
    for fp in iter_repo_files(cwd, extensions=_extension_set()):
        suffix = fp.suffix.lower()
        info = EXTENSION_WEIGHTS.get(suffix)
        if info is None:
            continue
        lang, weight = info
        totals[lang] += float(weight)

    grand = sum(totals.values())
    if grand == 0.0:
        return {"other": 1.0}
    return {lang: total / grand for lang, total in totals.items()}


def _newest_source_mtime(cwd: Path) -> float:
    """Return the newest mtime across tracked source files. ``0.0`` if empty."""
    newest = 0.0
    for fp in iter_repo_files(cwd, extensions=_extension_set()):
        try:
            m = fp.stat().st_mtime
        except OSError:
            continue
        if m > newest:
            newest = m
    return newest


def _cache_path(cwd: Path) -> Path:
    return cwd / ".autodev" / _CACHE_FILENAME


def _load_cache(path: Path) -> dict[str, float] | None:
    """Read a cached profile. Returns ``None`` on any I/O / schema error."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    profile = raw.get("profile") if isinstance(raw, dict) else None
    if not isinstance(profile, dict):
        return None
    out: dict[str, float] = {}
    for k, v in profile.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            return None
    return out


def _save_cache(path: Path, profile: dict[str, float]) -> None:
    """Persist the profile (best-effort). Failure is silent -- the cache
    is an optimisation, not a contract."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"profile": profile}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        pass


def compute_language_profile(
    cwd: Path, *, force_recompute: bool = False
) -> dict[str, float]:
    """Scan repo files; return ``{language: percentage}`` summing to 1.0.

    Empty / language-free repos return ``{"other": 1.0}``.

    Caching: when a cache file at ``.autodev/language_profile.json``
    exists and no tracked source file has an mtime newer than the
    cache's own mtime, the cached profile is returned without rescanning.
    Pass ``force_recompute=True`` (used by ``autodev init``) to bypass
    the cache.
    """
    cache_file = _cache_path(cwd)
    if not force_recompute and cache_file.exists():
        try:
            cache_mtime = cache_file.stat().st_mtime
        except OSError:
            cache_mtime = 0.0
        if cache_mtime > 0.0:
            newest = _newest_source_mtime(cwd)
            if newest <= cache_mtime:
                cached = _load_cache(cache_file)
                if cached is not None:
                    return cached

    profile = _scan(cwd)
    _save_cache(cache_file, profile)
    return profile


def get_dominant_language(profile: dict[str, float]) -> str:
    """Return the dominant language or ``"mixed"`` if no clear winner.

    A language is "dominant" iff its share is at or above
    :data:`DOMINANT_THRESHOLD` (0.40 by default). Empty profiles return
    ``"other"``.
    """
    if not profile:
        return "other"
    top_lang, top_share = max(profile.items(), key=lambda kv: kv[1])
    if top_share >= DOMINANT_THRESHOLD:
        return top_lang
    return "mixed"


def top_n(profile: dict[str, float], n: int = 5) -> list[tuple[str, float]]:
    """Return the ``n`` highest-share languages as ``(lang, share)`` pairs."""
    return sorted(profile.items(), key=lambda kv: kv[1], reverse=True)[:n]


__all__ = [
    "DOMINANT_THRESHOLD",
    "EXTENSION_WEIGHTS",
    "compute_language_profile",
    "get_dominant_language",
    "top_n",
]


def _iter_subset(
    paths: Iterable[Path], extensions: frozenset[str]
) -> Iterable[Path]:
    """Helper kept for callers that already have a path iterator."""
    for p in paths:
        if p.suffix.lower() in extensions:
            yield p
