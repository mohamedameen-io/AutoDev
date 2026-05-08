"""v0.17.0 S4: repeated-hypothesis detector.

Walks ``discard`` :class:`~state.knowledge.TournamentEvent` lessons from
the past 14 days and checks bigram-Jaccard similarity between a candidate
hypothesis and each prior one. Returns True iff any past discard exceeds
``threshold`` (default 0.6 — same as
:attr:`config.schema.KnowledgeConfig.dedup_threshold`).

Advisory only: callers (the multi-branch dispatcher) tag a branch with
``metadata={"hypothesis_repeat": True}`` and emit a warning, but do NOT
block execution. Re-attempting a discarded approach is sometimes correct
(the prior failure may have been transient or context-specific). The
detector exists to give downstream judges + the future plateau detector
a structural signal they can weigh against other evidence.

Usage::

    detector = RepeatedHypothesisDetector(orch.knowledge)
    if await detector.is_repeat(branch_hypothesis, family="plan-tournament"):
        log.warning("repeating prior discarded hypothesis")
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from autologging import get_logger
from state.knowledge import (
    KnowledgeEntry,
    KnowledgeStore,
    jaccard_bigrams,
)


logger = get_logger(__name__)


# Look-back window for the recency filter. 14 days mirrors the v0.15.0
# lessons-decay floor — events older than this are treated as historical
# rather than "active prior art" worth flagging.
_LOOK_BACK_DAYS: int = 14


def _extract_hypothesis(text: str) -> str | None:
    """Pull the ``HYPOTHESIS:`` line value out of a lesson body.

    :class:`~state.knowledge.TournamentEvent.to_lesson_text` formats the
    body with line-anchored ``HYPOTHESIS: <value>``. We grep for that
    prefix and return the rest of the line trimmed. Returns ``None`` when
    the line is missing — older events without the field are silently
    skipped (no false positives).
    """
    for line in text.splitlines():
        if line.startswith("HYPOTHESIS:"):
            return line[len("HYPOTHESIS:"):].strip()
    return None


def _is_recent(entry: KnowledgeEntry, cutoff: datetime) -> bool:
    """True iff ``entry.timestamp`` parses and is at or after ``cutoff``."""
    try:
        ts = datetime.fromisoformat(entry.timestamp)
    except (ValueError, TypeError):
        return False
    if ts.tzinfo is None:
        # Treat naive timestamps as UTC — mirrors knowledge.py convention.
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= cutoff


class RepeatedHypothesisDetector:
    """Advisory check for "we've tried this before and it was discarded".

    Constructed once per orchestrator session over the shared
    :class:`KnowledgeStore`. Calls are cheap: each invocation walks the
    swarm tier (small relative to hive) once, filtering to recent
    discards, and computes :func:`jaccard_bigrams` against every match.

    Methods are async because :meth:`KnowledgeStore.read_all` is async.
    """

    def __init__(self, knowledge: KnowledgeStore) -> None:
        self._knowledge = knowledge

    async def is_repeat(
        self,
        hypothesis: str,
        *,
        family: str | None = None,
        threshold: float = 0.6,
    ) -> bool:
        """Return True iff a recent discard's hypothesis exceeds ``threshold``.

        Args:
            hypothesis: The candidate hypothesis text to check. Compared
                via bigram Jaccard against each past discard.
            family: Optional family filter (e.g. ``"plan-tournament"``).
                When set, only past discards whose
                :attr:`KnowledgeEntry.metadata['family']` matches are
                considered. ``None`` (default) considers every family —
                use this for project-wide repeat detection.
            threshold: Bigram Jaccard cutoff in [0, 1]. Defaults to 0.6
                so the detector matches the dedup threshold used
                elsewhere in :mod:`state.knowledge`. Higher values ⇒
                stricter (fewer repeats reported); lower values ⇒
                looser.

        Returns ``False`` (not raise) when the knowledge store is empty
        or unreachable — advisory checks must never block forward progress.
        """
        if not hypothesis.strip():
            return False

        try:
            entries = await self._knowledge.read_all(tier="swarm")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "repeat_detector.read_all_failed",
                error=str(exc),
            )
            return False

        cutoff = datetime.now(timezone.utc) - timedelta(days=_LOOK_BACK_DAYS)
        for entry in entries:
            if entry.metadata.get("event_type") != "discard":
                continue
            if family is not None and entry.metadata.get("family") != family:
                continue
            if not _is_recent(entry, cutoff):
                continue
            past_hyp = _extract_hypothesis(entry.text)
            if past_hyp is None:
                continue
            similarity = jaccard_bigrams(hypothesis, past_hyp)
            if similarity >= threshold:
                logger.info(
                    "repeat_detector.match",
                    similarity=round(similarity, 3),
                    family=family or "<any>",
                    entry_id=entry.id,
                )
                return True
        return False


__all__ = [
    "RepeatedHypothesisDetector",
]
