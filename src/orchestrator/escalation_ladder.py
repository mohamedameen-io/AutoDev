"""v0.15.0 stuck-recovery escalation ladder (v0.17.0 adds WEB_SEARCH step).

A graduated response to repeated discard / pivot signals on the same
task. The ladder consults the per-task :class:`StuckState` (held in
in-memory PlanManager state, mirroring v0.11.0's ``_in_flight`` pattern)
and returns the next escalation step the executor should take.

Thresholds (rationale in the v0.15.0 plan section "Risks + rollback"):

==============  ==============================================
discard_count   meaning
==============  ==============================================
0..2            ordinary retry — ``next_step()`` returns ``"continue"``.
3..4            ``"REFINE"`` — invoke ``critic_sounding_board`` in
                STUCK RECOVERY MODE asking for a *small adjustment*.
>=5             ``"PIVOT"`` — invoke critic asking for a *radical*
                redirect.
==============  ==============================================

==============  ==============================================
pivot_count     meaning
==============  ==============================================
0..1            no pivot threshold yet.
>=2 + search_count<3   ``"WEB_SEARCH"`` — fetch top-3 results and
                splice as ``WEB_CONTEXT:`` block into the next critic
                prompt (v0.17.0 S2). Capped at 3 searches per task.
>=3 OR search_count>=3 ``"SOFT_BLOCKER"`` — terminate task and hand
                off to the human; we have exhausted the autonomous
                escalation budget.
==============  ==============================================

When a discard threshold AND a pivot threshold both qualify, the
more terminal step wins. This captures the intuition that "we already
pivoted multiple times and we are still stuck — autonomous progress is
no longer plausible."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from autologging import get_logger


logger = get_logger(__name__)


# Public step labels returned by :func:`next_step`. The executor branches
# on this to choose the next dispatch path.
StuckStepLabel = Literal[
    "continue",
    "REFINE",
    "PIVOT",
    "WEB_SEARCH",
    "ARCHITECT_CONSULT",
    "SOFT_BLOCKER",
]


# Tunable thresholds. Kept module-private so tests + future calibration
# (v0.15.1) can override via monkeypatch without touching call sites.
_DISCARD_REFINE_THRESHOLD: int = 3
_DISCARD_PIVOT_THRESHOLD: int = 5
# v0.17.0 S2: ``WEB_SEARCH`` activates at pivot_count >= 2 (one rung
# below SOFT_BLOCKER). The cap of 3 searches per task lives on
# :class:`StuckState.search_count` and is enforced in :func:`next_step`.
_PIVOT_WEB_SEARCH_THRESHOLD: int = 2
_PIVOT_SOFT_BLOCKER_THRESHOLD: int = 3
_SEARCH_COOLDOWN_CAP: int = 3
# v0.26.1 patch G: one-shot architect consult rung. When the autonomous
# search budget is exhausted (``search_count >= _SEARCH_ARCHITECT_THRESHOLD``)
# and the architect has not yet been consulted on this task
# (``architect_count < _ARCHITECT_SOFT_BLOCKER_THRESHOLD``), the ladder
# routes to ``ARCHITECT_CONSULT`` for a final structured intervention.
# After the architect's advice has been applied (``architect_count >= 1``)
# the next escalation falls through to SOFT_BLOCKER. This mirrors human-
# team behavior: junior dev struggles → asks the senior who designed the
# plan → applies their guidance → escalates to human if still stuck.
_SEARCH_ARCHITECT_THRESHOLD: int = 3
_ARCHITECT_SOFT_BLOCKER_THRESHOLD: int = 1


@dataclass
class StuckState:
    """In-memory per-task state tracked across escalations.

    Mirrors v0.11.0's ``PlanManager._in_flight`` pattern: held entirely
    in memory (NOT persisted to ``plan.json`` or the ledger) because a
    crash mid-flight should restart the task from a clean state. The
    cross-run lessons memory captures the *what was learned* signal;
    this struct captures the *how stuck are we right now* signal.

    Attributes:
        discard_count: Number of consecutive discards observed for the
            task (a discard is any failure that the conflict-/retry-
            escalation loop counts toward stuck progress).
        pivot_count: Number of times the ladder has dispatched a PIVOT
            critic invocation for the task. Pivots are stronger than
            discards — three of them mark the task as soft-blocker.
        last_event: Free-form label describing the most recent
            transition (e.g. ``"discard"``, ``"pivot"``, ``"refine"``).
            Used for forensics + ledger payloads; does NOT affect
            :func:`next_step`.
    """

    discard_count: int = 0
    pivot_count: int = 0
    # v0.17.0 S2: how many web searches have fired for this task across
    # the ladder's lifetime. Capped at 3 (see :data:`_SEARCH_COOLDOWN_CAP`)
    # to prevent runaway HTTP traffic on pathological cases.
    search_count: int = 0
    # v0.17.0 S2: ladder iteration index of the last search dispatch.
    # Used for the 2-iteration cooldown (the ladder won't return
    # ``WEB_SEARCH`` twice in a row).
    last_search_iter: int = 0
    # v0.26.1 patch G: number of times the ARCHITECT_CONSULT rung has
    # fired for this task. Threshold is 1 — the rung is one-shot. Once
    # the architect has weighed in, subsequent escalations fall through
    # to SOFT_BLOCKER. Persisted via :meth:`PlanManager.increment_architect_consult`.
    architect_count: int = 0
    last_event: str = ""


def next_step(
    state: StuckState,
    knowledge_context: dict[str, Any] | None = None,
) -> StuckStepLabel:
    """Return the ladder's next escalation step for ``state``.

    See module docstring for the threshold table. Pure function: does
    not mutate ``state``. Callers (typically
    :mod:`orchestrator.execute_phase`) branch on the return value:

    * ``"continue"`` — preserve legacy retry-then-escalate behavior.
    * ``"REFINE"`` — invoke critic_sounding_board with a STUCK_CONTEXT
      block, asking for a small adjustment. Bumps ``discard_count``.
    * ``"PIVOT"`` — invoke critic asking for a radical redirect.
      Bumps ``pivot_count``.
    * ``"WEB_SEARCH"`` — fetch top-3 search results and splice them as
      a ``WEB_CONTEXT:`` block into the next critic prompt. Bumps
      ``search_count``. v0.17.0 S2.
    * ``"ARCHITECT_CONSULT"`` — v0.26.1 patch G. One-shot per task. Fires
      when the web-search budget is exhausted (``search_count >= 3``) AND
      the architect hasn't been consulted yet (``architect_count < 1``).
    * ``"SOFT_BLOCKER"`` — terminate the task with a human-decision
      handoff (no further autonomous escalation).

    v0.32.0 Phase 4.1: ``knowledge_context`` is an optional dict carrying
    PRM-detected pattern names under the ``"detected_patterns"`` key.
    When ``"repetition_loop"`` is detected AND the ladder would otherwise
    return ``"REFINE"`` (or ``"continue"``), the result is escalated to
    ``"PIVOT"`` — repeating the same edit on the same files is the exact
    failure mode that REFINE cannot fix. When ``"ping_pong"`` is detected
    AND the ladder would otherwise return ``"REFINE"`` or ``"PIVOT"``,
    the result is escalated to ``"ARCHITECT_CONSULT"`` (assuming
    architect_count < 1) or ``"SOFT_BLOCKER"`` otherwise — alternating
    between two targets is structural confusion the architect is best
    positioned to resolve. When ``knowledge_context`` is None or carries
    no recognised patterns, behaviour is byte-identical to the
    pre-Phase-4 ladder.
    """
    # SOFT_BLOCKER beats every other step. v0.26.1 patch G: also fires
    # once the architect has been consulted (architect_count >= 1) —
    # the one-shot rung's exit path. The legacy v0.17.0 condition
    # ``search_count >= _SEARCH_COOLDOWN_CAP`` no longer routes directly
    # to SOFT_BLOCKER; it routes through ARCHITECT_CONSULT first.
    if (
        state.pivot_count >= _PIVOT_SOFT_BLOCKER_THRESHOLD
        or state.architect_count >= _ARCHITECT_SOFT_BLOCKER_THRESHOLD
    ):
        return "SOFT_BLOCKER"
    # v0.26.1 patch G: ARCHITECT_CONSULT — the web-search budget is
    # exhausted and the architect hasn't weighed in yet. One-shot.
    if (
        state.search_count >= _SEARCH_ARCHITECT_THRESHOLD
        and state.architect_count < _ARCHITECT_SOFT_BLOCKER_THRESHOLD
    ):
        return "ARCHITECT_CONSULT"
    # WEB_SEARCH: pivot_count just past the threshold AND budget remaining.
    # v0.17.0 S2 — opt-in via cfg.web_search_enabled at the executor's
    # dispatch site; the ladder itself unconditionally returns the label
    # so callers can gate behaviour without re-implementing the threshold.
    if (
        state.pivot_count >= _PIVOT_WEB_SEARCH_THRESHOLD
        and state.search_count < _SEARCH_COOLDOWN_CAP
    ):
        return "WEB_SEARCH"
    if state.discard_count >= _DISCARD_PIVOT_THRESHOLD:
        baseline: StuckStepLabel = "PIVOT"
    elif state.discard_count >= _DISCARD_REFINE_THRESHOLD:
        baseline = "REFINE"
    else:
        baseline = "continue"

    # v0.32.0 Phase 4.1: PRM-pattern-aware overrides. The detector in
    # :mod:`orchestrator.prm` already fires `repetition_loop` when the
    # same (role, action, target_files) triple appears 3× in a row and
    # `ping_pong` when two targets alternate 4×. Either pattern means
    # the cheap REFINE path will not produce a different outcome — we
    # have empirical evidence that the agent is stuck in a fixed point.
    detected = _detected_patterns(knowledge_context)
    if not detected:
        return baseline

    if "repetition_loop" in detected and baseline in ("continue", "REFINE"):
        logger.info(
            "escalation_ladder.repetition_loop_overrides_refine_to_pivot",
            discard_count=state.discard_count,
            pivot_count=state.pivot_count,
            baseline=baseline,
        )
        return "PIVOT"

    if "ping_pong" in detected and baseline in ("REFINE", "PIVOT"):
        # ping_pong means the agent is alternating between two targets —
        # the structural decision is the bug, not the local edit. Skip
        # the developer-facing rungs and route to the architect (if not
        # already consulted) or the human (if architect already weighed in).
        if state.architect_count < _ARCHITECT_SOFT_BLOCKER_THRESHOLD:
            logger.info(
                "escalation_ladder.ping_pong_escalates_to_architect_consult",
                discard_count=state.discard_count,
                pivot_count=state.pivot_count,
                baseline=baseline,
            )
            return "ARCHITECT_CONSULT"
        logger.info(
            "escalation_ladder.ping_pong_escalates_to_soft_blocker",
            discard_count=state.discard_count,
            pivot_count=state.pivot_count,
            baseline=baseline,
        )
        return "SOFT_BLOCKER"

    return baseline


def _detected_patterns(
    knowledge_context: dict[str, Any] | None,
) -> set[str]:
    """Extract the ``detected_patterns`` set from ``knowledge_context``.

    Returns an empty set on None / missing / malformed input — the
    override paths above degrade silently to the pre-Phase-4 ladder so
    a misshapen caller never crashes the retry loop.
    """
    if not knowledge_context:
        return set()
    raw = knowledge_context.get("detected_patterns")
    if raw is None:
        return set()
    try:
        return {str(p) for p in raw}
    except TypeError:
        return set()


__all__ = [
    "StuckState",
    "StuckStepLabel",
    "next_step",
]
