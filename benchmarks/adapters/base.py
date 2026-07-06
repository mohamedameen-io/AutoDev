"""Protocol for a benchmark solve-side adapter.

An adapter supplies the benchmark-specific glue around the reusable solve-half:
it turns one benchmark *instance* into a solvable git workdir, provides the
intent handed to ``plan``, and post-processes the solved workdir + its
:class:`~benchmarks.runner.solve.SolveOutcome` into a prediction record. The
reusable autodev-driving and diff-recovery live in
:mod:`benchmarks.runner.solve`; adapters stay tiny.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from benchmarks.runner.solve import SolveOutcome, SolveProfile

# One benchmark problem (SWE-bench's ``instance`` is a dict of instance_id,
# repo, base_commit, problem_statement, ...). Kept generic here.
Instance = Mapping[str, Any]


class InstancePrepareError(Exception):
    """Contract: an adapter raises this from :meth:`BenchmarkAdapter.prepare` to
    signal an EXPECTED per-instance setup failure (e.g. an invalid / missing
    ``base_commit`` that cannot be checked out).

    The runner (``run_guarded_solve`` / ``run_solve``) catches this base type,
    records that ONE instance as ERROR (with a patch-less prediction so it is
    accounted for, never a silent drop), and CONTINUES the sweep — one bad
    instance must never abort an overnight run. Any OTHER exception is treated as
    a genuine harness bug and propagates loudly (the runner does not swallow it).
    """


@runtime_checkable
class BenchmarkAdapter(Protocol):
    """Solve-side glue for one benchmark (e.g. host-arm64 SWE-bench-Lite)."""

    name: str

    def prepare(self, instance: Instance, workdir: Path) -> SolveProfile:
        """Materialise ``instance`` into ``workdir`` as a git repo checked out at
        its baseline commit, and return the :class:`SolveProfile` to solve it
        under (env overlay, ``config_patch``, timeout, diff-exclusions)."""
        ...

    def intent(self, instance: Instance) -> str:
        """Return the natural-language intent/spec handed to ``plan``."""
        ...

    def predict(
        self, instance: Instance, workdir: Path, outcome: SolveOutcome
    ) -> Mapping[str, Any]:
        """Post-process a solved workdir + outcome into a prediction record, e.g.
        ``{"instance_id", "model_name_or_path", "model_patch"}``."""
        ...


__all__ = [
    "BenchmarkAdapter",
    "Instance",
    "InstancePrepareError",
]
