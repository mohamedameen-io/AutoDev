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

from tournament.prompts import (
    ARCHITECT_B_PROMPT,
    CRITIC_PROMPT,
    JUDGE_RANK_3_PROMPT,
    SYNTHESIZER_PROMPT,
)


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
        not need the original task to identify problems in the proposal).
        """
        return CRITIC_PROMPT.format(version_a=t)

    def render_for_architect_b(self, task_prompt: str, a: str, critic_text: str) -> str:
        """Render the architect_b prompt with task, incumbent A, and the critique."""
        return ARCHITECT_B_PROMPT.format(
            task_prompt=task_prompt, version_a=a, critic=critic_text
        )

    def render_for_synthesizer(self, task_prompt: str, x: str, y: str) -> str:
        """Render the synthesizer prompt over two equal-weight versions.

        The tournament engine coin-flips which of A or B becomes X / Y so the
        synthesizer has no positional bias.
        """
        return SYNTHESIZER_PROMPT.format(
            task_prompt=task_prompt, version_x=x, version_y=y
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
        versions = {"A": v_a, "B": v_b, "AB": v_ab}
        parts: list[str] = []
        for slot in (1, 2, 3):
            label = order_map[slot]
            body = versions[label]
            parts.append(f"PROPOSAL {slot}:\n---\n{body}\n---")
        return JUDGE_RANK_3_PROMPT.format(
            task_prompt=task_prompt,
            judge_proposals="\n\n".join(parts),
        )

    # ── Parsing LLM outputs ────────────────────────────────────────────────

    def parse_revision(self, revision_text: str, original: str) -> str:
        """Extract the new plan markdown from author_b's response.

        The prompt asks for a full rewritten plan. We trim leading / trailing
        whitespace only; any richer section-level parsing lives in Phase 7's
        structured ``ImplBundle`` handler.
        """
        return revision_text.strip()

    def parse_synthesis(self, synth_text: str, a: str, b: str) -> str:
        """Extract the synthesized plan markdown from the synthesizer response."""
        return synth_text.strip()

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
