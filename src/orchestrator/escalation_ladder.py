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
from typing import Literal


# Public step labels returned by :func:`next_step`. The executor branches
# on this to choose the next dispatch path.
StuckStepLabel = Literal[
    "continue",
    "REFINE",
    "PIVOT",
    "WEB_SEARCH",
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
    * ``"WEB_SEARCH"`` — fetch top-3 search results and splice them as
      a ``WEB_CONTEXT:`` block into the next critic prompt. Bumps
      ``search_count``. v0.17.0 S2.
    * ``"SOFT_BLOCKER"`` — terminate the task with a human-decision
      handoff (no further autonomous escalation).
    """
    # SOFT_BLOCKER beats every other step. v0.17.0: also fires when the
    # web-search budget (3 per task) is exhausted — after three searches
    # the ladder concludes the task is genuinely beyond autonomous reach.
    if (
        state.pivot_count >= _PIVOT_SOFT_BLOCKER_THRESHOLD
        or state.search_count >= _SEARCH_COOLDOWN_CAP
    ):
        return "SOFT_BLOCKER"
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
        return "PIVOT"
    if state.discard_count >= _DISCARD_REFINE_THRESHOLD:
        return "REFINE"
    return "continue"


__all__ = [
    "StuckState",
    "StuckStepLabel",
    "next_step",
]
