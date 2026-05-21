"""v0.37.0 H5: centralised ``is_huge_repo(cwd)`` helper.

A single threshold check against
:attr:`config.schema.AutodevConfig.index_full_rebuild_threshold_files`
(default 5000) lets the H5 auto-defaults — multipliers on H1/H2/H3
knobs, hallucination-guard skip-list extension, adapter language-weight
default — all key off the same signal.

This module deliberately avoids importing :mod:`runtime.repo_probe` to
keep the dependency direction one-way: ``repo_probe.RepoCapacity.is_huge``
is a richer signal (uses 20k-files / 5 GB thresholds) used by the
existing tournament / worktree machinery; H5's auto-defaults use the
narrower ``index_full_rebuild_threshold_files`` signal because that's
the operator-facing knob most likely to be tuned per-project.

Escape hatch: when ``cfg.huge_repo_overrides_disabled`` is True,
:func:`is_huge_repo` ALWAYS returns False even when the file count
exceeds the threshold. Mirrors the existing per-tournament-phase
opt-out pattern.

v0.38.0 I1 (HK11): :func:`is_huge_repo_with_ttl` is an opt-in sibling
for long-lived sessions where the lru-cached :func:`is_huge_repo`
result would go stale (cwd file counts evolve during multi-hour runs).
Default behaviour is preserved — single-dispatch orchestrator calls
keep using the lru cache. Use the TTL variant only when re-probing
within a session is desirable (e.g. nightly daemons, long resumption
loops).
"""

from __future__ import annotations

import functools
import os
import subprocess
import time
from pathlib import Path
from typing import Any


# Default threshold mirrors ``AutodevConfig.index_full_rebuild_threshold_files``
# so the helper produces consistent results when called without a config
# (test fixtures, ad-hoc callers).
DEFAULT_HUGE_REPO_THRESHOLD = 5000


