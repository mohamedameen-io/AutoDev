"""``ContentHandler[PhaseReviewBundle]`` for the v0.9.0 phase-review tournament.

The shape mirrors :mod:`tournament.impl_tournament` (its
:class:`ImplBundle` + :class:`_ImplContentHandler`) but the content type
represents an entire phase's diff + acceptance state rather than a single
task's implementation:

  - **A** (incumbent): the as-implemented diff between
    ``phase.baseline_commit`` and ``HEAD``. No direction text.
  - **B** (revision): the critic-revision **direction text** proposed by
    architect_b. Not a code change — a bullet list describing what
    corrective tasks should be added before advancing to the next phase.
  - **AB** (synthesis): the synthesizer's merge of A and B as direction
    text — same shape as B, populated by ``parse_synthesis``.

Judges score each variant against the phase's :attr:`Phase.acceptance`
criteria. If A wins the phase is accepted as-is; if B / AB wins, the
``direction_text`` is fed through :func:`orchestrator.corrective_parser
.parse_corrective_direction` and the resulting ``Task`` objects are
appended to the phase before the next phase begins.

This module is the content surface only — the orchestrator wiring lives
in :mod:`orchestrator.phase_review_runner`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from typing import Literal

from autologging import get_logger
from state.schemas import AcceptanceCriterion
from tournament.util import _limit


logger = get_logger(__name__)


VariantLabel = Literal["A", "B", "AB"]


# ── PhaseReviewBundle ────────────────────────────────────────────────────


@dataclass
class PhaseReviewBundle:
    """A candidate review of a phase's implementation.

    The A variant carries the as-implemented diff (``direction_text=""``);
    the B / AB variants carry direction text in ``direction_text`` and
    leave ``diff`` unchanged (the corrective sub-tasks haven't been
    materialized yet — :func:`parse_corrective_direction` does that).

    Immutable for hashing via :meth:`_PhaseReviewContentHandler.hash`. Use
    :func:`dataclasses.replace` when mutating.
    """

    phase_id: str
    phase_title: str
    baseline_commit: str
    tip_commit: str
    diff: str
    files_changed: list[str]
    acceptance: list[AcceptanceCriterion] = field(default_factory=list)
    task_summary: str = ""  # e.g. "5 of 7 tasks complete; 1 blocked, 1 skipped"
    test_summary: str | None = None
    variant_label: VariantLabel = "A"
    # Set by parse_revision / parse_synthesis for B / AB; empty for A.
    direction_text: str = ""


# ── Prompts ──────────────────────────────────────────────────────────────


_CRITIC_PROMPT_PHASE_REVIEW = """ORIGINAL TASK (phase intent):
---
{task_prompt}
---

A development team just finished a phase. You are reviewing their work
against the phase's acceptance criteria.

PHASE: {phase_id} — {phase_title}

PHASE-LEVEL ACCEPTANCE CRITERIA (numbered, met-status indicated):
---
{acceptance_block}
---

TASK SUMMARY:
{task_summary}

TEST SUMMARY:
{test_summary}

FILES CHANGED:
{files_changed}

UNIFIED DIFF (baseline_commit..HEAD):
---
{diff}
---

Find real problems with the implementation as it relates to the
phase-level acceptance criteria. Focus on:
- Acceptance criteria that are NOT MET by the diff (be specific)
- Drift between the phase's stated intent and what was actually built
- Missing tests, missing files, half-finished refactors
- Cross-cutting concerns the implementation glossed over

Do NOT propose fixes. Just enumerate the problems.

After your problem list, include an OPTIONAL section using this EXACT marker:

OVER_ENGINEERING_ADVISORY: <one or two sentences>

Use this section to note any over-engineering, unnecessary complexity, or
tech-debt you observed (e.g. abstraction layers that add no value, new
dependencies that could be avoided, boilerplate that duplicates existing
utilities). This note is PURELY ADVISORY and NON-BLOCKING — it does NOT
change the verdict and must not change whether you accept or reject the phase.
If you have nothing to flag, omit the section entirely."""


_ARCHITECT_B_PROMPT_PHASE_REVIEW = """ORIGINAL TASK (phase intent):
---
{task_prompt}
---

