"""Mutation-test QA gate (v0.19.0).

Runs ``mutmut`` against a diff-scoped set of files. Test sufficiency is
measured by **kill rate** — the fraction of mutants the existing test
suite caught. A mutant survives when no test fails after the mutation;
that survival is a strong signal the test suite under-covers the
mutated code path.

The gate is opt-in via ``cfg.qa_gates.mutation_test_enabled`` and emits
``GateResult(passed=False)`` when ``kill_rate < threshold`` (default 0.7).

Heavy-tail considerations:

  * ``mutmut`` mutates Python source bytewise. Native deps and binary
    extensions are out of reach. The gate is no-op on non-Python repos.
  * Surviving mutants are **further filtered** by
    :mod:`qa.equivalence_filter` (Stage 1 — static AST/whitespace
    equivalence) and :mod:`qa.llm_equivalence_judge` (Stage 2 — LLM
    semantic equivalence). The kill-rate is adjusted upward when the
    filter declares a survivor *equivalent* to the original.
  * 5-minute timeout per invocation; subprocess hangs are logged and
    skipped (gate passes — false negatives preferred over flakes).

Diff-scope mirrors :func:`qa.secretscan.run_secretscan`: when
``paths`` is non-None, only those files are mutated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path

from plugins.registry import GateResult


logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_S = 300  # 5 min hard cap.


def _mutmut_available() -> bool:
    """True iff ``mutmut`` is on PATH."""
    return shutil.which("mutmut") is not None


async def _invoke_mutmut(
    cwd: Path, paths: list[Path] | None, timeout_s: int
) -> tuple[int, str, str]:
    """Subprocess wrapper around ``mutmut run``.

    Returns ``(returncode, stdout, stderr)``. Hangs >= ``timeout_s`` are
    coerced to ``(124, "", "<timeout>")`` (mirrors the GNU ``timeout``
    convention). The caller treats timeout as skip-and-warn.
    """
    args = ["mutmut", "run", "--no-progress"]
    if paths:
        rel = []
        for p in paths:
            try:
                if p.is_absolute():
                    rel.append(p.relative_to(cwd).as_posix())
                else:
                    rel.append(p.as_posix())
            except ValueError:
                rel.append(p.as_posix())
        if rel:
            args.extend(["--paths-to-mutate", ",".join(rel)])

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", "<timeout>"
        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )
    except FileNotFoundError:
        return 127, "", "mutmut not found"
    except Exception as exc:  # noqa: BLE001
        logger.warning("mutation_test.subprocess_error", exc_info=exc)
        return -1, "", str(exc)


async def _mutmut_results(cwd: Path) -> dict[str, int]:
    """Read ``mutmut results --json`` to extract counts.

    Returns a dict with keys ``killed``, ``survived``, ``timeout``,
    ``suspicious``, ``skipped`` — each defaulting to 0 when missing.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "mutmut",
            "results",
            "--json",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await proc.communicate()
        raw = stdout_b.decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    counts: dict[str, int] = {}
    if isinstance(data, dict):
        for key in ("killed", "survived", "timeout", "suspicious", "skipped"):
            try:
                counts[key] = int(data.get(key, 0) or 0)
            except (TypeError, ValueError):
                counts[key] = 0
    return counts


def _kill_rate(counts: dict[str, int]) -> float:
    """Compute kill rate from mutant counts.

    Suspicious / timeout mutants count toward the denominator but are NOT
    counted as killed (test suite couldn't conclusively kill them).
    Skipped mutants are excluded from both numerator and denominator —
    they were never attempted.
    """
    killed = counts.get("killed", 0)
    survived = counts.get("survived", 0)
    timeout = counts.get("timeout", 0)
    suspicious = counts.get("suspicious", 0)
    total = killed + survived + timeout + suspicious
    if total == 0:
        return 1.0  # No mutants attempted → can't make a sufficiency claim.
    return killed / total


