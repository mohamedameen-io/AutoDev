"""v0.18.0: pluggable voting strategies for the tournament Borda step.

The tournament's judge-aggregation step is now strategy-pluggable so that
different aggregation policies (council/veto, Borda, future weighted variants)
can be swapped in without rewriting :class:`tournament.core.Tournament`.

Strategies:
    * :class:`BordaAggregator` — extracted byte-identical from the legacy
      :func:`tournament.core.aggregate_rankings`. Default for plan / impl /
      phase-review tournaments. Behavioral parity with v0.17.0 and earlier
      is enforced by the regression suite in
      ``tests/test_tournament_voting.py``.
    * :class:`VetoAggregator` — council/veto policy. Each judge's
      first-place vote is treated as ``APPROVE`` and last-place as
      ``REJECT``. If any candidate is rejected by any judge it is
      vetoed; the tiebreak_winner (default ``"A"``) wins. When no veto
      fires, the result falls through to a Borda tally over the same
      rankings.

The :class:`VotingStrategy` protocol is structurally typed (PEP 544) so
existing callers can adopt the strategy interface without modifying their
class hierarchy.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class VotingStrategy(Protocol):
    """Structural protocol for tournament vote aggregation strategies.

    Implementations must accept a list of judge rankings (each a list of
    canonical labels in best→worst order, or ``None`` for parse failures)
    and return ``(winner_label, scores_dict, n_valid_judges)``.

    The contract mirrors :func:`tournament.core.aggregate_rankings` so
    strategies can be substituted at the call site without disturbing
    downstream Borda forensics.
    """

    def aggregate(
        self,
        rankings: list[list[str] | None],
        labels: list[str] | None = None,
        tiebreak_winner: str | None = "A",
    ) -> tuple[str, dict[str, int], int]: ...


class BordaAggregator:
    """Borda count aggregation, byte-identical to the legacy implementation.

    Extracted from :func:`tournament.core.aggregate_rankings` (lines 329-359
    of v0.17.0). The output of this class's :meth:`aggregate` MUST match
    the output of the legacy function for every input — see
    ``tests/test_tournament_voting.py::test_borda_aggregator_byte_identical_to_legacy``
    for the regression invariant.

    Algorithm:
        For each label in position ``p`` of a judge's ranking, add
        ``(n - p)`` points where ``n = len(labels)``. Sort labels by
        descending score; tiebreak by:

        * ``tiebreak_winner != None`` → that label gets priority 0
          (highest), every other label gets ``1 + index_in_labels``.
        * ``tiebreak_winner is None`` → labels get priority equal to
          their position in ``labels`` (first-listed wins ties).

    Conservative-tiebreak default ``"A"`` gives the incumbent priority
    over B / AB on a perfect tie, matching the v0.5.0+ behavior.
    """

    def aggregate(
        self,
        rankings: list[list[str] | None],
        labels: list[str] | None = None,
        tiebreak_winner: str | None = "A",
    ) -> tuple[str, dict[str, int], int]:
        if labels is None:
            labels = ["A", "B", "AB"]
        scores: dict[str, int] = {label: 0 for label in labels}
        n = len(labels)
        valid = [r for r in rankings if r is not None]
        for ranking in valid:
            for pos, label in enumerate(ranking):
                if label in scores and pos < n:
                    scores[label] += n - pos
        if tiebreak_winner:
            priority = {
                label: (0 if label == tiebreak_winner else i + 1)
                for i, label in enumerate(labels)
            }
        else:
            priority = {label: i for i, label in enumerate(labels)}
        ranked = sorted(scores.keys(), key=lambda k: (-scores[k], priority[k]))
        return ranked[0], scores, len(valid)


class VetoAggregator:
    """Council/veto strategy: any judge's last-place vote vetoes that candidate.

    Decision flow:

    1. Identify ``vetoed = {labels that any judge placed in their last slot}``.
       A judge with a ``None`` ranking contributes no veto.
    2. If ``vetoed`` covers every label (everyone vetoes everyone), or
       leaves no surviving candidate, return the ``tiebreak_winner`` (or
       the first label if ``tiebreak_winner is None``) with synthetic
       all-zero scores tagged ``"_veto"``.
    3. If ``vetoed`` is non-empty but at least one label survives, run a
       Borda tally over the original rankings, then sift winners through
       the surviving set: the highest-scoring un-vetoed label wins. If
       the tiebreak_winner is among the survivors and ties for top
       Borda score, it wins.
    4. If ``vetoed`` is empty (no judge ranked anyone last — ie every
       judge had a complete ranking with the candidate in slot ≥ 2), the
       result falls through entirely to Borda.

    v0.18.0 C2 will optionally consume ``criteria`` (a list of
    :class:`state.schemas.AcceptanceCriterion`) to attribute per-criterion
    veto reasoning. v0.18.0 C1 ships with the criteria parameter
    plumbed but unused — full per-criterion vote tracking lands in C2.
    """

    def __init__(
        self,
        criteria: list[object] | None = None,
    ) -> None:
        # ``criteria`` is opt-in metadata used by C2's per-criterion vote
        # tracking. When None (default), the veto agg behaves as a pure
        # last-place-rejects-candidate policy without per-criterion
        # bookkeeping.
        self.criteria = criteria

    def aggregate(
        self,
        rankings: list[list[str] | None],
        labels: list[str] | None = None,
        tiebreak_winner: str | None = "A",
    ) -> tuple[str, dict[str, int], int]:
        if labels is None:
            labels = ["A", "B", "AB"]

        valid = [r for r in rankings if r is not None]

        # Identify vetoed candidates: any candidate in a judge's last slot.
        vetoed: set[str] = set()
        for ranking in valid:
            if not ranking:
                continue
            last = ranking[-1]
            if last in labels:
                vetoed.add(last)

        # No veto fires → fall through to Borda directly.
        if not vetoed:
            return BordaAggregator().aggregate(
                rankings, labels=labels, tiebreak_winner=tiebreak_winner
            )

        survivors = [label for label in labels if label not in vetoed]

        # Synthetic scores tag the veto outcome for forensics. The
        # tournament artifact persists this dict so post-hoc analysis can
        # see *which* labels were vetoed by reading the all-zero entries
        # alongside the chosen winner.
        synthetic_scores: dict[str, int] = {label: 0 for label in labels}

        if not survivors:
            # Total veto: every label rejected. Pick the tiebreak fallback
            # (or first label) and return synthetic scores.
            fallback = (
                tiebreak_winner if tiebreak_winner is not None else labels[0]
            )
            return fallback, synthetic_scores, len(valid)

        # Some survivors remain. Run Borda over the full ranking set, then
        # restrict to survivors. The winner is the highest-Borda survivor;
        # tiebreak_winner among survivors keeps priority on ties.
        borda_winner, borda_scores, n_valid = BordaAggregator().aggregate(
            rankings, labels=labels, tiebreak_winner=tiebreak_winner
        )

        if borda_winner in survivors:
            return borda_winner, borda_scores, n_valid

        # Borda winner was vetoed. Pick the highest-scoring survivor; on
        # ties prefer ``tiebreak_winner`` if it survived, else first
        # survivor in label order.
        max_surv_score = max(borda_scores[s] for s in survivors)
        top_survivors = [s for s in survivors if borda_scores[s] == max_surv_score]
        if tiebreak_winner in top_survivors:
            chosen = tiebreak_winner
        else:
            chosen = top_survivors[0]
        return chosen, borda_scores, n_valid


__all__ = [
    "BordaAggregator",
    "VetoAggregator",
    "VotingStrategy",
]
