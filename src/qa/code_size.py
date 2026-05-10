"""Phase 1 (v0.22.0): code-size QA gate.

Diff-scoped, soft-warn (severity='warn'), Python-only. Built on the
deterministic primitives in :mod:`qa.code_size_metrics` (radon for cyclomatic +
LOC, AST for abstractions / defensive / doc counts, vulture / eradicate /
pylint R0801 as subprocess fallbacks).

Honest scope statement
----------------------
This gate catches deterministic *syntactic* bloat — dead code, commented-out
blocks, excessive cyclomatic complexity, oversized functions, cross-file
duplication. Per Token Sugar §I (arxiv 2512.08266), only ~25.5% of GPT-4's
Python tokens are syntax; the remaining ~75% (semantic redundancy, gratuitous
abstractions, repeated idioms) is the domain of the ``minimality_judge``
tournament role (Phase 4) and the longitudinal panel (Phase 6), NOT this
gate. Operators should expect the gate to surface low-recall, high-precision
findings — by design.

Severity: warn-only by default
------------------------------
The gate emits ``GateResult(passed=True, severity="warn", ...)`` whenever a
threshold is breached. ``passed=True`` is intentional: per the v0.22.0
contract on :class:`plugins.registry.GateResult`, ``passed`` reports whether
the gate *ran successfully*; ``severity`` decides whether the orchestrator
halts. Warn-only ensures the gate is always-on without blocking development.

Calibration-first promotion (per Cordeiro §II-B + PyExamine §IV-B)
------------------------------------------------------------------
A rule may be promoted from ``warn`` to ``block`` only after measured
precision ≥85% on a 50-PR calibration sample. See
``scripts/calibrate_code_size.py`` and ``tests/calibration/code_size/``.

Default thresholds (Fontana 2015 anchors)
-----------------------------------------
* ``cyclomatic_max > 20`` → warn (Fontana classifies > 10 as "complex",
  > 20 as "highly complex" — we use the higher anchor for the gate so we
  surface only the most actionable cases).
* per-function LOC > 100 → warn (Fontana > 50 = warn, > 100 =
  block-eligible — we adopt the block-eligible anchor).
* dead_symbols > 0 → warn (vulture ≥ 80% confidence is the floor).
* commented_out_blocks > 0 → warn (eradicate fills the Pylint blind
  spot per PyExamine Table I).
* duplicate_clusters > 0 → warn (pylint R0801; mostly inert at single-file
  granularity but contributes when scope spans multiple files).
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from plugins.registry import GateResult
from qa.code_size_metrics import (
    CodeSizeMetrics,
    aggregate,
    compute_metrics_for_file,
)


# Fontana 2015 anchors. Mirrored in src/config/schema.py CodeSizeThresholds
# defaults; we keep a local default here so the gate has a usable threshold
# even when the caller passes ``thresholds=None``.
_DEFAULT_THRESHOLDS: dict[str, int] = {
    "cyclomatic_max": 20,
    "loc_per_function": 100,
    "dead_symbols": 0,
    "commented_out_blocks": 0,
    "duplicate_clusters": 0,
}

# Skip directories — mirrors qa.secretscan._SKIP_DIRS so a full-tree walk
# (paths=None) doesn't dive into vendored / generated trees.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".tox",
})


def _path_in_scope(rel_path: str, scope_prefixes: list[str]) -> bool:
    """Mirror of qa.secretscan._path_in_scope. Returns True when *rel_path*
    lies under any prefix in *scope_prefixes*; True on empty scope."""
    if not scope_prefixes:
        return True
    for raw_prefix in scope_prefixes:
        prefix = raw_prefix.rstrip("/")
        if rel_path == prefix:
            return True
        if rel_path.startswith(prefix + "/"):
            return True
    return False


def _iter_python_files(cwd: Path, paths: list[Path] | None) -> list[Path]:
    """Yield .py files under *cwd*.

    Two modes (mirrors qa.secretscan._iter_files):

    * ``paths=None`` — recursive ``cwd.rglob("*.py")`` with skip-dir filter.
    * ``paths=[...]`` — only the listed files (resolved relative to *cwd*),
      filtered to .py extension. Caller is expected to have already
      curated the list (typically the developer's diff scope).
    """
    if paths is None:
        out: list[Path] = []
        for item in cwd.rglob("*.py"):
            if not item.is_file():
                continue
            if any(part in _SKIP_DIRS for part in item.parts):
                continue
            out.append(item)
        return out

    seen: set[Path] = set()
    out: list[Path] = []
    for raw in paths:
        candidate = raw if raw.is_absolute() else cwd / raw
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.is_file():
            continue
        if resolved.suffix != ".py":
            continue
        out.append(resolved)
    return out


def _check_thresholds(
    aggregated: CodeSizeMetrics,
    thresholds: dict[str, int],
) -> list[str]:
    """Return human-readable warning lines for breached thresholds."""
    warnings: list[str] = []

    if aggregated.cyclomatic_max > thresholds.get(
        "cyclomatic_max", _DEFAULT_THRESHOLDS["cyclomatic_max"]
    ):
        warnings.append(
            f"cyclomatic_max={aggregated.cyclomatic_max} "
            f"> {thresholds['cyclomatic_max']} (Fontana 2015)"
        )

    # long_functions are computed against ``loc_per_function`` at metric
    # time; the field is populated regardless of threshold but we surface
    # only when the threshold-marker is present.
    if aggregated.long_functions:
        warnings.append(
            f"long functions (LOC > {thresholds.get('loc_per_function', 100)}): "
            + ", ".join(aggregated.long_functions[:5])
            + (
                f" … and {len(aggregated.long_functions) - 5} more"
                if len(aggregated.long_functions) > 5
                else ""
            )
        )

    if aggregated.dead_symbols > thresholds.get(
        "dead_symbols", _DEFAULT_THRESHOLDS["dead_symbols"]
    ):
        warnings.append(
            f"dead_symbols={aggregated.dead_symbols} "
            f"(vulture ≥80% confidence)"
        )

    if aggregated.commented_out_blocks > thresholds.get(
        "commented_out_blocks", _DEFAULT_THRESHOLDS["commented_out_blocks"]
    ):
        warnings.append(
            f"commented_out_blocks={aggregated.commented_out_blocks} "
            f"(eradicate)"
        )

    if aggregated.duplicate_clusters > thresholds.get(
        "duplicate_clusters", _DEFAULT_THRESHOLDS["duplicate_clusters"]
    ):
        warnings.append(
            f"duplicate_clusters={aggregated.duplicate_clusters} "
            f"(pylint R0801)"
        )

    return warnings


async def run_code_size(
    cwd: Path,
    paths: list[Path] | None = None,
    edit_scope: list[str] | None = None,
    *,
    thresholds: dict[str, int] | None = None,
    baseline_enabled: bool = False,
    long_function_threshold: int | None = None,
) -> GateResult:
    """Diff-scoped code-size gate. Mirrors :func:`qa.secretscan.run_secretscan`
    signature so wiring at the orchestrator gate site is symmetric.

    Args:
        cwd: Repo root.
        paths: Optional diff-scope filter (Python files only). When non-None,
            only the listed files are measured. ``[]`` means "no Python files
            in diff" → gate returns a silent pass.
        edit_scope: Optional repo-relative prefix filter. Composes with
            ``paths`` (intersection).
        thresholds: Mapping of rule name → integer cap. Falls back to
            :data:`_DEFAULT_THRESHOLDS` (Fontana 2015) on missing keys.
            Accepts both raw ints (test path) and pydantic models with
            ``.model_dump()`` (config path) — anything dict-like works.
        baseline_enabled: When True, subtract the per-repo baseline from
            findings before threshold check. See :mod:`qa.code_size_baseline`.
        long_function_threshold: Optional override for the per-function LOC
            threshold passed to ``compute_metrics_for_file``. Defaults to
            ``thresholds["loc_per_function"]`` if present, else 100.

    Returns:
        :class:`GateResult` with:

        * ``passed=True`` always (warn-only gate).
        * ``severity="warn"`` when any threshold breached, else
          ``severity="info"``.
        * ``details`` is a human-readable summary; empty when nothing
          breached.
        * ``metrics`` carries the aggregated :class:`CodeSizeMetrics` for
          downstream consumers (Phase 2 knowledge seeding, Phase 6
          longitudinal CLI).
    """
    # Normalise thresholds to a plain dict.
    raw_thresholds: dict[str, int]
    if thresholds is None:
        raw_thresholds = dict(_DEFAULT_THRESHOLDS)
    elif hasattr(thresholds, "model_dump"):
        raw_thresholds = dict(_DEFAULT_THRESHOLDS)
        raw_thresholds.update(thresholds.model_dump())
    else:
        raw_thresholds = dict(_DEFAULT_THRESHOLDS)
        raw_thresholds.update(dict(thresholds))

    # Per-function LOC limit drives the metric-time long-function detector.
    fn_loc_limit = (
        long_function_threshold
        if long_function_threshold is not None
        else int(raw_thresholds.get("loc_per_function", 100))
    )

    # Empty diff-scope is a no-op pass — distinguish from "scan everything"
    # (paths=None). This mirrors qa.mutation_test.run_mutation_test.
    if paths is not None and not paths:
        return GateResult(
            passed=True,
            severity="info",
            details="code-size: no Python files in diff scope",
            metrics={},
        )

    files = _iter_python_files(cwd, paths)

    # Apply edit_scope filter (intersection with paths).
    if edit_scope:
        scope_prefixes = [p.rstrip("/") for p in edit_scope]
        filtered: list[Path] = []
        for path in files:
            try:
                rel = path.relative_to(cwd).as_posix()
            except ValueError:
                # Out-of-cwd path — conservative skip when scope active.
                continue
            if _path_in_scope(rel, scope_prefixes):
                filtered.append(path)
        files = filtered

    if not files:
        return GateResult(
            passed=True,
            severity="info",
            details="code-size: no Python files to measure",
            metrics={},
        )

    per_file = [
        compute_metrics_for_file(p, long_function_threshold=fn_loc_limit)
        for p in files
    ]
    aggregated = aggregate(per_file)

    # Phase 1 v1 baseline subtractor — opt-in via flag, mirrors secretscan.
    if baseline_enabled:
        from qa.code_size_baseline import filter_against_baseline

        aggregated = await filter_against_baseline(aggregated, cwd)

    warnings = _check_thresholds(aggregated, raw_thresholds)

    metrics_payload: dict[str, Any] = aggregated.to_dict()
    metrics_payload["files_measured"] = len(files)
    metrics_payload["thresholds_applied"] = dict(raw_thresholds)

    if warnings:
        detail = "code-size warnings (Fontana 2015 thresholds):\n  - " + (
            "\n  - ".join(warnings)
        )
        return GateResult(
            passed=True,
            severity="warn",
            details=detail,
            metrics=metrics_payload,
        )

    return GateResult(
        passed=True,
        severity="info",
        details=(
            f"code-size: {len(files)} file(s) measured; no threshold breaches "
            f"(loc={aggregated.loc_executable}, "
            f"cc_max={aggregated.cyclomatic_max})"
        ),
        metrics=metrics_payload,
    )


__all__ = ["run_code_size"]