@functools.lru_cache(maxsize=8)
def _count_files_cached(cwd_str: str) -> int:
    """File count for *cwd_str*, cached for the process lifetime.

    Mirrors :func:`runtime.repo_probe._count_files` — git fast-path with
    ``os.walk`` fallback. Cache key is the path string so the same cwd
    is not re-scanned across multiple ``is_huge_repo`` consultations
    within a single orchestrator run.
    """
    cwd = Path(cwd_str)
    if (cwd / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "ls-files"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if out.returncode == 0:
                return sum(1 for line in out.stdout.splitlines() if line.strip())
        except (OSError, subprocess.SubprocessError):
            pass

    count = 0
    for _root, _dirs, files in os.walk(cwd):
        count += len(files)
    return count


def is_huge_repo(
    cwd: Path,
    threshold: int | None = None,
    *,
    cfg: Any | None = None,
) -> bool:
    """Return True iff *cwd* contains more than *threshold* tracked files.

    Args:
        cwd: Repository root.
        threshold: File-count threshold. ``None`` (default) uses
            ``cfg.index_full_rebuild_threshold_files`` when *cfg* is
            supplied, else :data:`DEFAULT_HUGE_REPO_THRESHOLD`.
        cfg: Optional :class:`config.schema.AutodevConfig` (or duck-typed
            stand-in). When provided, honors
            ``cfg.huge_repo_overrides_disabled`` (master escape hatch)
            and reads ``cfg.index_full_rebuild_threshold_files`` when
            *threshold* is None.

    Returns:
        ``True`` when the file count exceeds *threshold* AND the escape
        hatch is not set; ``False`` otherwise. Cached per-cwd so
        repeated consultations during a single orchestrator dispatch
        are free.
    """
    # Operator escape hatch always wins — restores pre-v0.37.0 behavior
    # on huge repos by short-circuiting all H5 auto-defaults.
    if cfg is not None and getattr(cfg, "huge_repo_overrides_disabled", False):
        return False

    if threshold is None:
        if cfg is not None:
            threshold = int(
                getattr(
                    cfg,
                    "index_full_rebuild_threshold_files",
                    DEFAULT_HUGE_REPO_THRESHOLD,
                )
            )
        else:
            threshold = DEFAULT_HUGE_REPO_THRESHOLD

    try:
        count = _count_files_cached(str(cwd.resolve()))
    except OSError:
        return False
    return count > threshold


def clear_cache() -> None:
    """Drop the per-cwd file-count cache.

    Test-only helper so individual tests can re-scan a tmp_path that was
    populated mid-run; in production the cache lives for the process
    lifetime (cwd file counts don't materially change within a single
    AutoDev invocation).
    """
    _count_files_cached.cache_clear()


# ---------------------------------------------------------------------------
# v0.38.0 I1 (HK11): opt-in TTL-bounded variant for long-lived sessions.
# ---------------------------------------------------------------------------

# ``{resolved_cwd_str: (file_count, expires_at_unix_ts)}``. Module-level so
# the entries survive across :func:`is_huge_repo_with_ttl` calls within a
# single process; explicitly drained via :func:`clear_ttl_cache` in tests.
_ttl_cache: dict[str, tuple[int, float]] = {}


def _count_files_with_ttl(cwd_str: str, ttl_s: float) -> int:
    """File count for *cwd_str*, refreshed once per *ttl_s* seconds."""
    now = time.time()
    entry = _ttl_cache.get(cwd_str)
    if entry is not None and now < entry[1]:
        return entry[0]
    # Re-probe via the same git/os.walk path used by the lru-cached
    # variant; bypass the lru cache so the TTL entry stays authoritative.
    cwd = Path(cwd_str)
    count: int | None = None
    if (cwd / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "ls-files"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if out.returncode == 0:
                count = sum(
                    1 for line in out.stdout.splitlines() if line.strip()
                )
        except (OSError, subprocess.SubprocessError):
            count = None
    if count is None:
        count = 0
        for _root, _dirs, files in os.walk(cwd):
            count += len(files)
    _ttl_cache[cwd_str] = (count, now + ttl_s)
    return count


def is_huge_repo_with_ttl(
    cwd: Path,
    ttl_s: float = 3600.0,
    *,
    cfg: Any | None = None,
) -> bool:
    """Like :func:`is_huge_repo` but re-probes every *ttl_s* seconds.

    Intended for long-lived sessions (multi-hour resume loops, daemon
    processes) where the lru-cached :func:`is_huge_repo` result would
    miss a repo crossing the threshold mid-session. Default single-
    dispatch orchestrator behaviour is unchanged — only callers that
    explicitly opt in pay the periodic re-probe cost.

    Same semantics as :func:`is_huge_repo` otherwise:
    ``cfg.huge_repo_overrides_disabled`` short-circuits to False, and
    *threshold* defaults to ``cfg.index_full_rebuild_threshold_files``
    when *cfg* is supplied else :data:`DEFAULT_HUGE_REPO_THRESHOLD`.
    """
    if cfg is not None and getattr(cfg, "huge_repo_overrides_disabled", False):
        return False

    threshold: int
    if cfg is not None:
        threshold = int(
            getattr(
                cfg,
                "index_full_rebuild_threshold_files",
                DEFAULT_HUGE_REPO_THRESHOLD,
            )
        )
    else:
        threshold = DEFAULT_HUGE_REPO_THRESHOLD

    try:
        count = _count_files_with_ttl(str(cwd.resolve()), ttl_s)
    except OSError:
        return False
    return count > threshold


def clear_ttl_cache() -> None:
    """Drop the TTL-bounded per-cwd file-count cache.

    Test-only helper mirroring :func:`clear_cache`. Production callers
    rely on the TTL itself to age entries out.
    """
    _ttl_cache.clear()


__all__ = [
    "DEFAULT_HUGE_REPO_THRESHOLD",
    "clear_cache",
    "clear_ttl_cache",
    "is_huge_repo",
    "is_huge_repo_with_ttl",
]