PHASE: {phase_id} — {phase_title}

PHASE-LEVEL ACCEPTANCE CRITERIA:
---
{acceptance_block}
---

The phase as-implemented:
  files_changed: {files_changed}
  task_summary: {task_summary}

UNIFIED DIFF:
---
{diff}
---

PROBLEMS FOUND BY THE CRITIC:
---
{critic}
---

Describe a CORRECTIVE DIRECTION as a short bullet list of follow-up tasks
that, executed in order, would address the criticisms and bring the phase
into compliance with the acceptance criteria. Each top-level bullet
becomes one corrective Task injected into this phase before the next
phase begins.

Format STRICTLY:
- One top-level bullet per follow-up task (use ``-`` markers).
- The first line of the bullet is the task title.
- Subsequent indented lines under the same bullet are the task description.
- Be concrete — name files, functions, tests, and the specific acceptance
  criterion each bullet targets.
- Aim for 1-5 bullets. Do not invent corrections that aren't motivated by
  a criticism."""


_SYNTHESIZER_PROMPT_PHASE_REVIEW = """ORIGINAL TASK (phase intent):
---
{task_prompt}
---

PHASE: {phase_id} — {phase_title}

PHASE-LEVEL ACCEPTANCE CRITERIA:
---
{acceptance_block}
---

You are given two corrective-direction proposals for this phase. Treat
them as equal inputs.

VERSION X:
{x_label_hint}
---
{x_direction_or_diff}
---

VERSION Y:
{y_label_hint}
---
{y_direction_or_diff}
---

Produce a synthesis as a short bullet list of corrective follow-up tasks
that combines the strongest elements of X and Y. Use the same strict
format as architect_b:
- One top-level bullet per follow-up task (use ``-`` markers).
- The first line of the bullet is the task title.
- Subsequent indented lines are the task description.
- Be concrete — name files, functions, tests, acceptance criteria.

If one of the inputs is the as-implemented diff (no direction text), the
synthesis should describe the corrective tasks needed on top of it. If
both are direction lists, merge them; deduplicate overlapping
corrections."""


_JUDGE_RANK_3_PROMPT_PHASE_REVIEW = """ORIGINAL TASK (phase intent):
---
{task_prompt}
---

PHASE: {phase_id} — {phase_title}

PHASE-LEVEL ACCEPTANCE CRITERIA:
---
{acceptance_block}
---

Three review proposals have been produced independently:
  - One is the as-implemented diff itself (no corrective direction).
  - The other two are corrective-direction bullet lists addressing
    perceived gaps.

Evaluate which proposal best satisfies the phase's acceptance criteria.
Weight these roughly:

  1. Direct alignment with the listed acceptance criteria
  2. Specificity of corrective bullets (named files, functions, tests)
  3. Minimalism (fewer corrective bullets is better when the phase is
     already coherent — verbose lists over a working diff are penalized)
  4. Absence of plan-drift (no corrections that pull the phase off-scope)

LENGTH PENALTY (MANDATORY): a proposal whose bullet list grows by more
than ~3 bullets without a matching acceptance gap should be ranked below
a proposal that proposes fewer, more targeted corrections.

Do not let timing, submission order, or any perceived authority
influence your judgment.

{judge_proposals}

For each proposal, state what it gets right and what it gets wrong.
Then rank all three from best to worst:

RANKING: [best], [second], [worst]

