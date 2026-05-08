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
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from plugins.registry import GateResult


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


def is_suspiciously_perfect(gate_results: list[GateResult]) -> bool:
    """Flag tournament gate cohorts that look "too clean to be real".

    Returns True when ALL gates passed AND any gate's ``details`` matches
    a suspicious-clean substring pattern. Empty cohorts are NOT suspicious
    (no signal at all is different from suspiciously clean signal).
    """
    if not _all_gates_passed(gate_results):
        return False
    for gate in gate_results:
        details = gate.details or ""
        for pat in _SUSPICIOUS_PATTERNS:
            if pat.search(details):
                return True
    return False


def decide(
    grade: str, gate_results: list[GateResult]
) -> PromotionDecision:
    """Pick the next ladder action for a tournament winner.

    Rules (evaluated in order):

      1. If ``grade`` is unknown → ``no_change`` (safe fallback).
      2. If any gate failed → ``no_change`` (don't ladder a broken winner).
      3. If ``grade == "dev_best"`` AND the cohort is suspicious-perfect →
         ``demand_repeat`` with a distinct ``reason``. Distinguished from the
         rule-based dev_best→demand_repeat below so telemetry can tell which
         path fired.
      4. ``dev_best`` (passed gates) → ``demand_repeat``.
      5. ``pending_repeat`` (passed gates) → ``promote_to_repeated``.
      6. ``repeated`` (passed gates) → ``promote_to_eligible``.
      7. Otherwise → ``no_change`` (e.g. ``promotion_eligible`` is a
         terminal state — the ladder doesn't go higher in v0.16.0).
    """
    if grade not in _VALID_GRADES:
        return PromotionDecision(
            action="no_change", reason=f"unknown grade: {grade!r}"
        )
    if not _all_gates_passed(gate_results):
        return PromotionDecision(
            action="no_change", reason="one or more gates failed"
        )

    # Rule 3: suspicious-perfect override applies only at the bottom of the
    # ladder. A ``pending_repeat`` is *already* the demanded repeat, so even
    # a suspicious cohort just rolls forward to ``repeated``.
    if grade == "dev_best" and is_suspiciously_perfect(gate_results):
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
