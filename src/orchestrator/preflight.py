"""v0.25.4: orchestrator preflight checks.

Raised before any tournament-firing code runs so the operator gets a
clear path forward instead of an AssertionError from inside a runner.

The runner-level guards (in ``plan_tournament_runner``,
``impl_tournament_runner``, ``phase_review_runner``) remain as
defense-in-depth and raise the same typed exception — they survive
``python -O`` where bare ``assert`` would be stripped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adapters.inline import InlineAdapter
from errors import TournamentAdapterMismatchError

if TYPE_CHECKING:
    from orchestrator import Orchestrator


def check_tournament_adapter_compatibility(orch: "Orchestrator") -> None:
    """Raise :class:`TournamentAdapterMismatchError` when
    :class:`InlineAdapter` is paired with any enabled tournament.

    Called from ``run_plan_phase`` and ``run_execute_phase`` at their
    entry points so the operator sees the typed error *before* any LLM
    call is made (vs. deep inside the tournament runner after the
    architect has already drafted a plan).
    """
    if not isinstance(orch.adapter, InlineAdapter):
        return
    enabled: list[str] = []
    if orch.cfg.tournaments.plan.enabled:
        enabled.append("plan")
    if orch.cfg.tournaments.impl.enabled:
        enabled.append("impl")
    if orch.cfg.tournaments.phase_review.enabled:
        enabled.append("phase_review")
    if enabled:
        raise TournamentAdapterMismatchError(enabled)
