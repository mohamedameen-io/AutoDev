"""Protocol for a benchmark score-side adapter + its result types.

A scorer takes prediction records (from an adapter's ``predict``) and returns a
per-instance PASS/FAIL/ERROR verdict plus a summary. ERROR is first-class and
kept distinct from FAIL so infra/quota flakiness (a submission failure, a network
error, a quota abort) can never masquerade as a capability regression — see
``benchmarks/CONTEXT.md`` and the coarse-gate anti-vacuity rule (P1.5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"


@dataclass(frozen=True)
class InstanceScore:
    """One instance's verdict. ``status`` is one of ``PASS``/``FAIL``/``ERROR``."""

    instance_id: str
    status: str
    detail: str | None = None


@dataclass
class ScoreReport:
    """A scorer's per-instance verdicts plus an optional free-form summary."""

    instances: list[InstanceScore]
    summary: dict = field(default_factory=dict)

    def counts(self) -> dict:
        """Tally PASS / FAIL / ERROR distinctly (ERROR never folded into FAIL)."""
        c = {"passed": 0, "failed": 0, "errored": 0, "total": len(self.instances)}
        for s in self.instances:
            if s.status == PASS:
                c["passed"] += 1
            elif s.status == ERROR:
                c["errored"] += 1
            else:
                c["failed"] += 1
        return c


@runtime_checkable
class Scorer(Protocol):
    """Score-side judge for one benchmark (e.g. sb-cli cloud scoring)."""

    name: str

    def score(
        self, predictions: Sequence[Mapping[str, Any]], *, run_id: str
    ) -> ScoreReport:
        """Score prediction records, returning per-instance verdicts + summary.

        Infrastructure failures (submission/network/quota) MUST be reported as
        ``ERROR`` instances, never ``FAIL``.
        """
        ...
