"""Per-repo code-size baseline (v0.22.0 Phase 1).

Mirrors :mod:`qa.secretscan_baseline`. Some repos carry pre-existing
technical debt that would trip the v1 thresholds on every task — without
a baseline, the gate becomes noise. The baseline captures a *snapshot*
of current aggregate metrics so subsequent runs report only **net-new**
findings (drift from the recorded state).

Workflow
--------

1. Operator runs ``autodev code-size baseline`` (Phase 6 CLI hook —
   wired in a later phase) → :func:`compute_baseline` scans the full
   tree and writes ``.autodev/code-size-baseline.json``.
2. ``run_code_size`` (when ``cfg.qa_gates.code_size_baseline_enabled``)
   subtracts the baseline counts from the live aggregate via
   :func:`filter_against_baseline` before threshold checks.
3. Operator refreshes when intentionally accepting new technical debt.

v1 scope (intentional)
----------------------
Counts (``dead_symbols``, ``commented_out_blocks``, ``duplicate_clusters``,
``cyclomatic_max``) are subtracted; ratios (``defensive_ratio``,
``doc_density``, ``cyclomatic_mean``) are NOT — the baseline doesn't have
the per-file LOC weighting needed to subtract a ratio meaningfully.
``long_functions`` is filtered to "name not seen in baseline". This is
the simplest defensible rule for v1; Phase 6 may revisit.
"""

from __future__ import annotations

import json
from pathlib import Path

from qa.code_size_metrics import CodeSizeMetrics, aggregate, compute_metrics_for_file


_BASELINE_REL_PATH = Path(".autodev") / "code-size-baseline.json"


def _baseline_path(cwd: Path) -> Path:
    return cwd / _BASELINE_REL_PATH


async def compute_baseline(cwd: Path) -> CodeSizeMetrics:
    """Scan *cwd* and persist the baseline metrics carrier.

    Walks every ``.py`` file under *cwd* (excluding noise dirs), computes
    per-file metrics, aggregates, writes the JSON carrier alongside the
    secretscan baseline. Returns the computed :class:`CodeSizeMetrics`
    so callers may use it in-process before the file is re-read.
    """
    # Reuse the gate's _iter_python_files semantics by importing lazily —
    # avoids a circular import at module load.
    from qa.code_size import _iter_python_files

    files = _iter_python_files(cwd, None)
    per_file = [compute_metrics_for_file(p) for p in files]
    snapshot = aggregate(per_file)

    target = _baseline_path(cwd)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {"version": 1, "metrics": snapshot.to_dict()},
            indent=2,
        ),
        encoding="utf-8",
    )
    return snapshot


def load_baseline(cwd: Path) -> CodeSizeMetrics | None:
    """Read the persisted baseline. ``None`` if absent or malformed."""
    path = _baseline_path(cwd)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    metrics_dict = raw.get("metrics", {})
    if not isinstance(metrics_dict, dict):
        return None

    out = CodeSizeMetrics()
    for key, value in metrics_dict.items():
        if not hasattr(out, key):
            continue
        # Strict type guard — silently drop wrong types.
        try:
            setattr(out, key, value)
        except (TypeError, ValueError):
            continue
    return out


async def filter_against_baseline(
    metrics: CodeSizeMetrics, cwd: Path
) -> CodeSizeMetrics:
    """Return a new :class:`CodeSizeMetrics` with baseline counts subtracted.

    Counts subtract; ratios + cyclomatic_max pass through. Negative
    results clamp to 0 (baseline may be stale, e.g. dead code that was
    later removed). When the baseline file is missing, *metrics* is
    returned unchanged (fail-open).
    """
    baseline = load_baseline(cwd)
    if baseline is None:
        return metrics

    baseline_long = set(baseline.long_functions)

    return CodeSizeMetrics(
        token_count=metrics.token_count,  # passthrough: not threshold-checked
        defensive_ratio=metrics.defensive_ratio,
        doc_density=metrics.doc_density,
        functions_per_file=max(
            0, metrics.functions_per_file - baseline.functions_per_file
        ),
        loc_executable=metrics.loc_executable,
        cyclomatic_max=metrics.cyclomatic_max,
        cyclomatic_mean=metrics.cyclomatic_mean,
        n_abstractions=max(
            0, metrics.n_abstractions - baseline.n_abstractions
        ),
        dead_symbols=max(0, metrics.dead_symbols - baseline.dead_symbols),
        commented_out_blocks=max(
            0, metrics.commented_out_blocks - baseline.commented_out_blocks
        ),
        duplicate_clusters=max(
            0, metrics.duplicate_clusters - baseline.duplicate_clusters
        ),
        excess_over_min=metrics.excess_over_min,
        long_functions=[
            name for name in metrics.long_functions if name not in baseline_long
        ],
    )


__all__ = [
    "compute_baseline",
    "filter_against_baseline",
    "load_baseline",
]
