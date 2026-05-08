"""v0.18.0 B2: per-family + cross-family plateau detection.

A plateau is a structural condition where recent tournament passes are
stuck in a discard-heavy regime: many discards, no winner promotions.
When detected, the multi-branch dispatcher mutates one branch's lane to
``"distant-scout"`` so the cohort breaks out of the local minimum.

Two detection modes:

* **Per-family** (:meth:`PlateauDetector.detect_plateau`): walks the
  recent ``window`` events for a specific family and returns True when
  there are ``>=3`` events but no ``winner_promoted`` event in the
  window.
* **Cross-family** (:meth:`PlateauDetector.detect_cross_family_plateau`):
  walks the most recent ``window`` events across ALL families and
  returns True when zero ``winner_promoted`` events appear.

Force action: :meth:`PlateauDetector.force_distant_scout` mutates the
``branch_configs`` list — the first branch matching the plateaued
family is replaced with a copy whose lane is ``"distant-scout"``. When
no family-specific match exists, the first branch is forced. The
return value is the new (possibly identical) list; the original list
is NOT mutated in place.

Plateau detector is rule-based in v0.18.0; v0.20.0 will swap in a
statistical regression-based detector for both family + cross-family
modes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from config.schema import BranchConfig
    from state.knowledge import KnowledgeStore


class PlateauDetector:
    """Detect plateaus in the swarm-tier tournament event stream."""

    def __init__(self, knowledge: "KnowledgeStore") -> None:
        self.knowledge = knowledge

    async def detect_plateau(self, family: str, window: int = 4) -> bool:
        """True iff the recent ``window`` events for ``family`` show a plateau.

        Plateau condition: ``>= 3`` events in the window AND zero
        ``winner_promoted`` events. ``window`` is a count, not a time —
        we look at the trailing ``window`` events for that specific
        family in the swarm tier (most-recent-first read order).

        Returns False when:
            - fewer than 3 events for the family in the window;
            - any winner_promoted event appears in the window.
        """
        all_entries = await self.knowledge.read_all(tier="swarm")
        # Filter to the family.
        family_entries = [
            e for e in all_entries
            if e.metadata.get("family") == family
        ]
        # Sort by timestamp descending (most recent first).
        family_entries.sort(key=lambda e: e.timestamp, reverse=True)
        recent = family_entries[:window]

        if len(recent) < 3:
            return False

        for entry in recent:
            if entry.metadata.get("event_type") == "winner_promoted":
                return False
        return True

    async def detect_cross_family_plateau(self, window: int = 10) -> bool:
        """True iff the recent ``window`` events across all families plateau.

        Plateau condition: ``>= 3`` events AND zero ``winner_promoted``
        events in the window. Cross-family is a project-wide signal —
        when it fires, the entire multi-branch cohort is structurally
        stuck and a forced lane change is the appropriate intervention.
        """
        all_entries = await self.knowledge.read_all(tier="swarm")
        # Filter to entries that have an event_type (i.e. tournament events).
        tournament_entries = [
            e for e in all_entries
            if e.metadata.get("event_type") is not None
        ]
        tournament_entries.sort(key=lambda e: e.timestamp, reverse=True)
        recent = tournament_entries[:window]

        if len(recent) < 3:
            return False

        for entry in recent:
            if entry.metadata.get("event_type") == "winner_promoted":
                return False
        return True

    async def force_distant_scout(
        self,
        branch_configs: "list[BranchConfig]",
        plateaued_family: str | None = None,
    ) -> "list[BranchConfig]":
        """Replace one branch's lane with ``"distant-scout"`` and return the list.

        Selection priority:
            1. The first branch whose ``family == plateaued_family`` (when
               a family-specific plateau triggered the call).
            2. The first branch in the list (when no family match).

        The returned list is a new list (the input is NOT mutated). The
        replaced :class:`BranchConfig` carries every other field of the
        original; only ``lane`` changes to ``"distant-scout"``.
        """
        if not branch_configs:
            return []

        # Pick the index to mutate.
        target_idx = 0
        if plateaued_family is not None:
            for i, bc in enumerate(branch_configs):
                if bc.family == plateaued_family:
                    target_idx = i
                    break

        new_list = list(branch_configs)
        original = new_list[target_idx]
        new_list[target_idx] = original.model_copy(update={"lane": "distant-scout"})
        return new_list


__all__ = ["PlateauDetector"]
