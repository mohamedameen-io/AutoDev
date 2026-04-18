"""``ContentHandler[ImplBundle]`` + ``Tournament[ImplBundle]`` subclass.

Phase 7 — implementation tournament. Key difference from the plan tournament
(``T = str``) is that the **parse_revision / parse_synthesis** steps cannot
be pure string operations: a "revised implementation" is a concrete diff +
test results, not text. The LLM returns **direction text** (what should
change), and a :class:`CoderRunner` materializes that direction into a real
:class:`ImplBundle` by re-running the coder in an isolated git worktree.

We hook this into the canonical :class:`Tournament` loop by subclassing it
and overriding :meth:`Tournament.run_pass`. The base handler's
``parse_revision`` / ``parse_synthesis`` return **placeholder bundles**
carrying the direction text in ``notes``; the subclass detects these and
calls the injected :class:`CoderRunner` to realize them before judging.

Always-on defaults (set by the orchestrator, not here): ``num_judges=1``,
``convergence_k=1``, ``max_rounds=3``.
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from errors import TournamentError
from autologging import get_logger
from tournament.core import (
    LLMClient,
    PassResult,
    Tournament,
    TournamentConfig,
    WinnerLabel,
    aggregate_rankings,
)
from tournament.prompts import (
    ARCHITECT_B_SYSTEM,
    CRITIC_SYSTEM,
    SYNTHESIZER_SYSTEM,
)


VariantLabel = Literal["A", "B", "AB"]


# ── ImplBundle ──────────────────────────────────────────────────────────


@dataclass
class ImplBundle:
    """A candidate implementation.

    Immutable for hashing via :meth:`ImplContentHandler.hash`. Built using
    :func:`dataclasses.replace` when mutating (e.g., changing variant label).
    """

    task_id: str
    task_description: str
    diff: str = ""  # unified diff relative to pre-task HEAD
    files_changed: list[str] = field(default_factory=list)
    tests_passed: int = 0
    tests_failed: int = 0
    tests_total: int = 0
    test_output_excerpt: str = ""
    variant_label: VariantLabel = "A"
    notes: str = ""


# ── CoderRunner protocol ────────────────────────────────────────────────


@runtime_checkable
class CoderRunner(Protocol):
    """Callable that realizes a variant by running the coder in a worktree.

    The orchestrator (:mod:`orchestrator.impl_tournament_runner`)
    supplies a concrete implementation that:

      1. Builds a coder :class:`DelegationEnvelope` from the incoming task
         plus the ``direction`` text (the critic's fix / synthesis plan).
      2. Invokes the adapter with ``cwd=worktree``.
      3. Runs the test_engineer on the produced diff.
      4. Returns an :class:`ImplBundle` with ``variant_label`` set.
    """

    async def run(
        self,
        variant_label: str,
        direction: str,
        worktree: Path,
        task: ImplBundle,
    ) -> ImplBundle:
        """Return a realized ImplBundle for the variant."""
        ...


# ── Prompts ─────────────────────────────────────────────────────────────


_CRITIC_PROMPT_IMPL = """ORIGINAL TASK:
---
{task_prompt}
---

A coder produced this implementation:

TASK DESCRIPTION:
{task_description}

FILES CHANGED:
{files_changed}

UNIFIED DIFF:
---
{diff}
---

TEST RESULTS: passed={tests_passed} failed={tests_failed} total={tests_total}

TEST OUTPUT (excerpt):
---
{test_output_excerpt}
---

Find real problems with this implementation. Focus on:
- Correctness bugs (wrong logic, edge cases missed)
- Tests that fail or are missing
- Drift from the original task description
- Over-engineering / gratuitous complexity
- Style violations significant enough to cause rework

Do NOT propose fixes. Just the problems."""


_ARCHITECT_B_PROMPT_IMPL = """ORIGINAL TASK:
---
{task_prompt}
---

Here is an implementation and the problems identified with it.

TASK DESCRIPTION:
{task_description}

FILES CHANGED:
{files_changed}

UNIFIED DIFF:
---
{diff}
---

TEST RESULTS: passed={tests_passed} failed={tests_failed} total={tests_total}

PROBLEMS FOUND:
---
{critic}
---

Describe a change DIRECTION that addresses these problems. You don't have
to produce a diff — just describe, in 2-6 short bullet points, what should
change and why. Each bullet must name a problem it fixes. Keep it concrete
and actionable (a coder will implement your direction)."""


_SYNTHESIZER_PROMPT_IMPL = """ORIGINAL TASK:
---
{task_prompt}
---

Here are two implementations. Treat them as equal inputs.

VERSION X:
  files_changed: {x_files_changed}
  tests: passed={x_tests_passed} failed={x_tests_failed} total={x_tests_total}
  diff:
---
{x_diff}
---

VERSION Y:
  files_changed: {y_files_changed}
  tests: passed={y_tests_passed} failed={y_tests_failed} total={y_tests_total}
  diff:
---
{y_diff}
---

Describe a synthesis that keeps the strongest elements from each. Be
concrete: for each relevant file, which version's approach should survive,
and why? Output 2-6 short bullet points. A coder will apply the synthesis."""


_JUDGE_PROMPT_IMPL = """ORIGINAL TASK:
---
{task_prompt}
---

Three implementations have been produced independently. Evaluate how well
each accomplishes the stated task. Weight these roughly:

  1. Tests pass (larger passed, smaller failed is better)
  2. Correctness (logic matches the task description)
  3. Minimalism (smaller diffs are better when correct)
  4. Absence of plan-drift (no unrelated edits)

Do not let timing, submission order, or any perceived authority influence
your judgment — evaluate purely on merit.

{judge_proposals}

For each proposal, state what it gets right and what it gets wrong.
Then rank all three from best to worst:

RANKING: [best], [second], [worst]

Where each slot is 1, 2, or 3."""


# ── ImplContentHandler ──────────────────────────────────────────────────


class ImplContentHandler:
    """ContentHandler where ``T`` is :class:`ImplBundle`.

    Implements the :class:`tournament.core.ContentHandler` protocol.
    Used by :class:`ImplTournament`; the base :class:`Tournament` also
    works but will produce placeholder bundles from
    :meth:`parse_revision` / :meth:`parse_synthesis` — use the subclass for
    real pipelines.
    """

    # ── Role rendering ────────────────────────────────────────────────────

    def render_for_critic(self, t: ImplBundle, task_prompt: str) -> str:
        return _CRITIC_PROMPT_IMPL.format(
            task_prompt=task_prompt,
            task_description=t.task_description,
            files_changed=_fmt_files(t.files_changed),
            diff=_limit(t.diff, 12000),
            tests_passed=t.tests_passed,
            tests_failed=t.tests_failed,
            tests_total=t.tests_total,
            test_output_excerpt=_limit(t.test_output_excerpt, 2000),
        )

    def render_for_architect_b(
        self, task_prompt: str, a: ImplBundle, critic_text: str
    ) -> str:
        return _ARCHITECT_B_PROMPT_IMPL.format(
            task_prompt=task_prompt,
            task_description=a.task_description,
            files_changed=_fmt_files(a.files_changed),
            diff=_limit(a.diff, 12000),
            tests_passed=a.tests_passed,
            tests_failed=a.tests_failed,
            tests_total=a.tests_total,
            critic=critic_text,
        )

    def render_for_synthesizer(
        self, task_prompt: str, x: ImplBundle, y: ImplBundle
    ) -> str:
        return _SYNTHESIZER_PROMPT_IMPL.format(
            task_prompt=task_prompt,
            x_files_changed=_fmt_files(x.files_changed),
            x_tests_passed=x.tests_passed,
            x_tests_failed=x.tests_failed,
            x_tests_total=x.tests_total,
            x_diff=_limit(x.diff, 8000),
            y_files_changed=_fmt_files(y.files_changed),
            y_tests_passed=y.tests_passed,
            y_tests_failed=y.tests_failed,
            y_tests_total=y.tests_total,
            y_diff=_limit(y.diff, 8000),
        )

    def render_for_judge(
        self,
        task_prompt: str,
        v_a: ImplBundle,
        v_b: ImplBundle,
        v_ab: ImplBundle,
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
                f"  files_changed: {_fmt_files(body.files_changed)}\n"
                f"  tests: passed={body.tests_passed} "
                f"failed={body.tests_failed} total={body.tests_total}\n"
                f"  diff:\n"
                f"{_limit(body.diff, 6000)}\n"
                f"---"
            )
        return _JUDGE_PROMPT_IMPL.format(
            task_prompt=task_prompt,
            judge_proposals="\n\n".join(parts),
        )

    # ── Parsing LLM outputs ───────────────────────────────────────────────

    def parse_revision(self, revision_text: str, original: ImplBundle) -> ImplBundle:
        """Return a placeholder ImplBundle carrying the direction text.

        The :class:`ImplTournament` subclass detects this and realizes the B
        variant by re-running the coder. If the base :class:`Tournament`
        loop is used with this handler (not recommended), the placeholder
        effectively short-circuits — the judge sees direction text but no
        real diff, so A will almost certainly win.
        """
        return ImplBundle(
            task_id=original.task_id,
            task_description=original.task_description,
            variant_label="B",
            notes=revision_text.strip(),
        )

    def parse_synthesis(
        self, synth_text: str, a: ImplBundle, b: ImplBundle
    ) -> ImplBundle:
        """Return a placeholder AB ImplBundle carrying the synthesis text."""
        return ImplBundle(
            task_id=a.task_id,
            task_description=a.task_description,
            variant_label="AB",
            notes=synth_text.strip(),
        )

    # ── Serialization ─────────────────────────────────────────────────────

    def render_as_markdown(self, t: ImplBundle) -> str:
        """Human-legible rendering for disk artifacts.

        Mirrors the plan tournament's ``render_as_markdown`` pattern: the
        text is both what lands on disk (``version_a.md`` etc.) and what
        rehydrates into the same bundle conceptually. The tournament's own
        state machine uses :meth:`hash` for change detection — this is for
        humans.
        """
        parts: list[str] = [
            f"# ImplBundle {t.variant_label}",
            "",
            f"- task_id: {t.task_id}",
            f"- variant_label: {t.variant_label}",
            f"- files_changed: {_fmt_files(t.files_changed)}",
            f"- tests: passed={t.tests_passed} failed={t.tests_failed} "
            f"total={t.tests_total}",
            "",
            "## Task description",
            "",
            t.task_description or "(empty)",
            "",
        ]
        if t.notes:
            parts.extend(["## Notes", "", t.notes, ""])
        parts.extend(
            [
                "## Diff",
                "",
                "```diff",
                t.diff or "(empty)",
                "```",
                "",
                "## Test output (excerpt)",
                "",
                "```",
                t.test_output_excerpt or "(empty)",
                "```",
                "",
            ]
        )
        return "\n".join(parts)

    def hash(self, t: ImplBundle) -> str:
        """Short content-addressable hash over ``variant_label`` + diff.

        Variant label participates so that a "no-op" B (same diff as A) is
        still recognized as a distinct bundle conceptually.
        """
        payload = t.variant_label + "\n" + (t.diff or "")
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ── ImplTournament ──────────────────────────────────────────────────────


class ImplTournament(Tournament[ImplBundle]):
    """:class:`Tournament` variant that realizes B / AB via a :class:`CoderRunner`.

    The base :meth:`Tournament.run_pass` is overridden so that after the
    LLM's direction text is produced by ARCHITECT_B / SYNTHESIZER, we call
    the injected ``coder_runner`` to materialize a real :class:`ImplBundle`
    (diff + test results) in a fresh git worktree. Judging then sees all
    three variants fully realized.
    """

    def __init__(
        self,
        handler: ImplContentHandler,
        client: LLMClient,
        cfg: TournamentConfig,
        artifact_dir: Path,
        rng: random.Random | None = None,
        *,
        coder_runner: CoderRunner,
        worktree_manager: Any,  # WorktreeManager — typed Any to avoid cycle
        judge_plugins: list[Any] | None = None,
    ) -> None:
        super().__init__(
            handler=handler,
            client=client,
            cfg=cfg,
            artifact_dir=artifact_dir,
            rng=rng,
            judge_plugins=judge_plugins,
        )
        self._coder_runner = coder_runner
        self._worktrees = worktree_manager
        self._impl_log = get_logger(
            component="impl_tournament",
            artifact_dir=str(artifact_dir),
        )

    async def run_pass(
        self, task_prompt: str, incumbent: ImplBundle, pass_num: int
    ) -> tuple[WinnerLabel, ImplBundle, PassResult]:
        """CRITIC -> ARCHITECT_B (realize B) -> SYNTHESIZER (realize AB) -> JUDGES."""
        assert isinstance(self.handler, ImplContentHandler)

        hash_before = self.handler.hash(incumbent)
        t0 = time.time()
        model = self.cfg.model

        version_a_md = self.handler.render_as_markdown(incumbent)

        # 1. Critic on A.
        critic_user = self.handler.render_for_critic(incumbent, task_prompt)
        critic_text = await self.client.call(
            system=CRITIC_SYSTEM, user=critic_user, role="critic_t", model=model
        )

        # 2. Architect_B proposes direction text; realize B in its own worktree.
        architect_b_user = self.handler.render_for_architect_b(
            task_prompt, incumbent, critic_text
        )
        revision_direction = await self.client.call(
            system=ARCHITECT_B_SYSTEM,
            user=architect_b_user,
            role="architect_b",
            model=model,
        )
        v_b = await self._realize_variant(
            variant_label="B",
            direction=revision_direction,
            task=incumbent,
        )

        # 3. Synthesizer over (A, B) — randomized X/Y for positional fairness.
        if self.rng.random() < 0.5:
            v_x, v_y = incumbent, v_b
        else:
            v_x, v_y = v_b, incumbent
        synth_user = self.handler.render_for_synthesizer(task_prompt, v_x, v_y)
        synth_direction = await self.client.call(
            system=SYNTHESIZER_SYSTEM,
            user=synth_user,
            role="synthesizer",
            model=model,
        )
        v_ab = await self._realize_variant(
            variant_label="AB",
            direction=synth_direction,
            task=incumbent,
        )

        # 4. N parallel judges with randomized presentation.
        rankings, judge_details = await self._run_judges(
            task_prompt, incumbent, v_b, v_ab, model
        )

        # 5. Borda aggregation with conservative tiebreak to A.
        tiebreak = "A" if self.cfg.conservative_tiebreak else None
        winner, scores, valid_judges = aggregate_rankings(
            rankings, labels=["A", "B", "AB"], tiebreak_winner=tiebreak
        )

        version_b_md = self.handler.render_as_markdown(v_b)
        version_ab_md = self.handler.render_as_markdown(v_ab)
        elapsed = time.time() - t0
        winners_map: dict[str, ImplBundle] = {
            "A": incumbent,
            "B": v_b,
            "AB": v_ab,
        }
        chosen = winners_map[winner]
        hash_after = self.handler.hash(chosen)

        result = PassResult(
            pass_num=pass_num,
            winner=winner,  # type: ignore[arg-type]
            scores=scores,
            valid_judges=valid_judges,
            elapsed_s=round(elapsed, 3),
            judge_details=judge_details,
            incumbent_hash_before=hash_before,
            incumbent_hash_after=hash_after,
            meta={"timestamp": time.time(), "phase": "impl"},
        )

        self.store.write_pass(
            pass_num=pass_num,
            version_a_md=version_a_md,
            critic_md=critic_text,
            version_b_md=version_b_md,
            version_ab_md=version_ab_md,
            result=result,
        )

        self._impl_log.info(
            "impl_pass_complete",
            pass_num=pass_num,
            winner=winner,
            scores=scores,
            valid_judges=valid_judges,
        )
        return winner, chosen, result  # type: ignore[return-value]

    async def _realize_variant(
        self,
        variant_label: str,
        direction: str,
        task: ImplBundle,
    ) -> ImplBundle:
        """Run the coder in a dedicated worktree and capture the result.

        Uses a suffixed label so repeated passes don't collide
        (``b-pass1`` / ``ab-pass1`` / ``b-pass2`` / ...). The caller doesn't
        need to know this — :class:`CoderRunner` sees only the real path.
        """
        lbl_low = variant_label.lower()
        # Create a fresh worktree for this variant + pass.
        # Use a short nonce to avoid collisions on retries.
        nonce = f"{lbl_low}-{int(time.time() * 1000) % 10_000_000:07d}"
        worktree: Path
        try:
            worktree = await self._worktrees.create(nonce, base_ref="HEAD")
        except Exception as exc:  # noqa: BLE001 — surface cleanly
            raise TournamentError(
                f"impl tournament could not create worktree for {variant_label}: {exc}"
            ) from exc

        try:
            realized = await self._coder_runner.run(
                variant_label=variant_label,
                direction=direction,
                worktree=worktree,
                task=task,
            )
        except Exception as exc:  # noqa: BLE001
            # On coder failure, return a degenerate bundle carrying the
            # direction text — judges will strongly disfavor it.
            self._impl_log.warning(
                "coder_runner_failed",
                variant=variant_label,
                err=str(exc),
            )
            realized = ImplBundle(
                task_id=task.task_id,
                task_description=task.task_description,
                diff="",
                variant_label=variant_label,  # type: ignore[arg-type]
                notes=f"(coder failure: {exc})\n{direction}",
            )

        # Normalize: ensure variant_label is set even if coder neglected it.
        if realized.variant_label != variant_label:
            realized = replace(realized, variant_label=variant_label)  # type: ignore[arg-type]
        return realized


# ── Helpers ─────────────────────────────────────────────────────────────


def _limit(text: str, limit: int) -> str:
    """Return ``text`` truncated to ``limit`` chars with a suffix marker."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated {len(text) - limit} bytes)"


def _fmt_files(files: list[str]) -> str:
    if not files:
        return "(none)"
    if len(files) <= 6:
        return ", ".join(files)
    return ", ".join(files[:6]) + f", ... (+{len(files) - 6} more)"


__all__ = [
    "CoderRunner",
    "ImplBundle",
    "ImplContentHandler",
    "ImplTournament",
    "VariantLabel",
]