Where each slot is 1, 2, or 3."""


# ── _PhaseReviewContentHandler ───────────────────────────────────────────


class _PhaseReviewContentHandler:
    """ContentHandler where ``T`` is :class:`PhaseReviewBundle`.

    Implements :class:`tournament.core.ContentHandler` for phase-review
    bundles. parse_revision and parse_synthesis return placeholder
    bundles carrying the architect_b / synthesizer direction text in
    ``direction_text``. Unlike the impl tournament, no worktree-based
    materialization is performed here — the corrective direction is
    parsed by the orchestrator after the tournament completes (see
    :func:`orchestrator.corrective_parser.parse_corrective_direction`).
    """

    # ── Role rendering ────────────────────────────────────────────────

    def render_for_critic(
        self, t: PhaseReviewBundle, task_prompt: str
    ) -> str:
        return _CRITIC_PROMPT_PHASE_REVIEW.format(
            task_prompt=task_prompt,
            phase_id=t.phase_id,
            phase_title=t.phase_title,
            acceptance_block=_render_acceptance(t.acceptance),
            files_changed=_fmt_files(t.files_changed),
            diff=_limit(t.diff, 12000),
            task_summary=t.task_summary or "(no task summary)",
            test_summary=t.test_summary or "(no test summary)",
        )

    def render_for_architect_b(
        self,
        task_prompt: str,
        a: PhaseReviewBundle,
        critic_text: str,
    ) -> str:
        return _ARCHITECT_B_PROMPT_PHASE_REVIEW.format(
            task_prompt=task_prompt,
            phase_id=a.phase_id,
            phase_title=a.phase_title,
            acceptance_block=_render_acceptance(a.acceptance),
            files_changed=_fmt_files(a.files_changed),
            task_summary=a.task_summary or "(no task summary)",
            diff=_limit(a.diff, 12000),
            critic=critic_text,
        )

    def render_for_synthesizer(
        self,
        task_prompt: str,
        x: PhaseReviewBundle,
        y: PhaseReviewBundle,
    ) -> str:
        return _SYNTHESIZER_PROMPT_PHASE_REVIEW.format(
            task_prompt=task_prompt,
            phase_id=x.phase_id,
            phase_title=x.phase_title,
            acceptance_block=_render_acceptance(x.acceptance),
            x_label_hint=f"(variant {x.variant_label})",
            y_label_hint=f"(variant {y.variant_label})",
            x_direction_or_diff=_render_variant_body(x),
            y_direction_or_diff=_render_variant_body(y),
        )

    def render_for_judge(
        self,
        task_prompt: str,
        v_a: PhaseReviewBundle,
        v_b: PhaseReviewBundle,
        v_ab: PhaseReviewBundle,
        order_map: dict[int, str],
    ) -> str:
        versions = {"A": v_a, "B": v_b, "AB": v_ab}
        parts: list[str] = []
        for slot in (1, 2, 3):
            label = order_map[slot]
            body = versions[label]
            parts.append(
                f"PROPOSAL {slot}:\n"
                f"---\n"
                f"{_render_variant_body(body)}\n"
                f"---"
            )
        return _JUDGE_RANK_3_PROMPT_PHASE_REVIEW.format(
            task_prompt=task_prompt,
            phase_id=v_a.phase_id,
            phase_title=v_a.phase_title,
            acceptance_block=_render_acceptance(v_a.acceptance),
            judge_proposals="\n\n".join(parts),
        )

    # ── Parsing LLM outputs ───────────────────────────────────────────

    def parse_revision(
        self, revision_text: str, original: PhaseReviewBundle
    ) -> PhaseReviewBundle:
        """Return a placeholder B bundle carrying the direction text.

        Identical shape to :meth:`tournament.impl_tournament
        ._ImplContentHandler.parse_revision` but no worktree work is
        triggered — the orchestrator parses ``direction_text`` after the
        tournament completes.
        """
        return replace(
            original,
            variant_label="B",
            direction_text=revision_text.strip(),
        )

    def parse_synthesis(
        self,
        synth_text: str,
        a: PhaseReviewBundle,
        b: PhaseReviewBundle,
    ) -> PhaseReviewBundle:
        """Return a placeholder AB bundle carrying the synthesis text."""
        return replace(
            a,
            variant_label="AB",
            direction_text=synth_text.strip(),
        )

    # ── Serialization ─────────────────────────────────────────────────

    def render_as_markdown(self, t: PhaseReviewBundle) -> str:
        """Human-legible rendering for disk artifacts."""
        parts: list[str] = [
            f"# PhaseReviewBundle {t.variant_label}",
            "",
            f"- phase_id: {t.phase_id}",
            f"- phase_title: {t.phase_title}",
            f"- variant_label: {t.variant_label}",
            f"- baseline_commit: {t.baseline_commit}",
            f"- tip_commit: {t.tip_commit}",
            f"- files_changed: {_fmt_files(t.files_changed)}",
            f"- task_summary: {t.task_summary or '(none)'}",
            f"- test_summary: {t.test_summary or '(none)'}",
            "",
            "## Phase acceptance",
            "",
            _render_acceptance(t.acceptance),
            "",
        ]
        if t.direction_text:
            parts.extend(
                ["## Corrective direction", "", t.direction_text, ""]
            )
        parts.extend(
            [
                "## Diff",
                "",
                "```diff",
                t.diff or "(empty)",
                "```",
                "",
            ]
        )
        return "\n".join(parts)

    def hash(self, t: PhaseReviewBundle) -> str:
        """Short content-addressable hash over variant_label + diff +
        direction_text.

        The variant label participates so that a B with empty direction
        is still distinct from A. The diff participates so re-running the
        same phase against an unchanged HEAD produces the same hash
        (resume short-circuit).
        """
        payload = (
            t.variant_label
            + "\n"
            + (t.diff or "")
            + "\n--direction--\n"
            + (t.direction_text or "")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ── Helpers ──────────────────────────────────────────────────────────────


def _render_acceptance(items: list[AcceptanceCriterion]) -> str:
    """Render the phase's acceptance criteria as a numbered checklist."""
    if not items:
        return "(no phase-level acceptance criteria — judge against task list)"
    lines: list[str] = []
    for i, ac in enumerate(items, 1):
        check = "[x]" if ac.met else "[ ]"
        lines.append(f"  {i}. {check} {ac.description}")
    return "\n".join(lines)