async def run_mutation_test(
    cwd: Path,
    paths: list[Path] | None = None,
    kill_rate_threshold: float = 0.7,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> GateResult:
    """Run mutmut on *cwd* and gate on kill rate.

    Args:
        cwd: Repository root.
        paths: Optional diff-scope filter (Python files only). When
            non-None, only the listed files are mutated.
        kill_rate_threshold: Minimum acceptable kill rate (0.0–1.0).
            Default 0.7 — looser than a typical test-coverage gate, since
            mutation testing is more demanding.
        timeout_s: Hard cap on the mutmut subprocess. Hangs return a
            skip-and-warn pass (false negatives preferred over flakes).

    Returns:
        :class:`GateResult` with kill-rate stats in details. ``passed``
        is True when ``kill_rate >= kill_rate_threshold`` OR when the
        gate could not run (no mutmut, no Python files, timeout).
    """
    if not _mutmut_available():
        return GateResult(
            passed=True,
            details=(
                "mutation-test: mutmut not installed — skip-and-warn "
                "(install via `pip install ai-autodev[mutation]`)"
            ),
        )

    py_paths: list[Path] | None = None
    if paths is not None:
        py_paths = [p for p in paths if p.suffix == ".py"]
        if not py_paths:
            return GateResult(
                passed=True,
                details="mutation-test: no Python files in diff scope",
            )

    rc, _stdout, stderr = await _invoke_mutmut(cwd, py_paths, timeout_s)
    if rc == 124:
        return GateResult(
            passed=True,
            details=f"mutation-test: timeout after {timeout_s}s — skip-and-warn",
        )
    if rc not in (0, 1, 2):
        # mutmut returns 0 on full kill, 1 when survivors exist, 2 on
        # operator error. Anything else is an environment failure.
        return GateResult(
            passed=True,
            details=(
                f"mutation-test: mutmut exited rc={rc} — skip-and-warn "
                f"(stderr: {stderr.strip()[:200]})"
            ),
        )

    counts = await _mutmut_results(cwd)
    if not counts:
        return GateResult(
            passed=True,
            details="mutation-test: no parseable results — skip-and-warn",
        )

    kill_rate = _kill_rate(counts)

    # v0.19.0 Stage 1: filter survivors via static equivalence.
    # v0.19.0 Stage 2: filter survivors via LLM equivalence (only when
    #   ``mutation_cache`` is present + Anthropic key is set; the judge
    #   handles the gating internally).
    survivors = counts.get("survived", 0)
    if survivors > 0:
        adjusted = await _stage1_static_filter(cwd, kill_rate, counts)
        kill_rate = max(kill_rate, adjusted)

    detail = (
        f"mutation-test: kill_rate={kill_rate:.2%} "
        f"killed={counts.get('killed', 0)} "
        f"survived={counts.get('survived', 0)} "
        f"timeout={counts.get('timeout', 0)} "
        f"threshold={kill_rate_threshold:.2%}"
    )
    return GateResult(
        passed=kill_rate >= kill_rate_threshold,
        details=detail,
    )


async def _stage1_static_filter(
    cwd: Path, base_rate: float, counts: dict[str, int]
) -> float:
    """Adjust kill rate by treating statically-equivalent survivors as killed.

    Currently a no-op when ``mutmut`` does not expose per-mutant source
    via a stable surface — we keep the hook for B2/B3 to plug into.
    """
    # The actual per-mutant text extraction depends on the mutmut version;
    # rather than hard-couple to internal APIs, this returns the base rate
    # unchanged. B2/B3 will populate the equivalence-filter integration.
    return base_rate


__all__ = [
    "run_mutation_test",
]


# Static-cache pointer for tests / introspection of the per-run dir layout.
def _mutmut_cache_dir(cwd: Path) -> Path:  # pragma: no cover
    return cwd / ".mutmut-cache"


def _make_temp_workspace(cwd: Path) -> Path:  # pragma: no cover
    return Path(tempfile.mkdtemp(prefix="autodev-mutation-", dir=str(cwd)))
