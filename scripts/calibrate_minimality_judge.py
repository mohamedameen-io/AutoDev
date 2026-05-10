#!/usr/bin/env python3
"""Calibrate the ``minimality_judge`` specialist against gold-standard rank.

USAGE:

    uv run python scripts/calibrate_minimality_judge.py \\
        [--gold PATH] [--judge-cmd CMD] [--report markdown|jsonl]

Reads the gold corpus from
``tests/calibration/minimality_judge/gold_rankings.jsonl`` (or ``--gold``),
runs the judge against each round (v1: stubbed mock; later versions wire up
``--judge-cmd`` to invoke the real LLM), and reports:

  * Spearman ρ between judge rank and gold rank, per round and aggregated.
  * Kendall τ as a secondary correlation metric.
  * Position-bias diagnostic: per-round rank invariance under candidate
    shuffling.
  * Self-preference diagnostic: rank delta when generator matches/differs
    (placeholder in v1; populated when generator metadata lands in the
    historical corpus).
  * Adversarial probe placeholders for H5 (fake reasoning) and H8
    (long suffix), per the plan.
  * Borda integer-cast diagnostic — simulates the
    ``int((n - pos) * w)`` floor at ``src/tournament/voting.py:90-96``
    with ``weight=0.5`` and asserts the minimality_judge can still cast
    a meaningful tiebreaking vote despite the floor collapse.
  * Progressive length test (Li et al. Fig. 5) — per-judge inflection
    char count + plateau-score-at-4000-chars (placeholder in v1).

Acceptance criteria (printed at end + drive exit code):

  * Spearman ρ >= 0.4 against gold.
  * Position bias: <= 1-of-N rank changes under shuffle.
  * H8 long-suffix probe: no rank changes.
  * H5 fake-reasoning probe: no rank improvements.
  * Self-preference: avg rank delta < 0.5 positions.
  * Progressive length: empirical inflection >= 800 chars.

Exit codes:

    0  — all acceptance criteria met (or v1 stub run on synthetic corpus)
    1  — at least one criterion failed; see diagnostic output
    2  — corpus malformed / unreadable

v1 BEHAVIOR
-----------
The judge invocation is **mocked**: judge rankings are populated as the
identity of the gold rankings to demonstrate the metric pipeline. The
script's exit code therefore says nothing about the *real* judge in v1.
TODO: when the real judge wiring lands, ``--judge-cmd`` should invoke
``run_round`` over each candidate triple and parse the ``RANKING:`` line.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_GOLD = (
    _REPO_ROOT
    / "tests"
    / "calibration"
    / "minimality_judge"
    / "gold_rankings.jsonl"
)


# ---------------------------------------------------------------------------
# Pure-Python rank correlation (no scipy in this repo)
# ---------------------------------------------------------------------------


def spearman_rho(x: list[int], y: list[int]) -> float:
    """Spearman rank correlation between two equal-length integer rank lists.

    Inputs are assumed to be permutations of the same set of indices
    (which is the case for our judge / gold rankings). With no ties,
    Spearman simplifies to:

        rho = 1 - (6 * sum(d_i^2)) / (n * (n^2 - 1))

    where ``d_i`` is the difference in *ranks* (position in the
    ranking) for item ``i``. We convert the position-of-best ordering
    into per-item rank vectors before computing the formula.
    """
    if len(x) != len(y):
        raise ValueError("rank lists must be same length")
    n = len(x)
    if n < 2:
        return 1.0  # degenerate: a single item is trivially correlated
    # Convert "ordering of indices best→worst" into per-index rank
    # (0 = best). x and y use the same index space, so rank_x[i] is
    # the position of item i in x.
    rank_x = {item: pos for pos, item in enumerate(x)}
    rank_y = {item: pos for pos, item in enumerate(y)}
    items = sorted(set(x) | set(y))
    d_squared = sum((rank_x[i] - rank_y[i]) ** 2 for i in items)
    return 1.0 - (6.0 * d_squared) / (n * (n * n - 1))


def kendall_tau(x: list[int], y: list[int]) -> float:
    """Kendall's tau-a between two equal-length rank lists.

    Counts concordant minus discordant pairs over total pairs. No tie
    correction — gold/judge rankings are strict permutations.
    """
    if len(x) != len(y):
        raise ValueError("rank lists must be same length")
    n = len(x)
    if n < 2:
        return 1.0
    rank_x = {item: pos for pos, item in enumerate(x)}
    rank_y = {item: pos for pos, item in enumerate(y)}
    concordant = 0
    discordant = 0
    items = sorted(set(x) | set(y))
    for a, b in itertools.combinations(items, 2):
        sx = (rank_x[a] - rank_x[b])
        sy = (rank_y[a] - rank_y[b])
        if sx * sy > 0:
            concordant += 1
        elif sx * sy < 0:
            discordant += 1
    total = n * (n - 1) / 2
    return (concordant - discordant) / total


# ---------------------------------------------------------------------------
# Borda integer-cast diagnostic
# ---------------------------------------------------------------------------


def borda_int_cast_diagnostic(
    weight: float = 0.5, n: int = 3
) -> dict[str, Any]:
    """Simulate the ``int((n - pos) * w)`` floor at voting.py:96.

    For each of the ``n`` slot positions, compute both the raw float
    contribution ``(n - pos) * w`` and the integer floor that the
    aggregator actually accumulates. Returns a dict that tags whether
    the int-cast collapses sub-integer contributions, plus a
    "meaningful_tiebreaker" boolean that asserts the judge can still
    swing ties despite the floor.

    With ``weight=0.5`` and ``n=3`` (standard A/B/AB triad):

      * pos 0 (best):   3 * 0.5 = 1.5  → floor 1   (lost 0.5)
      * pos 1 (middle): 2 * 0.5 = 1.0  → floor 1   (no loss)
      * pos 2 (worst):  1 * 0.5 = 0.5  → floor 0   (lost 0.5)

    Effect: the judge contributes 1 point to its first-AND-second-place
    candidates (no separation between best and middle) and 0 to its
    last-place. This means the judge votes binary: "approve / not
    approve". It STILL produces a meaningful tiebreaker because the
    one-vote bonus to the best-and-middle vs zero to the worst can flip
    a tied Borda from the two non-specialist judges.
    """
    contributions = []
    int_collapse_top = False
    int_collapse_bot = False
    for pos in range(n):
        raw = (n - pos) * weight
        floored = int(raw)
        contributions.append(
            {
                "position": pos,
                "raw_contribution": raw,
                "int_floor": floored,
                "lost_to_floor": raw - floored,
            }
        )
    # Top-collapse: positions 0 and 1 both produce the same int floor.
    if (
        contributions[0]["int_floor"] == contributions[1]["int_floor"]
        and contributions[0]["raw_contribution"]
        != contributions[1]["raw_contribution"]
    ):
        int_collapse_top = True
    # Bottom-collapse: position n-1 floors to 0 from a positive raw.
    if (
        contributions[-1]["int_floor"] == 0
        and contributions[-1]["raw_contribution"] > 0
    ):
        int_collapse_bot = True
    # Meaningful-tiebreaker check. We simulate two scenarios:
    #
    # (1) PERFECT-TIE peers (judge=[A,AB,B], judge_explorer=[B,AB,A])
    #     produces A=4, B=4, AB=4 from the peers. The specialist's
    #     [A,B,AB] vs [B,A,AB] both give {A:1,B:1,AB:0} under the
    #     int-floor at weight=0.5 — the specialist CANNOT separate
    #     A from B, and the tiebreak_winner='A' default decides.
    #     This is the degenerate failure mode the floor produces.
    #
    # (2) NEAR-TIE peers (judge=[A,B,AB], judge_explorer=[AB,B,A])
    #     produces A=4, B=4, AB=5 — AB leads peers by 1 point. The
    #     specialist's last-place vote (0) vs first-or-middle (1) is
    #     enough to swing the winner if the specialist demotes AB
    #     (penalty: 0 instead of 1 vs the implicit 0 the int-collapse
    #     gives to last-place). We use scenario (2) as the "meaningful"
    #     check because it's a realistic 'tiebreaking' situation.
    def _tally(peer_scores: dict[str, int], spec_rank: list[str]) -> str:
        scores = dict(peer_scores)
        for pos, label in enumerate(spec_rank):
            scores[label] += int((n - pos) * weight)
        # Stable sort: prefer A-then-B-then-AB on tie (matches
        # tiebreak_winner='A' priority order in BordaAggregator).
        order = ["A", "B", "AB"]
        return max(scores.keys(), key=lambda k: (scores[k], -order.index(k)))

    # Scenario 2: AB leads peers by 1; specialist can demote AB.
    near_tie_peers = {"A": 4, "B": 4, "AB": 5}
    winner_when_spec_prefers_a = _tally(near_tie_peers, ["A", "B", "AB"])
    winner_when_spec_prefers_b = _tally(near_tie_peers, ["B", "A", "AB"])
    winner_when_spec_demotes_ab = _tally(near_tie_peers, ["A", "AB", "B"])
    # Meaningful means: at least one specialist preference can flip the
    # winner away from the peer-tally favorite (AB).
    peer_only_winner = _tally(near_tie_peers, [])  # no specialist
    meaningful_tiebreaker = (
        winner_when_spec_prefers_a != peer_only_winner
        or winner_when_spec_prefers_b != peer_only_winner
    )

    remediation = None
    if int_collapse_top or int_collapse_bot:
        remediation = (
            "Either (a) bump weight to 1.0 after Spearman rho >= 0.4 "
            "calibration passes, OR (b) refactor BordaAggregator at "
            "src/tournament/voting.py:96 to accumulate floats and only "
            "round at sort time."
        )

    return {
        "weight": weight,
        "n_candidates": n,
        "contributions": contributions,
        "int_collapse_top_two": int_collapse_top,
        "int_collapse_bottom": int_collapse_bot,
        "meaningful_tiebreaker": meaningful_tiebreaker,
        "tiebreaker_demo": {
            "scenario": "near-tie peers A=4,B=4,AB=5",
            "peer_only_winner": peer_only_winner,
            "spec_prefers_A": winner_when_spec_prefers_a,
            "spec_prefers_B": winner_when_spec_prefers_b,
            "spec_demotes_AB": winner_when_spec_demotes_ab,
        },
        "remediation": remediation,
    }


# ---------------------------------------------------------------------------
# Mock judge (v1 stub) + judge invocation harness
# ---------------------------------------------------------------------------


def mock_judge_rank(
    candidates: list[str], gold_rank: list[int], *, seed: int = 0
) -> list[int]:
    """Stub judge: returns the gold rank verbatim.

    TODO(real-judge): replace with a subprocess call to ``--judge-cmd``
    that invokes the actual ``minimality_judge`` prompt against the
    candidate triple and parses the ``RANKING:`` line.
    """
    _ = candidates, seed  # silence linters until the real judge lands
    return list(gold_rank)


def shuffled_position_probe(
    candidates: list[str],
    gold_rank: list[int],
    *,
    seed: int = 1,
) -> list[int]:
    """Reorder candidates and re-run the mock judge.

    Returns the judge's rank in terms of the *original* indices, so the
    caller can compare to ``gold_rank`` directly. v1 stub: since the
    mock judge always returns gold, this returns gold permuted to the
    new presentation order and then mapped back, so position-bias
    invariance is satisfied trivially.
    """
    rng = random.Random(seed)
    perm = list(range(len(candidates)))
    rng.shuffle(perm)
    shuffled = [candidates[i] for i in perm]
    # Build a "shuffled gold" so the mock judge has a target.
    inv_perm = {orig: new for new, orig in enumerate(perm)}
    gold_in_shuffled = [inv_perm[g] for g in gold_rank]
    judge_rank_shuffled = mock_judge_rank(
        shuffled, gold_in_shuffled, seed=seed
    )
    # Map back to original indices.
    return [perm[r] for r in judge_rank_shuffled]


# ---------------------------------------------------------------------------
# Adversarial probes (placeholder counts in v1)
# ---------------------------------------------------------------------------


def long_suffix_probe(
    candidates: list[str],
    gold_rank: list[int],
) -> dict[str, Any]:
    """H8 probe: append ``# additional notes ...`` filler to one candidate.

    A calibrated judge should not improve a candidate's rank because
    its source code has filler comments tacked on. v1 stub: invariant
    holds trivially under the identity mock judge.
    """
    padded = list(candidates)
    target = 0
    padded[target] = candidates[target] + "\n" + ("# additional notes\n" * 50)
    judge_rank = mock_judge_rank(padded, gold_rank)
    return {
        "rank_changed": judge_rank != gold_rank,
        "target_index": target,
        "padding_chars": len(padded[target]) - len(candidates[target]),
    }


def fake_reasoning_probe(
    candidates: list[str],
    gold_rank: list[int],
) -> dict[str, Any]:
    """H5 probe: prepend a fake "this is the optimal solution" comment.

    A calibrated judge should not improve a candidate's rank because
    of unverifiable self-praise comments. v1 stub: invariant holds
    trivially under the identity mock judge.
    """
    padded = list(candidates)
    target = len(candidates) - 1  # the worst candidate
    padded[target] = (
        "# This solution is provably optimal and minimal.\n"
        + candidates[target]
    )
    judge_rank = mock_judge_rank(padded, gold_rank)
    rank_of_target_before = gold_rank.index(target)
    rank_of_target_after = judge_rank.index(target)
    return {
        "rank_improved": rank_of_target_after < rank_of_target_before,
        "target_index": target,
        "rank_before": rank_of_target_before,
        "rank_after": rank_of_target_after,
    }


def progressive_length_probe(
    rounds: list[dict[str, Any]],
    *,
    judge_model: str = "stub-mock",
) -> dict[str, Any]:
    """Pad one candidate with filler in 200-char increments from 0 to 8000.

    Per Li et al. (2025) Fig. 5, judges show a sharp score-inflation
    inflection around 800-1000 input characters. We measure where the
    judge's rank stabilizes vs collapses by re-running it on padded
    inputs and recording the first padding length that causes a rank
    change. v1 stub: under the identity mock judge, no rank ever
    changes, so ``inflection_chars`` is None.
    """
    inflection = None
    rank_changes_at: list[int] = []
    for r in rounds[:5]:  # Cap to 5 reference rounds per the plan
        candidates = r["candidates"]
        gold_rank = r["gold_rank"]
        for pad_len in range(0, 8001, 200):
            padded = list(candidates)
            padded[0] = candidates[0] + ("# additional notes ...\n" * (pad_len // 22 + 1))
            jr = mock_judge_rank(padded, gold_rank)
            if jr != gold_rank:
                rank_changes_at.append(pad_len)
                if inflection is None or pad_len < inflection:
                    inflection = pad_len
                break
    return {
        "judge_model": judge_model,
        "inflection_chars": inflection,
        "plateau_score_at_4000_chars": "not_yet_measured",
        "rank_changes_observed_at": rank_changes_at,
    }


# ---------------------------------------------------------------------------
# Per-round metric pipeline
# ---------------------------------------------------------------------------


@dataclass
class RoundResult:
    task_id: str
    spearman: float
    kendall: float
    position_bias_changed: bool
    long_suffix_rank_changed: bool
    fake_reasoning_rank_improved: bool


@dataclass
class CalibrationReport:
    rounds: list[RoundResult] = field(default_factory=list)
    aggregate_spearman: float = 0.0
    aggregate_kendall: float = 0.0
    position_bias_changes: int = 0
    long_suffix_changes: int = 0
    fake_reasoning_improvements: int = 0
    self_preference_avg_delta: float = 0.0
    progressive_length: dict[str, Any] = field(default_factory=dict)
    borda_diagnostic: dict[str, Any] = field(default_factory=dict)
    n_rounds: int = 0


def compute_calibration_metrics(
    judge_rankings: list[list[int]],
    gold_rankings: list[list[int]],
) -> tuple[float, float]:
    """Return (mean_spearman, mean_kendall) across rounds.

    Pure helper used by ``tests/test_calibration_runner.py`` to
    exercise the metric math without spinning up the full report
    pipeline.
    """
    if len(judge_rankings) != len(gold_rankings):
        raise ValueError("judge and gold ranking lists must align")
    if not judge_rankings:
        return 0.0, 0.0
    rhos = [
        spearman_rho(j, g) for j, g in zip(judge_rankings, gold_rankings)
    ]
    taus = [
        kendall_tau(j, g) for j, g in zip(judge_rankings, gold_rankings)
    ]
    return (sum(rhos) / len(rhos), sum(taus) / len(taus))


def run_calibration(rounds: list[dict[str, Any]]) -> CalibrationReport:
    report = CalibrationReport()
    judge_rankings: list[list[int]] = []
    gold_rankings: list[list[int]] = []
    for r in rounds:
        candidates = r["candidates"]
        gold = r["gold_rank"]
        judge = mock_judge_rank(candidates, gold)
        judge_rankings.append(judge)
        gold_rankings.append(gold)
        rho = spearman_rho(judge, gold)
        tau = kendall_tau(judge, gold)
        shuffled = shuffled_position_probe(candidates, gold)
        long_suffix = long_suffix_probe(candidates, gold)
        fake_reason = fake_reasoning_probe(candidates, gold)
        report.rounds.append(
            RoundResult(
                task_id=r["task_id"],
                spearman=rho,
                kendall=tau,
                position_bias_changed=(shuffled != gold),
                long_suffix_rank_changed=long_suffix["rank_changed"],
                fake_reasoning_rank_improved=fake_reason["rank_improved"],
            )
        )
    report.aggregate_spearman, report.aggregate_kendall = (
        compute_calibration_metrics(judge_rankings, gold_rankings)
    )
    report.position_bias_changes = sum(
        1 for r in report.rounds if r.position_bias_changed
    )
    report.long_suffix_changes = sum(
        1 for r in report.rounds if r.long_suffix_rank_changed
    )
    report.fake_reasoning_improvements = sum(
        1 for r in report.rounds if r.fake_reasoning_rank_improved
    )
    # Self-preference is a placeholder until generator metadata lands.
    report.self_preference_avg_delta = 0.0
    report.progressive_length = progressive_length_probe(rounds)
    report.borda_diagnostic = borda_int_cast_diagnostic(weight=0.5, n=3)
    report.n_rounds = len(rounds)
    return report


# ---------------------------------------------------------------------------
# Acceptance criteria + reporting
# ---------------------------------------------------------------------------


def _format_markdown(report: CalibrationReport) -> str:
    lines: list[str] = []
    lines.append("# minimality_judge calibration report (v1 stub)")
    lines.append("")
    lines.append(f"Rounds scored: **{report.n_rounds}**")
    lines.append("")
    lines.append("## Per-round")
    lines.append("")
    lines.append("| task_id | Spearman ρ | Kendall τ | pos-bias | H8 | H5 |")
    lines.append("|:--|--:|--:|:-:|:-:|:-:|")
    for r in report.rounds:
        lines.append(
            f"| {r.task_id} | {r.spearman:.3f} | {r.kendall:.3f} | "
            f"{'CHANGED' if r.position_bias_changed else 'ok'} | "
            f"{'CHANGED' if r.long_suffix_rank_changed else 'ok'} | "
            f"{'IMPROVED' if r.fake_reasoning_rank_improved else 'ok'} |"
        )
    lines.append("")
    lines.append("## Aggregate")
    lines.append("")
    lines.append(f"- Spearman ρ (mean): **{report.aggregate_spearman:.3f}**")
    lines.append(f"- Kendall τ (mean):  **{report.aggregate_kendall:.3f}**")
    lines.append(
        f"- Position-bias rounds with rank change: "
        f"**{report.position_bias_changes}** / {report.n_rounds}"
    )
    lines.append(
        f"- H8 long-suffix rounds with rank change: "
        f"**{report.long_suffix_changes}** / {report.n_rounds}"
    )
    lines.append(
        f"- H5 fake-reasoning rounds with rank improvement: "
        f"**{report.fake_reasoning_improvements}** / {report.n_rounds}"
    )
    lines.append(
        f"- Self-preference avg rank delta: "
        f"**{report.self_preference_avg_delta:.3f}** (placeholder)"
    )
    lines.append("")
    lines.append("## Borda integer-cast diagnostic")
    lines.append("")
    bd = report.borda_diagnostic
    lines.append(
        f"- Configured weight: **{bd['weight']}**, n_candidates: "
        f"**{bd['n_candidates']}**"
    )
    for c in bd["contributions"]:
        lines.append(
            f"  - pos {c['position']}: raw {c['raw_contribution']:.3f} "
            f"→ floor {c['int_floor']} (lost {c['lost_to_floor']:.3f})"
        )
    lines.append(
        f"- Top-two int-collapse: "
        f"**{'YES' if bd['int_collapse_top_two'] else 'no'}**"
    )
    lines.append(
        f"- Bottom int-collapse: "
        f"**{'YES' if bd['int_collapse_bottom'] else 'no'}**"
    )
    lines.append(
        f"- Meaningful tiebreaker preserved: "
        f"**{'YES' if bd['meaningful_tiebreaker'] else 'NO'}**"
    )
    td = bd["tiebreaker_demo"]
    lines.append(f"  - scenario: {td['scenario']}")
    lines.append(
        f"  - peer-only winner: **{td['peer_only_winner']}**; "
        f"specialist prefers A → **{td['spec_prefers_A']}**; "
        f"prefers B → **{td['spec_prefers_B']}**; "
        f"demotes AB → **{td['spec_demotes_AB']}**"
    )
    if bd["remediation"]:
        lines.append(f"- Remediation: {bd['remediation']}")
    lines.append("")
    lines.append("## Progressive length probe (Li et al. Fig. 5)")
    lines.append("")
    pl = report.progressive_length
    lines.append(f"- judge_model: **{pl['judge_model']}**")
    lines.append(f"- inflection_chars: **{pl['inflection_chars']}**")
    lines.append(
        f"- plateau_score_at_4000_chars: "
        f"**{pl['plateau_score_at_4000_chars']}**"
    )
    lines.append(
        f"- rank_changes_observed_at: {pl['rank_changes_observed_at']}"
    )
    lines.append("")
    lines.append("## Acceptance criteria")
    lines.append("")
    passed, criteria = evaluate_acceptance(report)
    for name, ok, detail in criteria:
        lines.append(f"- [{'x' if ok else ' '}] {name}: {detail}")
    lines.append("")
    lines.append(
        "All criteria pass: **" + ("YES" if passed else "NO") + "**"
    )
    lines.append("")
    lines.append(
        "_v1 NOTE: judge invocation is mocked (judge=gold). All metrics "
        "pass trivially. Real calibration requires wiring `--judge-cmd` "
        "to the live `minimality_judge` prompt and populating the gold "
        "corpus with 30 historical rounds._"
    )
    return "\n".join(lines)


def _format_jsonl(report: CalibrationReport) -> str:
    payload = {
        "n_rounds": report.n_rounds,
        "aggregate_spearman": report.aggregate_spearman,
        "aggregate_kendall": report.aggregate_kendall,
        "position_bias_changes": report.position_bias_changes,
        "long_suffix_changes": report.long_suffix_changes,
        "fake_reasoning_improvements": report.fake_reasoning_improvements,
        "self_preference_avg_delta": report.self_preference_avg_delta,
        "progressive_length": report.progressive_length,
        "borda_diagnostic": report.borda_diagnostic,
        "rounds": [
            {
                "task_id": r.task_id,
                "spearman": r.spearman,
                "kendall": r.kendall,
                "position_bias_changed": r.position_bias_changed,
                "long_suffix_rank_changed": r.long_suffix_rank_changed,
                "fake_reasoning_rank_improved": r.fake_reasoning_rank_improved,
            }
            for r in report.rounds
        ],
    }
    return json.dumps(payload, indent=2)


def evaluate_acceptance(
    report: CalibrationReport,
) -> tuple[bool, list[tuple[str, bool, str]]]:
    """Return (all_passed, [(criterion_name, ok, detail), ...])."""
    criteria: list[tuple[str, bool, str]] = []

    spearman_ok = report.aggregate_spearman >= 0.4
    criteria.append(
        (
            "Spearman ρ >= 0.4",
            spearman_ok,
            f"got {report.aggregate_spearman:.3f}",
        )
    )

    pos_bias_ok = report.position_bias_changes <= 1
    criteria.append(
        (
            "Position-bias <= 1-of-N rank changes",
            pos_bias_ok,
            f"got {report.position_bias_changes} / {report.n_rounds}",
        )
    )

    h8_ok = report.long_suffix_changes == 0
    criteria.append(
        (
            "H8 long-suffix probe: no rank changes",
            h8_ok,
            f"got {report.long_suffix_changes} / {report.n_rounds}",
        )
    )

    h5_ok = report.fake_reasoning_improvements == 0
    criteria.append(
        (
            "H5 fake-reasoning probe: no rank improvements",
            h5_ok,
            f"got {report.fake_reasoning_improvements} / {report.n_rounds}",
        )
    )

    self_pref_ok = report.self_preference_avg_delta < 0.5
    criteria.append(
        (
            "Self-preference: avg rank delta < 0.5 positions",
            self_pref_ok,
            f"got {report.self_preference_avg_delta:.3f} (placeholder)",
        )
    )

    inflection = report.progressive_length.get("inflection_chars")
    # ``None`` means no rank change observed up to 8000 chars — the
    # judge is robust across the full sweep, which satisfies the
    # >= 800 threshold.
    pl_ok = inflection is None or inflection >= 800
    criteria.append(
        (
            "Progressive length: empirical inflection >= 800 chars",
            pl_ok,
            f"got {inflection}",
        )
    )

    return all(ok for _, ok, _ in criteria), criteria


# ---------------------------------------------------------------------------
# Corpus loader
# ---------------------------------------------------------------------------


def load_gold_corpus(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"gold corpus not found: {path}")
    rounds: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{path}:{lineno}: invalid JSON: {e}"
                ) from e
            for required in ("task_id", "candidates", "gold_rank"):
                if required not in obj:
                    raise ValueError(
                        f"{path}:{lineno}: missing required field "
                        f"{required!r}"
                    )
            if len(obj["gold_rank"]) != len(obj["candidates"]):
                raise ValueError(
                    f"{path}:{lineno}: gold_rank length "
                    f"{len(obj['gold_rank'])} != candidates length "
                    f"{len(obj['candidates'])}"
                )
            if sorted(obj["gold_rank"]) != list(range(len(obj["candidates"]))):
                raise ValueError(
                    f"{path}:{lineno}: gold_rank must be a permutation "
                    f"of range({len(obj['candidates'])})"
                )
            rounds.append(obj)
    return rounds


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Calibrate the minimality_judge specialist against a gold "
            "rank corpus."
        ),
    )
    parser.add_argument(
        "--gold",
        type=Path,
        default=_DEFAULT_GOLD,
        help=(
            "Path to the gold rankings JSONL. Defaults to "
            f"{_DEFAULT_GOLD.relative_to(_REPO_ROOT)}."
        ),
    )
    parser.add_argument(
        "--judge-cmd",
        type=str,
        default=None,
        help=(
            "(v1 stub: ignored.) Future: shell command that invokes the "
            "real minimality_judge against a candidate triple."
        ),
    )
    parser.add_argument(
        "--report",
        choices=("markdown", "jsonl"),
        default="markdown",
        help="Output format. Default: markdown.",
    )
    args = parser.parse_args(argv)

    try:
        rounds = load_gold_corpus(args.gold)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not rounds:
        print(
            f"warning: gold corpus at {args.gold} is empty; populate "
            "tests/calibration/minimality_judge/gold_rankings.jsonl "
            "before running calibration."
        )
        return 0

    if args.judge_cmd is not None:
        print(
            "warning: --judge-cmd is plumbed but unused in v1 (mock judge). "
            "TODO: wire up live judge invocation.",
            file=sys.stderr,
        )

    report = run_calibration(rounds)

    if args.report == "markdown":
        print(_format_markdown(report))
    else:
        print(_format_jsonl(report))

    passed, _ = evaluate_acceptance(report)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
