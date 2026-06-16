"""Shared text helpers for the tournament package.

``_limit`` used to live as byte-identical copies in :mod:`tournament.phase_review`
and :mod:`tournament.impl_tournament`. v0.42.1 (F2/A4) hoists it here so the plan
tournament (:mod:`tournament.plan_tournament`) can apply the *same* proven
truncation to its render inputs — the unbounded plan text fed to critic /
architect_b / synthesizer / judge was the root cause of the Run-5 ``critic_t
error_max_turns`` exhaustion (190K–262K-token reads).
"""

from __future__ import annotations


def _limit(text: str, limit: int) -> str:
    """Return ``text`` truncated to ``limit`` chars with a suffix marker."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated {len(text) - limit} bytes)"
