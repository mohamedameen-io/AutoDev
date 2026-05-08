"""v0.15.0 stuck-recovery escalation ladder.

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
0..2            no pivot threshold yet.
>=3             ``"SOFT_BLOCKER"`` — terminate task and hand off
                to the human; we have exhausted the autonomous
                escalation budget.
==============  ==============================================

When a discard threshold AND ``pivot_count >= 3`` both qualify, the
more terminal step (``"SOFT_BLOCKER"``) wins. This captures the
intuition that "we already pivoted three times and we are still
stuck — autonomous progress is no longer plausible."

Web search step (originally between PIVOT and SOFT_BLOCKER per
leo-lilinxiao) is deferred to v0.15.1 — see plan "DEFERRED" section.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# Public step labels returned by :func:`next_step`. The executor branches
# on this to choose the next dispatch path.
StuckStepLabel = Literal[
    "continue",
    "REFINE",
    "PIVOT",
    "SOFT_BLOCKER",
]


# Tunable thresholds. Kept module-private so tests + future calibration
# (v0.15.1) can override via monkeypatch without touching call sites.
_DISCARD_REFINE_THRESHOLD: int = 3
_DISCARD_PIVOT_THRESHOLD: int = 5
_PIVOT_SOFT_BLOCKER_THRESHOLD: int = 3


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
    last_event: str = ""


def next_step(state: StuckState) -> StuckStepLabel:
    """Return the ladder's next escalation step for ``state``.

    See module docstring for the threshold table. Pure function: does
    not mutate ``state``. Callers (typically
    :mod:`orchestrator.execute_phase`) branch on the return value:

    * ``"continue"`` — preserve legacy retry-then-escalate behavior.
    * ``"REFINE"`` — invoke critic_sounding_board with a STUCK_CONTEXT
      block, asking for a small adjustment. Bumps ``discard_count``.
    * ``"PIVOT"`` — invoke critic asking for a radical redirect.
      Bumps ``pivot_count``.
    * ``"SOFT_BLOCKER"`` — terminate the task with a human-decision
      handoff (no further autonomous escalation).
    """
    if state.pivot_count >= _PIVOT_SOFT_BLOCKER_THRESHOLD:
        return "SOFT_BLOCKER"
    if state.discard_count >= _DISCARD_PIVOT_THRESHOLD:
        return "PIVOT"
    if state.discard_count >= _DISCARD_REFINE_THRESHOLD:
        return "REFINE"
    return "continue"


__all__ = [
    "StuckState",
    "StuckStepLabel",
    "next_step",
]
