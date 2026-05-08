"""Repo-capacity probing + max_turns resolver.

v0.13.0 introduces repo-size-aware ``max_turns`` auto-scaling: rather than a
fixed budget per complexity bucket (``simple=10, medium=20, complex=40``),
the orchestrator probes the repo at startup and, when the repo is
"Unity-class huge", doubles the per-task turn budget so genuinely complex
investigations have runway to finish without burning retry quota on
``error_max_turns``.

The module is a small sibling to :mod:`runtime.resource_probe`. It exports a
:class:`RepoCapacity` snapshot, a :func:`probe_repo` reader, and a
:func:`resolve_max_turns` resolver that layers an ``is_huge`` multiplier
over :data:`tournament.task_overrides.TASK_MAX_TURNS_DEFAULTS`.

Exported surface:

* :class:`RepoCapacity` — snapshot of repo size at probe time.
* :func:`probe_repo` — runs ``git ls-files | wc -l`` (fast path for git
  repos) with a ``du``-style fallback. Returns a populated RepoCapacity.
* :func:`resolve_max_turns` — maps (complexity, capacity, base) → an int
  ``max_turns`` value, doubling when ``capacity.is_huge`` is True.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from autologging import get_logger
from tournament.task_overrides import TASK_MAX_TURNS_DEFAULTS


logger = get_logger(component="runtime.repo_probe")


# Thresholds for ``is_huge``. Tuned conservatively in v0.13.0:
# - 20k files matches the Unity QNX run that motivated the feature.
# - 5 GB total bytes catches binary-asset-heavy game/ML repos that may have
#   fewer files but still exhaust per-task budgets due to slow grep/build.
_HUGE_FILE_COUNT_THRESHOLD = 20_000
_HUGE_TOTAL_BYTES_THRESHOLD = 5 * 1024**3  # 5 GB

# Multiplier applied when ``capacity.is_huge`` is True. v0.13.0 used a single
# 2.0× multiplier across all buckets; v0.20.0 D1 introduces per-bucket curves
# because the navigation cost is unevenly distributed: simple tasks burn the
# most extra turns just navigating the repo, while complex tasks already
# carry generous budgets. Default per-bucket curves:
#
#     simple  → 3.0× (10 → 30)   # heavy navigation overhead
#     medium  → 2.0× (20 → 40)   # legacy multiplier preserved
#     complex → 1.5× (40 → 60)   # already generous; modest bump
#
# The legacy single-multiplier (used as a fallback when a bucket isn't in the
# map) remains 2.0 for byte-identical behavior on operator-supplied ``base``
# overrides (which have no complexity bucket to consult).
_HUGE_MULTIPLIER = 2.0
_HUGE_BUCKET_MULTIPLIERS: dict[str, float] = {
    "simple": 3.0,
    "medium": 2.0,
    "complex": 1.5,
}


@dataclass
class RepoCapacity:
    """Snapshot of repo size at probe time.

    Attributes:
        file_count: Number of files counted at probe time. For git repos the
            count is from ``git ls-files`` (tracked files only — vendored
            ``node_modules`` and ``.venv`` are correctly excluded). For non-
            git directories, falls back to a recursive walk.
        total_bytes: Total bytes across all enumerated files. For git repos,
            sums ``stat()`` over tracked files. Approximate — does not chase
            symlinks or LFS-pointer-resolved sizes.
        depth_max: Maximum directory depth observed during enumeration.
            Currently informational; reserved for future heuristics that
            could tune ``max_turns`` based on directory tree shape.
        is_huge: True iff ``file_count > 20_000`` OR ``total_bytes > 5 GB``.
            The single field downstream resolvers consult to decide whether
            to apply the ``_HUGE_MULTIPLIER`` to ``max_turns``.
    """

    file_count: int
    total_bytes: int
    depth_max: int
    is_huge: bool


def _count_files(cwd: Path) -> int:
    """Count files under *cwd*. Fast path: ``git ls-files | wc -l``.

    Falls back to a recursive walk when the directory is not a git
    worktree or when the git CLI is unavailable. The walk path is
    necessarily slower for large non-git trees; users with non-git huge
    repos should accept the cold-start cost as a one-shot.
    """
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

    # Non-git fallback: walk the tree.
    count = 0
    for _root, _dirs, files in os.walk(cwd):
        count += len(files)
    return count


def _total_bytes(cwd: Path) -> int:
    """Sum bytes across all files under *cwd*.

    Uses ``git ls-files -z | xargs -0 wc -c`` style logic via Python stat for
    git repos (avoids re-walking vendored deps). Falls back to ``os.walk``
    for non-git trees.
    """
    if (cwd / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "ls-files", "-z"],
                cwd=str(cwd),
                capture_output=True,
                timeout=30,
                check=False,
            )
            if out.returncode == 0:
                total = 0
                for raw in out.stdout.split(b"\x00"):
                    if not raw:
                        continue
                    try:
                        total += (cwd / raw.decode("utf-8", errors="replace")).stat().st_size
                    except OSError:
                        continue
                return total
        except (OSError, subprocess.SubprocessError):
            pass

    total = 0
    for root, _dirs, files in os.walk(cwd):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _max_depth(cwd: Path) -> int:
    """Return the maximum directory depth observed under *cwd*.

    Depth is measured relative to *cwd* — *cwd* itself is depth 0. Cheap
    informational signal; reserved for future heuristics.
    """
    max_depth = 0
    base_parts = len(cwd.parts)
    for root, _dirs, _files in os.walk(cwd):
        depth = len(Path(root).parts) - base_parts
        if depth > max_depth:
            max_depth = depth
    return max_depth


def probe_repo(cwd: Path) -> RepoCapacity:
    """Read repo size signals and return a populated :class:`RepoCapacity`.

    Cheap-ish (~10ms on a 1k-file repo, up to ~1s on a 100k-file repo) — safe
    to call once per orchestrator startup and cache the result for the
    session.

    Args:
        cwd: Repo root. Need not be a git worktree; non-git directories use
            a slower ``os.walk`` fallback.

    Returns:
        A fresh :class:`RepoCapacity` snapshot. ``is_huge`` is True iff at
        least one threshold is exceeded.
    """
    file_count = _count_files(cwd)
    total_bytes = _total_bytes(cwd)
    depth_max = _max_depth(cwd)
    is_huge = (
        file_count > _HUGE_FILE_COUNT_THRESHOLD
        or total_bytes > _HUGE_TOTAL_BYTES_THRESHOLD
    )

    logger.info(
        "tournament.repo_probed",
        file_count=file_count,
        total_bytes=total_bytes,
        depth_max=depth_max,
        is_huge=is_huge,
    )

    return RepoCapacity(
        file_count=file_count,
        total_bytes=total_bytes,
        depth_max=depth_max,
        is_huge=is_huge,
    )


def resolve_max_turns(
    complexity: str | None,
    capacity: RepoCapacity,
    base: int | None = None,
    bucket_multipliers: dict[str, float] | None = None,
) -> int | None:
    """Resolve a ``max_turns`` value for a task, optionally scaled for huge repos.

    Resolution algorithm:

    1. Pick a "raw" value:

       * If ``base`` is provided (operator override), use it.
       * Else if ``complexity`` is one of ``simple|medium|complex``, look it up in
         :data:`tournament.task_overrides.TASK_MAX_TURNS_DEFAULTS`.
       * Else return ``None`` (caller falls back to its own spec default).

    2. If ``capacity.is_huge`` is True, multiply by the per-bucket curve
       value and round to the nearest int. v0.20.0 D1 default per-bucket
       curves — simple 3.0×, medium 2.0×, complex 1.5× — replace the
       legacy single 2.0× multiplier.

       * When ``bucket_multipliers`` is supplied (operator override), look
         up the bucket there first; missing buckets fall through to the
         default curve.
       * When ``base`` is supplied (no complexity bucket to key off of) OR
         ``complexity`` is unknown, the legacy single ``_HUGE_MULTIPLIER``
         (2.0) is used — preserves byte-identical behavior for operator
         overrides.

    Args:
        complexity: The task's complexity bucket (``simple``, ``medium``,
            ``complex``) or None when the architect did not tag it.
        capacity: Snapshot from :func:`probe_repo`.
        base: Operator-supplied override. When set, bypasses the lookup
            table; still subject to the ``is_huge`` multiplier (legacy
            single-multiplier path).
        bucket_multipliers: Optional operator override for the per-bucket
            curve. ``None`` (default) uses :data:`_HUGE_BUCKET_MULTIPLIERS`.
            Missing buckets fall through to the default curve.

    Returns:
        A positive int suitable as ``max_turns`` for an
        :class:`~adapters.types.AgentInvocation`, or None when no signal
        was available (caller's spec default kicks in).
    """
    bucket: str | None = None
    if base is not None:
        raw: int | None = base
    elif complexity is not None and complexity in TASK_MAX_TURNS_DEFAULTS:
        raw = TASK_MAX_TURNS_DEFAULTS[complexity]
        bucket = complexity
    else:
        raw = None

    if raw is None:
        return None

    if capacity.is_huge:
        # Per-bucket curve when we have a bucket; legacy single multiplier
        # for operator-supplied bases (no bucket) and unknown complexity.
        if bucket is None:
            return int(round(raw * _HUGE_MULTIPLIER))
        # Operator-overridden curve wins; otherwise default.
        if bucket_multipliers is not None and bucket in bucket_multipliers:
            mult = bucket_multipliers[bucket]
        else:
            mult = _HUGE_BUCKET_MULTIPLIERS.get(bucket, _HUGE_MULTIPLIER)
        return int(round(raw * mult))
    return raw


__all__ = [
    "RepoCapacity",
    "probe_repo",
    "resolve_max_turns",
]
