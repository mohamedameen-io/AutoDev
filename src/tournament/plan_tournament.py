"""``ContentHandler[str]`` for plan-markdown refinement.

The plan tournament treats the incumbent (A) as an opaque markdown string.
Each pass runs the tournament loop:

  - **CRITIC** reads the plan and names structural / feasibility problems.
  - **ARCHITECT_B** revises the plan addressing the critique.
  - **SYNTHESIZER** picks best per-section from A and B.
  - **JUDGES** rank A / B / AB on task coverage, phase ordering, task
    granularity, acceptance-criterion concreteness and scope tightness.

Because ``T = str`` there is no structured parsing: the LLM returns a full
revised plan markdown, and this handler simply returns it as the new
incumbent. Richer per-section picking is Phase 7 territory.
"""

from __future__ import annotations

import hashlib
import re

from autologging import get_logger
from tournament.prompts import (
    ARCHITECT_B_PROMPT,
    CRITIC_PROMPT,
    JUDGE_RANK_3_PROMPT,
    SYNTHESIZER_PROMPT,
)
from tournament.util import _limit


logger = get_logger(__name__)


_PREAMBLE_HEADING = re.compile(r"^#\s+", flags=re.MULTILINE)


def _strip_preamble(text: str) -> str:
    """Slice ``text`` from the first ``# `` heading.

    Tournament authors (``architect_b``, ``synthesizer``) sometimes prepend
    commentary like "Here is the revised plan…" before the actual plan. That
    commentary must not contaminate the next pass's incumbent.

    Behavior:
        - leading whitespace is stripped first;
        - if the result starts with ``#``, return as-is;
        - otherwise scan for the first ``^# `` heading (single hash + ws) and
          slice from there;
        - if no such heading exists, return the stripped text and log
          ``preamble_strip_failed`` so the artifact surfaces the gap.
    """
    stripped = text.lstrip()
    if not stripped or stripped.startswith("#"):
        return stripped.rstrip()
    match = _PREAMBLE_HEADING.search(stripped)
    if match is None:
        logger.warning(
            "preamble_strip_failed",
            head_preview=stripped[:120],
        )
        return stripped.rstrip()
    return stripped[match.start() :].rstrip()


class PlanContentHandler:
    """ContentHandler where ``T`` is plan markdown (str).

    Implements the :class:`tournament.core.ContentHandler` protocol for
    ``T = str``. Intentionally stateless — one instance is safe to reuse across
    passes and across tournaments.
    """

    # ── Role rendering ─────────────────────────────────────────────────────

    def render_for_critic(self, t: str, task_prompt: str) -> str:
        """Render the critic prompt over incumbent ``t``.

        The canonical :data:`CRITIC_PROMPT` in :mod:`tournament.prompts`
        takes only ``version_a``; ``task_prompt`` is implicit (the critic does
        not need the original task to identify problems in the proposal) and is
        never embedded, so it needs no bound. The incumbent plan IS bounded —
        unbounded inlining of the full plan was the Run-5 ``critic_t
        error_max_turns`` root cause (see :mod:`tournament.util`).
        """
        return CRITIC_PROMPT.format(version_a=_limit(t, 12000))

    def render_for_architect_b(self, task_prompt: str, a: str, critic_text: str) -> str:
        """Render the architect_b prompt with task, incumbent A, and the critique.

        Both large inputs are bounded: the incumbent plan ``a`` and the LLM
        critique ``critic_text`` (an LLM critique can itself be large). The
        ``task_prompt`` is the enriched spec, which can also be large, so it is
        bounded too.
        """
        return ARCHITECT_B_PROMPT.format(
            task_prompt=_limit(task_prompt, 12000),
            version_a=_limit(a, 12000),
            critic=_limit(critic_text, 12000),
        )

    def render_for_synthesizer(self, task_prompt: str, x: str, y: str) -> str:
        """Render the synthesizer prompt over two equal-weight versions.

        The tournament engine coin-flips which of A or B becomes X / Y so the
        synthesizer has no positional bias. Both plan versions are bounded;
        ``task_prompt`` (the enriched spec) is bounded too — it can be large.
        """
        return SYNTHESIZER_PROMPT.format(
            task_prompt=_limit(task_prompt, 8000),
            version_x=_limit(x, 8000),
            version_y=_limit(y, 8000),
        )

    def render_for_judge(
        self,
        task_prompt: str,
        v_a: str,
        v_b: str,
        v_ab: str,
        order_map: dict[int, str],
    ) -> str:
        """Render the judge prompt with A / B / AB shuffled into display slots.

        ``order_map`` maps display-position (1..3) to canonical label
        ("A" | "B" | "AB"). We fill PROPOSAL slots 1/2/3 in that order so the
        judge cannot infer identity from position.
        """
        # Each plan version is bounded before it lands in a PROPOSAL slot;
        # ``task_prompt`` (the enriched spec) is bounded too — it can be large.
        versions = {
            "A": _limit(v_a, 8000),
            "B": _limit(v_b, 8000),
            "AB": _limit(v_ab, 8000),
        }
        parts: list[str] = []
        for slot in (1, 2, 3):
            label = order_map[slot]
            body = versions[label]
            parts.append(f"PROPOSAL {slot}:\n---\n{body}\n---")
        return JUDGE_RANK_3_PROMPT.format(
            task_prompt=_limit(task_prompt, 8000),
            judge_proposals="\n\n".join(parts),
        )

    # ── Parsing LLM outputs ────────────────────────────────────────────────

    def parse_revision(self, revision_text: str, original: str) -> str:
        """Extract the new plan markdown from author_b's response.

        Strips leading/trailing whitespace and any preamble before the first
        ``# `` heading. See :func:`_strip_preamble`.
        """
        return _strip_preamble(revision_text)

    def parse_synthesis(self, synth_text: str, a: str, b: str) -> str:
        """Extract the synthesized plan markdown from the synthesizer response.

        Strips leading/trailing whitespace and any preamble (e.g.
        "Looking at both versions, X is...") before the first ``# `` heading.
        See :func:`_strip_preamble`.
        """
        return _strip_preamble(synth_text)

    # ── Serialization ──────────────────────────────────────────────────────

    def render_as_markdown(self, t: str) -> str:
        """Identity — the incumbent already IS markdown."""
        return t

    def hash(self, t: str) -> str:
        """Return a short content-addressable hash for change detection.

        16 hex chars of SHA-256 matches the style used elsewhere in autodev
        (see :mod:`state.ledger`).
        """
        return hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]


__all__ = ["PlanContentHandler"]
