"""Code-size metrics shared between the QA gate (Phase 1) and the
longitudinal CLI (Phase 6).

Pure metric computation — no I/O beyond reading the file the caller hands us
and (for vulture/eradicate/pylint) invoking subprocesses. Designed so a
single ``compute_metrics_for_file`` call returns the deterministic structural
primitives Bohr §3.4 + the Phase 1 plan describe:

* AST-derived counts: ``n_abstractions``, ``defensive_ratio``, ``doc_density``,
  ``functions_per_file``, ``token_count``.
* radon programmatic API: ``loc_executable`` (raw.sloc), ``cyclomatic_max``,
  ``cyclomatic_mean``.
* Subprocess fallbacks (graceful degradation when binaries are absent):
  ``dead_symbols`` (vulture --min-confidence 80), ``commented_out_blocks``
  (eradicate), ``duplicate_clusters`` (pylint --enable=R0801).

Tokeniser choice
----------------
``token_count`` uses ``len(source.split())`` as a fast whitespace-token
proxy. We considered ``tiktoken`` (cl100k_base) as the Bohr §3.4 default,
but: (a) it adds a heavyweight optional dep; (b) the absolute numbers
matter less than the verbose-vs-lean ratio for our gate; (c) the v1 gate
emits ``warn`` only, so an order-of-magnitude tokenizer is sufficient.
Phase 6 longitudinal CLI MAY upgrade to tiktoken when promoted from
warn → block.

Subprocess robustness
---------------------
Every subprocess wrapper catches ``FileNotFoundError`` and any non-zero
exit, returning 0 (unknown). Missing tools must NOT crash the gate —
operators may run AutoDev without installing the optional ``code-size``
extra and we degrade to AST-only metrics rather than failing.
"""

from __future__ import annotations

import ast
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_SUBPROCESS_TIMEOUT_S = 30


