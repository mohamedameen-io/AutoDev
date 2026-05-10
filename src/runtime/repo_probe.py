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
from typing import Iterator

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

# v0.25.0: directories never walked by :func:`iter_repo_files`. Mirrors
# ``qa.hallucination_guard._SKIP_DIRS`` (canonical skip set used across the
# codebase). Kept identical so the file-index inventory and the
# hallucination-guard scanner agree on what counts as "real source".
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".tox",
    }
)


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
    # v0.24.0 D5: repo shape signals. Default 0 preserves back-compat for
    # callers that build RepoCapacity directly. Computed by
    # :func:`probe_repo` from the same file walk so the cost is amortized.
    avg_file_size_bytes: int = 0
    # The directory containing the most files (largest-fan-out hot spot).
    # Useful for sparse-checkout target tuning: if 90% of files are in
    # one subdir, the sparse default is "include it" rather than "exclude
    # everything outside edit_scope". Captured as posix-style relative
    # path; empty string when the probe could not determine a winner.
    largest_dir: str = ""
    largest_dir_file_count: int = 0


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


def _largest_directory(cwd: Path) -> tuple[str, int]:
    """v0.24.0 D5: return ``(rel_path, file_count)`` for the busiest directory.

    Walks tracked-only when the repo is git-initialized (mirrors
    :func:`_count_files`'s git fast-path), otherwise falls back to
    ``os.walk``. Best-effort: subprocess / IO failure returns
    ``("", 0)`` so callers can downgrade gracefully. Returns the
    immediate-parent directory of each file (top-level directory wins
    when multiple files live in the same one).
    """
    import collections
    import os
    import subprocess

    counter: dict[str, int] = collections.Counter()

    git_dir = Path(cwd) / ".git"
    if git_dir.exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(cwd), "ls-files"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    rel = line.strip()
                    if not rel:
                        continue
                    parent = os.path.dirname(rel) or "."
                    counter[parent] += 1
        except (OSError, subprocess.SubprocessError):
            counter = collections.Counter()

    if not counter:
        for root, _dirs, files in os.walk(cwd):
            try:
                rel_root = str(Path(root).relative_to(cwd))
            except ValueError:
                continue
            rel_root = rel_root.replace(os.sep, "/")
            if any(p.startswith(".") for p in Path(rel_root).parts if p):
                continue
            if files:
                counter[rel_root if rel_root != "." else "."] += len(files)

    if not counter:
        return "", 0
    rel, n = counter.most_common(1)[0]
    return rel, n


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

    # v0.24.0 D5: shape signals. avg_file_size guards against
    # ZeroDivisionError on empty repos; largest_dir is computed via a
    # lightweight directory-bucket count over the same walk surface.
    avg_file_size = int(total_bytes // file_count) if file_count > 0 else 0
    largest_dir, largest_dir_file_count = _largest_directory(cwd)

    logger.info(
        "tournament.repo_probed",
        file_count=file_count,
        total_bytes=total_bytes,
        depth_max=depth_max,
        is_huge=is_huge,
        avg_file_size_bytes=avg_file_size,
        largest_dir=largest_dir,
        largest_dir_file_count=largest_dir_file_count,
    )

    return RepoCapacity(
        file_count=file_count,
        total_bytes=total_bytes,
        avg_file_size_bytes=avg_file_size,
        largest_dir=largest_dir,
        largest_dir_file_count=largest_dir_file_count,
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


def iter_repo_files(
    cwd: Path,
    extensions: frozenset[str] | None = None,
) -> Iterator[Path]:
    """Yield repo-relative source files under *cwd*.

    v0.25.0: introduced for the file/symbol index builder. Mirrors the
    :func:`_count_files` git fast-path / :func:`os.walk` fallback strategy:

    1. **Git fast-path** (``cwd/.git`` exists): runs ``git ls-files`` once
       and yields one absolute :class:`pathlib.Path` per tracked file.
       Vendored ``node_modules`` and ``.venv`` are correctly excluded
       because they're not tracked.
    2. **Walk fallback** (no ``.git``, or git CLI absent): recursively walks
       *cwd*, skipping any directory whose name appears in
       :data:`_SKIP_DIRS` (canonical set, mirrors
       ``qa.hallucination_guard:54``).

    Args:
        cwd: Repo root.
        extensions: Optional extension allowlist (lowercase, with leading
            dot, e.g. ``frozenset({".py", ".cpp"})``). When ``None``, every
            file is yielded.

    Yields:
        Absolute :class:`pathlib.Path` instances under *cwd* that survive
        the skip-dirs filter and the optional extension filter.
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
                for line in out.stdout.splitlines():
                    rel = line.strip()
                    if not rel:
                        continue
                    # Defensive: even tracked files may sit under a SKIP
                    # dir (someone added node_modules/foo.js). Apply the
                    # filter consistently with the walk fallback below.
                    parts = Path(rel).parts
                    if any(p in _SKIP_DIRS for p in parts):
                        continue
                    if extensions is not None:
                        if Path(rel).suffix.lower() not in extensions:
                            continue
                    candidate = cwd / rel
                    if candidate.exists():
                        yield candidate
                return
        except (OSError, subprocess.SubprocessError):
            pass

    # Walk fallback. Modify ``dirs`` in-place so ``os.walk`` does not
    # descend into skipped directories at all (cheap; preserves the
    # cost-bound used by ``hallucination_guard._iter_files``).
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        root_path = Path(root)
        for name in files:
            if extensions is not None:
                if Path(name).suffix.lower() not in extensions:
                    continue
            yield root_path / name


__all__ = [
    "RepoCapacity",
    "iter_repo_files",
    "probe_repo",
    "resolve_max_turns",
]
