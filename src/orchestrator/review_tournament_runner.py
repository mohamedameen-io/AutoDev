"""Review-tournament runner — autoreason A/B/AB pipeline for the reviewer step.

v0.32.0 Phase 2: replaces the legacy single-shot
``delegate(orch, "reviewer", review_env, ...)`` call with a three-candidate
tournament:

  * **A**: the unchanged developer patch + original reviewer verdict.
  * **B**: an *adversarial* second-opinion review produced by the
    ``adversarial_reviewer`` role — deliberately framed to find the
    angle the original reviewer missed.
  * **AB**: a synthesis produced by the ``merge_synthesizer`` role
    that combines A's strengths with B's improvements.

Then FRESH judge agents (no shared context with the three reviewers)
score all three blindly via Borda count. "Do nothing" (A wins) is a
first-class verdict — the loop converges on "the original was fine,
stop" after ``convergence_k=2`` consecutive A wins.

The loop's three exit paths:

1. **A wins ``convergence_k`` times in a row** — original verdict
   stands; the orchestrator advances or soft-blocks WITHOUT another
   developer-refine cycle. Emits ``review_tournament_converged``.
2. **B or AB wins** — extract winning verdict; the orchestrator
   invokes the developer ONCE MORE with the winning issues and exits
   the tournament (no nested tournaments). Emits
   ``review_tournament_judged`` for the winning round and exits.
3. **``max_rounds`` without convergence** — escalate to the existing
   ``critic_sounding_board`` rung. Emits
   ``review_tournament_escalated``.

This mirrors the shape of :mod:`orchestrator.impl_tournament_runner`
but is DELIBERATELY simpler: there is no worktree fan-out, no diff
parsing, no per-pass on-disk checkpointing. The artifact tree is
``.autodev/tournaments/review-{8hex}/`` with one JSON sidecar per
round listing the candidates + judge details.

v0.31.0 instrumentation preserved by construction:

* Each candidate (A, B, AB) is grounded against the SAME chunked
  review envelope (Phase 1.4).
* Each judge's verdict parses through
  :func:`orchestrator.execute_phase._parse_review_verdict` so the
  Phase 1.3 MALFORMED-vs-content distinction propagates.
* ``raw_response`` is captured on every candidate
  (:class:`state.schemas.ReviewCandidate.raw_response`) — Phase 1.2
  parity.
* The empty-result dump path lives inside the adapter / delegate
  layer; this runner is a pure consumer.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autologging import get_logger
from adapters.types import AgentInvocation
from agents import load_prompt
from orchestrator.delegation_envelope import DelegationEnvelope
from state.evidence import write_evidence
from state.paths import autodev_root
from state.schemas import (
    CoderEvidence,
    ReviewCandidate,
    ReviewTournamentEvidence,
    Task,
)
from tournament.core import randomize_for_judge
from tournament.voting import BordaAggregator


if TYPE_CHECKING:
    from orchestrator import Orchestrator


logger = get_logger(__name__)


_DEFAULT_REVIEW_JUDGE_COHORT: tuple[str, ...] = (
    "judge",
    "minimality_judge",
    "judge_explorer",
)
"""Standard 3-role cohort. ``cfg.review_judge_roles`` overrides at runtime."""

_LABELS: tuple[str, ...] = ("A", "B", "AB")


@dataclass
class ReviewTournamentResult:
    """Return shape of :func:`run_review_tournament`.

    Carries the winning verdict + issues plus enough evidence for the
    caller's logging / ledger / retry-decision logic. The caller
    (``execute_phase``) inspects ``winning_label`` to decide:

      * ``"A"`` → original verdict stood; advance or soft-block
        WITHOUT another developer-refine cycle.
      * ``"B"`` / ``"AB"`` → developer is invoked ONCE MORE with the
        winning issues, then exits the tournament loop.

    ``escalated`` flips True when the loop hit ``max_rounds`` without
    a stable A-streak; the caller must route the task to
    ``critic_sounding_board``.
    """

    winning_verdict: str
    winning_issues: list[str]
    winning_label: str
    tournament_id: str
    converged: bool
    escalated: bool
    rounds: int
    evidence: ReviewTournamentEvidence


def _resolve_judge_cohort(cfg: Any) -> list[str]:
    """Return the effective judge-role list for one review tournament.

    Priority:
        1. Explicit ``cfg.review_judge_roles`` (operator override).
        2. Built-in default: ``["judge", "minimality_judge",
           "judge_explorer"]``.

    The list length wins over ``cfg.review_num_judges`` — operators
    that pin the cohort get exactly that many judges, no padding.
    """
    override = getattr(cfg, "review_judge_roles", None)
    if override:
        return list(override)
    return list(_DEFAULT_REVIEW_JUDGE_COHORT)


def _truncate(text: str, n: int) -> str:
    """Bytewise truncation with an ellipsis marker. None-safe."""
    if not text:
        return ""
    if len(text) <= n:
        return text
    return text[:n] + f"\n…[truncated, {len(text) - n} bytes]"


async def _build_candidate_a(
    orch: "Orchestrator",
    task: "Task",
    coder_ev: "CoderEvidence",
    review_env: "DelegationEnvelope",
    cwd_override: Path | None = None,
) -> ReviewCandidate:
    """Build Variant A: the unchanged patch + original reviewer verdict.

    Defers to :func:`orchestrator.execute_phase.delegate` so the
    v0.31.0 retry / budget-escalation / chunked-envelope plumbing all
    fires unchanged.
    """
    from orchestrator.execute_phase import _parse_review_verdict, delegate

    review_result = await delegate(
        orch,
        "reviewer",
        review_env,
        retry_count=task.retry_count,
        cwd_override=cwd_override,
    )
    verdict, issues = _parse_review_verdict(review_result.text)
    return ReviewCandidate(
        diff_excerpt=_truncate(coder_ev.diff or "", 4000),
        verdict=verdict,
        issues=issues,
        raw_response=review_result.text,
    )


async def _call_role_with_prompt(
    orch: "Orchestrator",
    role: str,
    user_message: str,
    cwd_override: Path | None,
    max_turns: int = 5,
) -> str:
    """Invoke an unregistered specialist role via :func:`load_prompt`.

    The ``adversarial_reviewer`` and ``merge_synthesizer`` roles are
    NOT in :data:`config.schema.REQUIRED_AGENT_ROLES` (they ship as
    optional v0.32.0 prompts), so we can't go through the registry +
    :func:`delegate` path. Instead we load the prompt body directly,
    construct an :class:`AgentInvocation`, and call the adapter.

    Tools default to read-only because both roles produce reviews
    (text-only output, no patches).
    """
    from agents.tool_map import resolve_claude_tools

    raw_prompt = load_prompt(role)
    full_prompt = "\n\n---\n".join([raw_prompt.strip(), user_message])
    tools = resolve_claude_tools("reviewer")  # read/glob/grep
    inv = AgentInvocation(
        role=role,
        prompt=full_prompt,
        cwd=cwd_override or orch.cwd,
        model=None,  # adapter resolves the default
        allowed_tools=tools,
        max_turns=max_turns,
    )
    result = await orch.adapter.execute(inv)
    return result.text or ""


async def _build_candidate_b(
    orch: "Orchestrator",
    coder_ev: "CoderEvidence",
    candidate_a: ReviewCandidate,
    cwd_override: Path | None,
) -> ReviewCandidate:
    """Build Variant B: the adversarial second-opinion review.

    Receives Variant A's verdict + issues + raw response so the
    adversarial reviewer can deliberately frame a *different*
    assessment. The ``DIFFERENCE-FROM-A:`` block in the prompt tells
    the adversarial reviewer that a parrot of A is treated as a
    duplicate and silently dropped from the Borda tally.
    """
    from orchestrator.execute_phase import _parse_review_verdict

    user_msg = "\n".join(
        [
            "ADVERSARIAL REVIEW REQUEST",
            "",
            "Below is the original developer patch + the original",
            "reviewer's verdict on it (Variant A). Your job is to",
            "produce a SECOND-OPINION review that is deliberately",
            "different from Variant A — find the framing the original",
            "reviewer missed.",
            "",
            "ORIGINAL DEVELOPER PATCH (excerpt):",
            "```",
            _truncate(coder_ev.diff or "", 6000),
            "```",
            "",
            f"VARIANT A VERDICT: {candidate_a.verdict}",
            "VARIANT A ISSUES:",
            *(f"  - {iss}" for iss in candidate_a.issues),
            "",
            "VARIANT A FULL RESPONSE:",
            _truncate(candidate_a.raw_response or "", 4000),
        ]
    )
    text = await _call_role_with_prompt(
        orch, "adversarial_reviewer", user_msg, cwd_override
    )
    verdict, issues = _parse_review_verdict(text)
    return ReviewCandidate(
        diff_excerpt=candidate_a.diff_excerpt,
        verdict=verdict,
        issues=issues,
        raw_response=text,
    )


async def _build_candidate_ab(
    orch: "Orchestrator",
    coder_ev: "CoderEvidence",
    candidate_a: ReviewCandidate,
    candidate_b: ReviewCandidate,
    cwd_override: Path | None,
) -> ReviewCandidate:
    """Build Variant AB: the synthesis of A and B.

    Receives both candidates' verdicts + issues + raw responses. The
    ``SYNTHESIS-NOTE:`` block in the prompt tells the synthesizer
    that a trivial concatenation is treated as a duplicate.
    """
    from orchestrator.execute_phase import _parse_review_verdict

    user_msg = "\n".join(
        [
            "REVIEW SYNTHESIS REQUEST",
            "",
            "Below is the original developer patch and TWO independent",
            "reviews of it. Your job is to produce a SINGLE synthesised",
            "review that combines A's strengths with B's improvements.",
            "",
            "ORIGINAL DEVELOPER PATCH (excerpt):",
            "```",
            _truncate(coder_ev.diff or "", 6000),
            "```",
            "",
            f"VARIANT A VERDICT: {candidate_a.verdict}",
            "VARIANT A ISSUES:",
            *(f"  - {iss}" for iss in candidate_a.issues),
            "",
            "VARIANT A FULL RESPONSE:",
            _truncate(candidate_a.raw_response or "", 3000),
            "",
            f"VARIANT B VERDICT: {candidate_b.verdict}",
            "VARIANT B ISSUES:",
            *(f"  - {iss}" for iss in candidate_b.issues),
            "",
            "VARIANT B FULL RESPONSE:",
            _truncate(candidate_b.raw_response or "", 3000),
        ]
    )
    text = await _call_role_with_prompt(
        orch, "merge_synthesizer", user_msg, cwd_override
    )
    verdict, issues = _parse_review_verdict(text)
    return ReviewCandidate(
        diff_excerpt=candidate_a.diff_excerpt,
        verdict=verdict,
        issues=issues,
        raw_response=text,
    )


def _render_judge_prompt(
    task: "Task",
    candidates: dict[str, ReviewCandidate],
    order_map: dict[int, str],
) -> str:
    """Render a blind judge prompt for the three candidates.

    ``order_map`` maps 1-based display index → canonical label so the
    judge cannot infer identity from position. The judge emits a
    ``RANKING:`` line over the display indices ``1 2 3``; the runner
    inverse-maps via :func:`tournament.core.parse_ranking`.
    """
    lines: list[str] = [
        "REVIEW TOURNAMENT — judge prompt",
        "",
        f"TASK: {task.id} — {task.title}",
        f"DESCRIPTION: {task.description}",
        "",
        "Three candidate REVIEWS of the developer's patch are presented",
        "below in randomised order. Each is a verdict (APPROVED /",
        "NEEDS_CHANGES / REJECTED / MALFORMED) plus an issues list.",
        "",
        "Your job is to RANK them best-to-worst. 'Best' means: the",
        "review most likely to produce a correct, minimal patch on the",
        "next iteration. A candidate that approves a defective patch is",
        "WORSE than one that rejects with a precise issue. A candidate",
        "that fabricates issues is WORSE than one that approves",
        "honestly.",
        "",
        "Emit a single line at the end of your response of the form:",
        "  RANKING: <1|2|3> <1|2|3> <1|2|3>",
        "with the three slot numbers in best-to-worst order.",
        "",
        "==============================",
    ]
    for pos in (1, 2, 3):
        label = order_map[pos]
        cand = candidates[label]
        lines.append("")
        lines.append(f"PROPOSAL {pos}")
        lines.append("---")
        lines.append(f"VERDICT: {cand.verdict}")
        lines.append("ISSUES:")
        if cand.issues:
            for iss in cand.issues:
                lines.append(f"  - {iss}")
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("RAW RESPONSE EXCERPT:")
        lines.append(_truncate(cand.raw_response or "", 1500))
        lines.append("==============================")
    lines.append("")
    lines.append("Remember: emit RANKING: <slot> <slot> <slot> as the")
    lines.append("FINAL line of your response.")
    return "\n".join(lines)


async def _run_one_judge(
    orch: "Orchestrator",
    judge_role: str,
    user_message: str,
    cwd_override: Path | None,
    order: dict[int, str],
) -> tuple[list[str] | None, str]:
    """Invoke one judge, parse its RANKING line, return canonical labels.

    Returns ``(canonical_ranking_or_None, raw_text)``. ``None`` for
    parse failures so the caller can drop the judge from the Borda
    tally per the standard
    :class:`tournament.voting.BordaAggregator` contract.
    """
    from agents.tool_map import resolve_claude_tools
    from tournament.core import parse_ranking

    # Specialist judge prompts (judge_explorer.md, minimality_judge.md)
    # ARE on disk; the standard ``judge`` role uses the tournament
    # judge system prompt. Try the on-disk variant first, fall back
    # to a minimal stub for the bare "judge" role so the existing
    # 1-shot reviewer call site doesn't grow a hard dependency on
    # ``tournament/prompts.py``.
    try:
        raw_prompt = load_prompt(judge_role)
    except FileNotFoundError:
        if judge_role == "judge":
            from tournament.prompts import JUDGE_SYSTEM as _JS
            raw_prompt = _JS
        else:
            raise

    full_prompt = "\n\n---\n".join([raw_prompt.strip(), user_message])
    inv = AgentInvocation(
        role=judge_role,
        prompt=full_prompt,
        cwd=cwd_override or orch.cwd,
        model=None,
        allowed_tools=resolve_claude_tools("reviewer"),
        max_turns=2,
    )
    try:
        result = await orch.adapter.execute(inv)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "review_tournament.judge_call_failed",
            judge_role=judge_role,
            err=str(exc),
        )
        return None, ""
    raw = result.text or ""
    raw_ranking = parse_ranking(raw, "123")
    if raw_ranking is None:
        return None, raw
    canonical = [order.get(int(slot), slot) for slot in raw_ranking]
    return canonical, raw


async def _run_judge_cohort(
    orch: "Orchestrator",
    task: "Task",
    candidates: dict[str, ReviewCandidate],
    judge_roles: list[str],
    rng: random.Random,
    cwd_override: Path | None,
) -> tuple[list[list[str] | None], list[dict[str, Any]]]:
    """Run the full judge cohort once and return (rankings, details).

    Each judge gets its OWN randomised display order (so two judges
    cannot collude on position bias). The ``details`` list is one
    dict per judge for the on-disk artifact + the ledger breadcrumb.
    """
    rankings: list[list[str] | None] = []
    details: list[dict[str, Any]] = []
    for judge_role in judge_roles:
        order = randomize_for_judge(
            candidates["A"], candidates["B"], candidates["AB"], rng
        )
        prompt = _render_judge_prompt(task, candidates, order)
        canonical, raw = await _run_one_judge(
            orch, judge_role, prompt, cwd_override, order
        )
        rankings.append(canonical)
        details.append(
            {
                "judge_role": judge_role,
                "order": {str(k): v for k, v in order.items()},
                "ranking": canonical,
                "raw_response": _truncate(raw, 4000),
            }
        )
    return rankings, details


def _no_progress(candidates: dict[str, ReviewCandidate]) -> bool:
    """Return True iff B and AB are structurally identical to A.

    Heuristic: same verdict + same set of issue strings. The
    adversarial reviewer / merge synthesizer prompts both warn that
    duplication of A is treated as a no-progress event; this detector
    enforces it from the runner side so the Borda tally falls through
    to the ``tiebreak_winner="A"`` invariant.
    """
    a = candidates["A"]
    b = candidates["B"]
    ab = candidates["AB"]

    def _same(x: ReviewCandidate, y: ReviewCandidate) -> bool:
        return x.verdict == y.verdict and set(x.issues) == set(y.issues)

    return _same(a, b) and _same(a, ab)


async def run_review_tournament(
    orch: "Orchestrator",
    task: "Task",
    coder_ev: "CoderEvidence",
    review_env: "DelegationEnvelope",
    *,
    cwd_override: Path | None = None,
) -> ReviewTournamentResult:
    """Run the A/B/AB review tournament for one developer attempt.

    Returns a :class:`ReviewTournamentResult` carrying the winning
    verdict + issues + label so the caller can decide:

      * ``winning_label == "A"`` AND ``converged`` → original verdict
        stands; advance or soft-block.
      * ``winning_label in ("B", "AB")`` → developer is invoked ONCE
        MORE with ``winning_issues`` and the loop exits.
      * ``escalated == True`` → caller routes to
        ``critic_sounding_board``.

    Always writes a :class:`ReviewTournamentEvidence` and a parallel
    ledger breadcrumb (``review_tournament_started`` +
    ``review_tournament_judged`` per round +
    ``review_tournament_converged`` | ``review_tournament_escalated``).
    """
    cfg = orch.cfg.tournaments
    num_judges_default = getattr(cfg, "review_num_judges", 3)
    convergence_k = getattr(cfg, "review_convergence_k", 2)
    max_rounds = getattr(cfg, "review_max_rounds", 5)
    judge_roles = _resolve_judge_cohort(cfg)
    if len(judge_roles) > num_judges_default:
        # Cohort length wins — operators that pin roles get exactly
        # the cohort they configured.
        num_judges = len(judge_roles)
    else:
        num_judges = num_judges_default
        # Pad / trim the cohort to ``num_judges``. Padding repeats the
        # first role (``"judge"`` by default) so the cohort always has
        # the configured size.
        while len(judge_roles) < num_judges:
            judge_roles.append(judge_roles[0])
        judge_roles = judge_roles[:num_judges]

    tournament_id = f"review-{uuid.uuid4().hex[:8]}"
    artifact_dir = autodev_root(orch.cwd) / "tournaments" / tournament_id
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Forensics directory is best-effort — a write failure must
        # not abort the tournament.
        logger.warning(
            "review_tournament.artifact_dir_failed",
            tournament_id=tournament_id,
            err=str(exc),
        )

    diff_bytes = len(coder_ev.diff or "")
    await orch.plan_manager.ledger_append(
        op="review_tournament_started",
        payload={
            "tournament_id": tournament_id,
            "task_id": task.id,
            "diff_bytes": diff_bytes,
            "convergence_k": convergence_k,
            "max_rounds": max_rounds,
            "num_judges": len(judge_roles),
        },
    )

    logger.info(
        "review_tournament.start",
        tournament_id=tournament_id,
        task_id=task.id,
        convergence_k=convergence_k,
        max_rounds=max_rounds,
        judge_roles=judge_roles,
    )

    rng = random.Random(uuid.uuid4().int)
    aggregator = BordaAggregator()

    # State across rounds.
    a_streak = 0
    last_winner = "A"
    last_borda_scores: dict[str, int] = {}
    last_valid_judges = 0
    last_candidates: dict[str, ReviewCandidate] = {}
    last_rankings: list[list[str] | None] = []
    rounds_run = 0

    for round_num in range(1, max_rounds + 1):
        rounds_run = round_num

        # Build the three candidates for this round. A is rebuilt each
        # round because each round's developer state may have shifted
        # via the orchestrator's between-round retry; in practice the
        # caller invokes the runner once per developer attempt so A is
        # the current developer output.
        candidate_a = await _build_candidate_a(
            orch, task, coder_ev, review_env, cwd_override
        )
        try:
            candidate_b = await _build_candidate_b(
                orch, coder_ev, candidate_a, cwd_override
            )
        except Exception as exc:  # noqa: BLE001
            # B failed — synthesize a MALFORMED placeholder so the
            # tournament can still tally. The judges will rank it last.
            logger.warning(
                "review_tournament.b_call_failed",
                tournament_id=tournament_id,
                round=round_num,
                err=str(exc),
            )
            candidate_b = ReviewCandidate(
                diff_excerpt=candidate_a.diff_excerpt,
                verdict="MALFORMED",
                issues=[f"adversarial_reviewer call failed: {exc}"],
                raw_response=None,
            )
        try:
            candidate_ab = await _build_candidate_ab(
                orch, coder_ev, candidate_a, candidate_b, cwd_override
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "review_tournament.ab_call_failed",
                tournament_id=tournament_id,
                round=round_num,
                err=str(exc),
            )
            candidate_ab = ReviewCandidate(
                diff_excerpt=candidate_a.diff_excerpt,
                verdict="MALFORMED",
                issues=[f"merge_synthesizer call failed: {exc}"],
                raw_response=None,
            )

        candidates = {
            "A": candidate_a,
            "B": candidate_b,
            "AB": candidate_ab,
        }

        # No-progress short-circuit: when B and AB are structurally
        # identical to A, skip the judge cohort and call A the winner
        # by tiebreak. This preserves the round's "A wins" signal
        # without burning the judge budget on a foregone outcome.
        if _no_progress(candidates):
            logger.info(
                "review_tournament.no_progress",
                tournament_id=tournament_id,
                round=round_num,
            )
            rankings: list[list[str] | None] = []
            details: list[dict[str, Any]] = []
            winner = "A"
            scores = {"A": 0, "B": 0, "AB": 0}
            valid_judges = 0
        else:
            rankings, details = await _run_judge_cohort(
                orch, task, candidates, judge_roles, rng, cwd_override
            )
            winner, scores, valid_judges = aggregator.aggregate(
                rankings, labels=list(_LABELS), tiebreak_winner="A"
            )

        last_winner = winner
        last_borda_scores = dict(scores)
        last_valid_judges = valid_judges
        last_candidates = candidates
        last_rankings = rankings

        await orch.plan_manager.ledger_append(
            op="review_tournament_judged",
            payload={
                "tournament_id": tournament_id,
                "task_id": task.id,
                "round": round_num,
                "winner": winner,
                "borda_scores": dict(scores),
                "valid_judges": valid_judges,
            },
        )
        logger.info(
            "review_tournament.round_complete",
            tournament_id=tournament_id,
            task_id=task.id,
            round=round_num,
            winner=winner,
            scores=scores,
            valid_judges=valid_judges,
        )

        # Convergence: A wins ``convergence_k`` consecutive rounds →
        # do nothing, exit.
        if winner == "A":
            a_streak += 1
            if a_streak >= convergence_k:
                evidence = ReviewTournamentEvidence(
                    task_id=task.id,
                    tournament_id=tournament_id,
                    candidates=candidates,
                    judge_rankings=rankings,
                    winner="A",
                    borda_scores=dict(scores),
                    valid_judges=valid_judges,
                    converged=True,
                    rounds=round_num,
                )
                await write_evidence(orch.cwd, task.id, evidence)
                await orch.plan_manager.ledger_append(
                    op="review_tournament_converged",
                    payload={
                        "tournament_id": tournament_id,
                        "task_id": task.id,
                        "rounds": round_num,
                        "final_winner": "A",
                        "final_verdict": candidate_a.verdict,
                    },
                )
                return ReviewTournamentResult(
                    winning_verdict=candidate_a.verdict,
                    winning_issues=candidate_a.issues,
                    winning_label="A",
                    tournament_id=tournament_id,
                    converged=True,
                    escalated=False,
                    rounds=round_num,
                    evidence=evidence,
                )
            # Otherwise keep looping — A streak still building.
            continue

        # B or AB won this round — exit immediately. The caller
        # invokes the developer ONCE MORE with the winner's issues;
        # the tournament does NOT recurse.
        a_streak = 0
        winning_cand = candidates[winner]
        evidence = ReviewTournamentEvidence(
            task_id=task.id,
            tournament_id=tournament_id,
            candidates=candidates,
            judge_rankings=rankings,
            winner=winner,
            borda_scores=dict(scores),
            valid_judges=valid_judges,
            converged=False,
            rounds=round_num,
        )
        await write_evidence(orch.cwd, task.id, evidence)
        return ReviewTournamentResult(
            winning_verdict=winning_cand.verdict,
            winning_issues=winning_cand.issues,
            winning_label=winner,
            tournament_id=tournament_id,
            converged=False,
            escalated=False,
            rounds=round_num,
            evidence=evidence,
        )

    # Fell out of the loop without convergence → escalate.
    evidence = ReviewTournamentEvidence(
        task_id=task.id,
        tournament_id=tournament_id,
        candidates=last_candidates,
        judge_rankings=last_rankings,
        winner=last_winner,
        borda_scores=last_borda_scores,
        valid_judges=last_valid_judges,
        converged=False,
        rounds=rounds_run,
    )
    await write_evidence(orch.cwd, task.id, evidence)
    await orch.plan_manager.ledger_append(
        op="review_tournament_escalated",
        payload={
            "tournament_id": tournament_id,
            "task_id": task.id,
            "rounds": rounds_run,
            "final_winner": last_winner,
            "escalation_reason": "max_rounds_without_convergence",
        },
    )
    logger.warning(
        "review_tournament.escalated",
        tournament_id=tournament_id,
        task_id=task.id,
        rounds=rounds_run,
    )
    last_winning_cand = last_candidates.get(last_winner) if last_candidates else None
    return ReviewTournamentResult(
        winning_verdict=last_winning_cand.verdict if last_winning_cand else "MALFORMED",
        winning_issues=last_winning_cand.issues if last_winning_cand else [],
        winning_label=last_winner,
        tournament_id=tournament_id,
        converged=False,
        escalated=True,
        rounds=rounds_run,
        evidence=evidence,
    )


__all__ = [
    "ReviewTournamentResult",
    "_DEFAULT_REVIEW_JUDGE_COHORT",
    "_no_progress",
    "_resolve_judge_cohort",
    "run_review_tournament",
]