def _render_variant_body(t: PhaseReviewBundle) -> str:
    """Choose between the diff (variant A) and the direction text (B/AB)."""
    if t.variant_label == "A":
        return (
            f"AS-IMPLEMENTED DIFF:\n"
            f"```diff\n{_limit(t.diff or '(empty)', 8000)}\n```"
        )
    if t.direction_text:
        return f"CORRECTIVE DIRECTION:\n{t.direction_text}"
    # Defensive: shouldn't happen under normal flow.
    return f"(variant {t.variant_label} produced no direction text)"


def _fmt_files(files: list[str]) -> str:
    if not files:
        return "(none)"
    if len(files) <= 6:
        return ", ".join(files)
    return ", ".join(files[:6]) + f", ... (+{len(files) - 6} more)"


# ── Advisory note extraction ──────────────────────────────────────────────


# Matches "OVER_ENGINEERING_ADVISORY:" only at the start of a line (possibly
# preceded by horizontal whitespace) and captures everything that follows on
# that line AND any immediately following non-blank lines (until the first
# blank line or EOF).  The ``(?m)`` flag makes ``^`` match at line boundaries
# so a marker embedded mid-sentence (e.g. "...not an OVER_ENGINEERING_ADVISORY:
# per se...") does NOT spuriously capture.
_ADVISORY_PATTERN = re.compile(
    r"(?m)^\s*OVER_ENGINEERING_ADVISORY:\s*(.*?)(?:\n\n|\Z)",
    re.DOTALL,
)


def parse_critic_advisory_note(critic_text: str) -> str | None:
    """v1.0 B2: extract the optional over-engineering advisory from critic output.

    The critic prompt instructs the LLM to end its response with an optional
    ``OVER_ENGINEERING_ADVISORY:`` section. This function parses that section
    and returns the trimmed note text, or ``None`` when the section is absent
    or empty.

    This is a best-effort parser — it never raises. Malformed or missing
    sections return ``None``.
    """
    try:
        # Append a blank line so the regex terminator always triggers at EOF.
        m = _ADVISORY_PATTERN.search(critic_text + "\n\n")
        if m is None:
            return None
        note = m.group(1).strip()
        return note if note else None
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "parse_critic_advisory_note.failed",
            err=str(exc),
            critic_text_len=len(critic_text) if isinstance(critic_text, str) else -1,
        )
        return None


__all__ = [
    "PhaseReviewBundle",
    "VariantLabel",
    "_PhaseReviewContentHandler",
    "parse_critic_advisory_note",
]