@dataclass
class CodeSizeMetrics:
    """Bohr §3.4 quad + static-analysis primitives + YapBench-style baseline-relative.

    The split mirrors the Phase 1 plan:

    * Bohr §3.4 quad — token count, defensive ratio, doc density,
      functions per file. Used by both the gate (thresholds) and the
      longitudinal panel (median trend).
    * Static-analysis primitives — radon (programmatic), vulture +
      eradicate + pylint R0801 (subprocess). Per Token Sugar §I, this
      catches deterministic syntactic bloat (~25% of headroom).
    * YapBench-style baseline-relative — set at tournament time when
      the minimum-passing-candidate LOC is known. ``None`` for
      gate-time computation (no candidate cohort to compare against).
    """

    # Bohr §3.4 quad
    token_count: int = 0
    defensive_ratio: float = 0.0
    doc_density: float = 0.0
    functions_per_file: int = 0
    # Static-analysis primitives
    loc_executable: int = 0
    cyclomatic_max: int = 0
    cyclomatic_mean: float = 0.0
    n_abstractions: int = 0
    dead_symbols: int = 0
    commented_out_blocks: int = 0
    duplicate_clusters: int = 0
    # YapBench-style baseline-relative (set at tournament time, not here)
    excess_over_min: int | None = None
    # Optional per-file extras for the gate's threshold check.
    # Not part of the Bohr quad — needed so the gate can offer
    # actionable detail (which functions exceed loc_per_function).
    long_functions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for ``GateResult.metrics`` (must be JSON-stable)."""
        return {
            "token_count": self.token_count,
            "defensive_ratio": round(self.defensive_ratio, 4),
            "doc_density": round(self.doc_density, 4),
            "functions_per_file": self.functions_per_file,
            "loc_executable": self.loc_executable,
            "cyclomatic_max": self.cyclomatic_max,
            "cyclomatic_mean": round(self.cyclomatic_mean, 4),
            "n_abstractions": self.n_abstractions,
            "dead_symbols": self.dead_symbols,
            "commented_out_blocks": self.commented_out_blocks,
            "duplicate_clusters": self.duplicate_clusters,
            "excess_over_min": self.excess_over_min,
            "long_functions": list(self.long_functions),
        }


# ---------------------------------------------------------------------------
# AST + radon: deterministic, no subprocess
# ---------------------------------------------------------------------------


def _count_ast_primitives(tree: ast.AST) -> tuple[int, int, int, int, int]:
    """Walk the AST once and return:

    ``(n_abstractions, defensive_count, docstring_count, callable_count,
       function_loc_max)``

    where:

    * ``n_abstractions`` = ``ClassDef`` + ``FunctionDef`` + ``AsyncFunctionDef``.
    * ``defensive_count`` = ``Try`` blocks + ``None`` literal comparisons
      + ``assert`` statements (Bohr §3.4 defensive ratio numerator).
    * ``docstring_count`` = functions/classes/module with a non-empty
      ``ast.get_docstring`` (Bohr §3.4 doc density numerator).
    * ``callable_count`` = ``FunctionDef`` + ``AsyncFunctionDef`` (denominator
      for doc density when paired with class count).
    * ``function_loc_max`` = max (end_lineno - lineno + 1) across all
      callables; 0 if no callables (used by the gate to emit
      ``loc_per_function`` warnings).
    """
    n_abstractions = 0
    defensive_count = 0
    docstring_count = 0
    callable_count = 0
    function_loc_max = 0

    # Module-level docstring counts toward doc_density.
    if isinstance(tree, ast.Module):
        if ast.get_docstring(tree):
            docstring_count += 1

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n_abstractions += 1
            callable_count += 1
            if ast.get_docstring(node):
                docstring_count += 1
            # end_lineno is set on Python 3.8+; fall back conservatively.
            end = getattr(node, "end_lineno", None) or node.lineno
            function_loc_max = max(function_loc_max, end - node.lineno + 1)
        elif isinstance(node, ast.ClassDef):
            n_abstractions += 1
            if ast.get_docstring(node):
                docstring_count += 1
        elif isinstance(node, ast.Try):
            defensive_count += 1
        elif isinstance(node, ast.Assert):
            defensive_count += 1
        elif isinstance(node, ast.Compare):
            # ``x is None``, ``x is not None``, ``x == None`` etc.
            for cmp_op, comparator in zip(node.ops, node.comparators):
                if isinstance(comparator, ast.Constant) and comparator.value is None:
                    defensive_count += 1
                    break

    return (
        n_abstractions,
        defensive_count,
        docstring_count,
        callable_count,
        function_loc_max,
    )


def _long_functions(tree: ast.AST, threshold: int) -> list[str]:
    """Return qualified names of functions with LOC > *threshold*.

    Used by the gate to surface actionable per-function bloat findings.
    Names are unqualified (``foo`` not ``mod.foo``) for brevity in the
    GateResult.details body.
    """
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", None) or node.lineno
            loc = end - node.lineno + 1
            if loc > threshold:
                out.append(f"{node.name} (loc={loc})")
    return out


def _radon_metrics(source: str) -> tuple[int, int, float]:
    """Return ``(loc_executable, cyclomatic_max, cyclomatic_mean)`` via radon.

    Returns ``(0, 0, 0.0)`` when radon is not importable or the source
    cannot be analysed (radon raises on some malformed input even when
    ast.parse succeeds — defensively swallow).
    """
    try:
        from radon.complexity import cc_visit
        from radon.raw import analyze
    except ImportError:
        return 0, 0, 0.0

    try:
        raw = analyze(source)
        loc = int(raw.sloc)
    except Exception:  # noqa: BLE001
        loc = 0

    try:
        ccs = cc_visit(source)
    except Exception:  # noqa: BLE001
        return loc, 0, 0.0

    if not ccs:
        return loc, 0, 0.0
    complexities = [c.complexity for c in ccs]
    cc_max = max(complexities)
    cc_mean = sum(complexities) / len(complexities)
    return loc, cc_max, cc_mean


# ---------------------------------------------------------------------------
# Subprocess fallbacks: vulture, eradicate, pylint R0801
# ---------------------------------------------------------------------------


def _run_subprocess(
    args: list[str], *, cwd: Path | None = None, timeout_s: int
) -> tuple[int, str, str]:
    """Synchronous subprocess wrapper. Returns ``(rc, stdout, stderr)``.

    Returns ``(127, "", "<not-found>")`` when the binary is missing.
    Returns ``(124, "", "<timeout>")`` on hangs.
    Any other failure returns ``(-1, "", str(exc))`` — caller treats as
    "tool failed, contribute 0 to the metric".
    """
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", "<not-found>"
    except subprocess.TimeoutExpired:
        return 124, "", "<timeout>"
    except Exception as exc:  # noqa: BLE001
        logger.debug("code_size_metrics.subprocess_error", exc_info=exc)
        return -1, "", str(exc)


def _vulture_dead_symbols(path: Path, *, timeout_s: int) -> int:
    """Count dead symbols vulture reports at ≥80% confidence.

    vulture exits with code 0 (no findings), 3 (findings), or non-zero
    on parse error. Each line of stdout is one finding. We count lines
    with non-empty content.
    """
    if shutil.which("vulture") is None:
        return 0
    rc, stdout, _ = _run_subprocess(
        ["vulture", str(path), "--min-confidence", "80"],
        timeout_s=timeout_s,
    )
    # rc=0: no findings; rc=3: findings present (vulture documented).
    if rc not in (0, 3):
        return 0
    return sum(1 for line in stdout.splitlines() if line.strip())


def _eradicate_blocks(path: Path, *, timeout_s: int) -> int:
    """Count commented-out code blocks (3+ consecutive lines).

    eradicate prints one finding per line; we approximate "blocks of 3+"
    by counting eradicate findings outright. Per PyExamine Table I,
    Pylint has no equivalent rule, so this gives a real signal even
    though our block detection is coarse.
    """
    if shutil.which("eradicate") is None:
        return 0
    rc, stdout, _ = _run_subprocess(
        ["eradicate", str(path)],
        timeout_s=timeout_s,
    )
    # eradicate exits 0 when no findings; non-zero with findings on stdout.
    if rc == 127:
        return 0
    return sum(1 for line in stdout.splitlines() if line.strip())


def _pylint_duplicates(path: Path, *, timeout_s: int) -> int:
    """Count duplicate-code clusters via pylint R0801.

    pylint with ``--disable=all --enable=R0801`` reports duplicate-code
    findings as messages of type ``duplicate-code``. With ``--output-format=json``,
    each finding is a JSON dict; we count distinct clusters by message ID.
    Single-file invocation rarely surfaces R0801 (pylint needs ≥2 files
    for cross-file duplicate detection), so this metric is more useful
    when invoked across the diff scope. The per-file API still returns
    a deterministic 0 when no clusters are detected.
    """
    if shutil.which("pylint") is None:
        return 0
    rc, stdout, _ = _run_subprocess(
        [
            "pylint",
            "--disable=all",
            "--enable=R0801",
            "--output-format=json",
            str(path),
        ],
        timeout_s=timeout_s,
    )
    if rc == 127:
        return 0
    if not stdout.strip():
        return 0
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return 0
    if not isinstance(data, list):
        return 0
    return sum(
        1
        for entry in data
        if isinstance(entry, dict)
        and entry.get("symbol") == "duplicate-code"
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def compute_metrics_for_file(
    path: Path,
    *,
    long_function_threshold: int = 100,
    subprocess_timeout_s: int = _DEFAULT_SUBPROCESS_TIMEOUT_S,
    skip_subprocess: bool = False,
) -> CodeSizeMetrics:
    """Compute deterministic metrics for a single Python file.

    Args:
        path: Absolute path to a ``.py`` file. Non-Python paths return
            an empty :class:`CodeSizeMetrics`.
        long_function_threshold: Functions with LOC > this are recorded
            in ``CodeSizeMetrics.long_functions``. Mirrors the Fontana
            2015 anchor (50 = warn, 100 = block-eligible).
        subprocess_timeout_s: Hard cap on each external tool. Hangs
            count as 0 contributions (skip-and-continue).
        skip_subprocess: When True, skip vulture/eradicate/pylint and
            return AST+radon metrics only. Used by the longitudinal
            CLI in fast-mode and by tests that don't have the optional
            extras installed.

    Returns:
        :class:`CodeSizeMetrics` populated as best-effort. A file that
        won't parse with ``ast.parse`` returns the empty default — we
        prefer silent zero over crashing the gate.
    """
    if path.suffix != ".py":
        return CodeSizeMetrics()
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return CodeSizeMetrics()

    metrics = CodeSizeMetrics()

    # Whitespace-token proxy — see module docstring tokenizer choice.
    metrics.token_count = len(source.split())

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Even when parse fails, radon / subprocess tools may still
        # contribute usable signal — but the AST-derived primitives are
        # 0 by necessity.
        loc, cc_max, cc_mean = _radon_metrics(source)
        metrics.loc_executable = loc
        metrics.cyclomatic_max = cc_max
        metrics.cyclomatic_mean = cc_mean
        return metrics

    (
        n_abstractions,
        defensive_count,
        docstring_count,
        callable_count,
        _function_loc_max,
    ) = _count_ast_primitives(tree)

    metrics.n_abstractions = n_abstractions
    metrics.functions_per_file = callable_count

    # Bohr §3.4 ratios — guard against zero denominators.
    loc, cc_max, cc_mean = _radon_metrics(source)
    metrics.loc_executable = loc
    metrics.cyclomatic_max = cc_max
    metrics.cyclomatic_mean = cc_mean

    if loc > 0:
        metrics.defensive_ratio = defensive_count / loc
    # doc_density denominator: callables + classes (Bohr §3.4 = "documentable
    # entities"). Module-level docstring contributes to numerator already.
    class_count = sum(1 for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    documentable = callable_count + class_count
    if documentable > 0:
        metrics.doc_density = docstring_count / documentable

    metrics.long_functions = _long_functions(tree, long_function_threshold)

    if not skip_subprocess:
        metrics.dead_symbols = _vulture_dead_symbols(
            path, timeout_s=subprocess_timeout_s
        )
        metrics.commented_out_blocks = _eradicate_blocks(
            path, timeout_s=subprocess_timeout_s
        )
        metrics.duplicate_clusters = _pylint_duplicates(
            path, timeout_s=subprocess_timeout_s
        )

    return metrics


def aggregate(metrics_list: list[CodeSizeMetrics]) -> CodeSizeMetrics:
    """Sum-aggregate per-file metrics into a single corpus-wide carrier.

    Counts (token, loc, abstractions, dead, commented_out, duplicates,
    callables) sum. Maxima (cyclomatic_max) take max. Means
    (cyclomatic_mean, defensive_ratio, doc_density) are weighted by
    loc_executable so a 5-line file doesn't dominate a 500-line file.
    Long-function lists concatenate.
    """
    if not metrics_list:
        return CodeSizeMetrics()

    total = CodeSizeMetrics()
    weighted_def = 0.0
    weighted_doc = 0.0
    weighted_cc_mean = 0.0
    total_loc_for_weight = 0

    for m in metrics_list:
        total.token_count += m.token_count
        total.loc_executable += m.loc_executable
        total.functions_per_file += m.functions_per_file
        total.n_abstractions += m.n_abstractions
        total.dead_symbols += m.dead_symbols
        total.commented_out_blocks += m.commented_out_blocks
        total.duplicate_clusters += m.duplicate_clusters
        total.cyclomatic_max = max(total.cyclomatic_max, m.cyclomatic_max)
        total.long_functions.extend(m.long_functions)
        if m.loc_executable > 0:
            weighted_def += m.defensive_ratio * m.loc_executable
            weighted_doc += m.doc_density * m.loc_executable
            weighted_cc_mean += m.cyclomatic_mean * m.loc_executable
            total_loc_for_weight += m.loc_executable

    if total_loc_for_weight > 0:
        total.defensive_ratio = weighted_def / total_loc_for_weight
        total.doc_density = weighted_doc / total_loc_for_weight
        total.cyclomatic_mean = weighted_cc_mean / total_loc_for_weight

    return total


__all__ = [
    "CodeSizeMetrics",
    "aggregate",
    "compute_metrics_for_file",
]
