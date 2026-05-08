"""Promotion-grade ladder for tournament winners (v0.16.0).

The Borda-winner-becomes-incumbent flow trusts a single tournament pass too
implicitly. The ladder demands a repeat-confirmation step before a winner is
treated as a real, durable improvement on the incumbent. Grade transitions:

    dev_best   →  (re-run on same candidate)   →  pending_repeat
    pending_repeat → (same candidate wins)     →  repeated
    repeated   →  (holdout-equivalent passes)  →  promotion_eligible

A separate "suspicious-perfect" heuristic flips ``dev_best → demand_repeat``
whenever ALL gates passed AND any gate's ``details`` smells like a
suspiciously clean signal ("0 errors", "100%", "no findings"). Suspicious-
clean output is the canonical pattern for tools that didn't actually run
(e.g. a lint script that exits 0 on a missing config file). The override is
distinct from the rule-based demand_repeat so ``reason`` carries the
"suspicious" marker for telemetry / post-hoc analysis.

The ladder is opt-in via ``cfg.tournaments.plan.promotion_grade_enabled``
(default off) — see :mod:`config.schema`.

v0.19.0 extensions:

  * Mutation-test integration. ``decide`` accepts an optional
    ``kill_rate``. When ``grade=='dev_best'`` and ``kill_rate < 0.5``,
    the ladder demands a repeat with a "test sufficiency questioned"
    reason, even when all gates passed.
  * ``is_suspiciously_perfect`` flags ``kill_rate=1.0`` on the gate
    cohort as suspicious — too-perfect mutation kill rates often
    indicate the test runner didn't actually exercise the mutated code.
  * Holdout integration (v0.19.0 C1). ``decide`` accepts an optional
    ``holdout_result``. When ``grade=='repeated'`` and the holdout
    failed, the ladder rejects promotion ("holdout test failed");
    success promotes to ``eligible`` as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from plugins.registry import GateResult

if TYPE_CHECKING:
    from tournament.holdout import HoldoutResult  # type: ignore[import-untyped]


PromotionAction = Literal[
    "promote_to_repeated",
    "demand_repeat",
    "promote_to_eligible",
    "no_change",
]


# Order matters for telemetry: the legacy ladder expects these in
# transition order. ``promotion_eligible`` is the final terminal state.
_VALID_GRADES = frozenset(
    {"dev_best", "pending_repeat", "repeated", "promotion_eligible"}
)


# Suspicious-clean signals. Each pattern fires on case-insensitive substring
# match in any gate's ``details``. The patterns are deliberately narrow —
# false positives here would block legitimate auto-promotions.
_SUSPICIOUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b0\s+errors?\b", re.IGNORECASE),
    re.compile(r"\b0\s+findings?\b", re.IGNORECASE),
    re.compile(r"\bno\s+findings?\b", re.IGNORECASE),
    # ``%`` is not a regex word boundary char, so we anchor on the digits
    # only and accept any trailing whitespace/% in the source text.
    re.compile(r"\b100\s*%", re.IGNORECASE),
)


@dataclass
class PromotionDecision:
    """One ladder verdict.

    Attributes:
        action: What the tournament loop should do — promote one rung,
            demand a repeat, or hold position.
        reason: Human-readable rationale. Carries the suspicious-perfect
            override marker so post-hoc telemetry can distinguish a rule-
            based ``demand_repeat`` from a heuristic-triggered one.
    """

    action: PromotionAction
    reason: str


def _all_gates_passed(gate_results: list[GateResult]) -> bool:
    """True iff every gate passed AND the cohort is non-empty."""
    if not gate_results:
        return False
    return all(g.passed for g in gate_results)


def is_suspiciously_perfect(
    gate_results: list[GateResult],
    kill_rate: float | None = None,
) -> bool:
    """Flag tournament gate cohorts that look "too clean to be real".

    Returns True when ALL gates passed AND any of:

      * Any gate's ``details`` matches a suspicious-clean substring
        pattern.
      * ``kill_rate`` is provided and equals 1.0 — perfect mutation
        coverage is rare and often indicates the runner didn't execute.

    Empty cohorts are NOT suspicious (no signal at all is different from
    suspiciously clean signal).
    """
    if not _all_gates_passed(gate_results):
        return False
    if kill_rate is not None and kill_rate >= 1.0:
        return True
    for gate in gate_results:
        details = gate.details or ""
        for pat in _SUSPICIOUS_PATTERNS:
            if pat.search(details):
                return True
    return False


def decide(
    grade: str,
    gate_results: list[GateResult],
    *,
    kill_rate: float | None = None,
    holdout_result: "HoldoutResult | None" = None,
) -> PromotionDecision:
    """Pick the next ladder action for a tournament winner.

    Rules (evaluated in order):

      1. If ``grade`` is unknown → ``no_change`` (safe fallback).
      2. If any gate failed → ``no_change`` (don't ladder a broken winner).
      3. v0.19.0: ``dev_best`` AND ``kill_rate < 0.5`` → ``demand_repeat``
         with "test sufficiency questioned" reason. Catches winners that
         passed gates but whose tests barely exercise the code.
      4. If ``grade == "dev_best"`` AND the cohort is suspicious-perfect
         → ``demand_repeat`` with a distinct ``reason``.
      5. ``dev_best`` (passed gates) → ``demand_repeat``.
      6. ``pending_repeat`` (passed gates) → ``promote_to_repeated``.
      7. v0.19.0: ``repeated`` AND holdout supplied AND ``passed=False``
         → ``no_change`` ("holdout test failed").
      8. ``repeated`` (passed gates, holdout absent or passed) →
         ``promote_to_eligible``.
      9. Otherwise → ``no_change`` (e.g. ``promotion_eligible`` is a
         terminal state).
    """
    if grade not in _VALID_GRADES:
        return PromotionDecision(
            action="no_change", reason=f"unknown grade: {grade!r}"
        )
    if not _all_gates_passed(gate_results):
        return PromotionDecision(
            action="no_change", reason="one or more gates failed"
        )

    # Rule 3 (v0.19.0): mutation-test sufficiency threshold.
    if grade == "dev_best" and kill_rate is not None and kill_rate < 0.5:
        return PromotionDecision(
            action="demand_repeat",
            reason=(
                f"test sufficiency questioned — mutation kill rate "
                f"{kill_rate:.2%} below 50%"
            ),
        )

    # Rule 4: suspicious-perfect override applies only at the bottom of the
    # ladder. A ``pending_repeat`` is *already* the demanded repeat.
    if grade == "dev_best" and is_suspiciously_perfect(
        gate_results, kill_rate=kill_rate
    ):
        return PromotionDecision(
            action="demand_repeat",
            reason="suspicious-perfect gates — demanding repeat for confirmation",
        )

    if grade == "dev_best":
        return PromotionDecision(
            action="demand_repeat",
            reason="dev_best winner — repeat to confirm before promotion",
        )
    if grade == "pending_repeat":
        return PromotionDecision(
            action="promote_to_repeated",
            reason="winner reproduced on repeat — promoted to repeated",
        )
    if grade == "repeated":
        # Rule 7 (v0.19.0): holdout-set evaluation.
        if holdout_result is not None and not holdout_result.passed:
            return PromotionDecision(
                action="no_change",
                reason=(
                    f"holdout test failed: {holdout_result.failure_count}/"
                    f"{holdout_result.test_count} failed"
                ),
            )
        return PromotionDecision(
            action="promote_to_eligible",
            reason="repeated winner cleared holdout — promoted to eligible",
        )

    # promotion_eligible (terminal) — nothing higher to climb.
    return PromotionDecision(
        action="no_change", reason=f"grade {grade!r} is terminal"
    )


__all__ = [
    "PromotionAction",
    "PromotionDecision",
    "decide",
    "is_suspiciously_perfect",
]
