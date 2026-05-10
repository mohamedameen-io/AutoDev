# `minimality_judge` calibration — operator guide

This document describes how to run the calibration study that gates the
promotion of `minimality_judge`'s Borda weight from **0.5** (default,
tiebreaker-only) to **1.0** (peer to `judge` and `judge_explorer`).

The calibration is mandated by the v0.22.0 cross-cutting decision:

> `minimality_judge` weight = 0.5 by default, raised to 1.0 only if
> Phase 7 calibration shows Spearman ρ > 0.4 against gold-standard
> human rank.

## Why 0.4

Spearman ρ ≥ 0.4 is the promotion threshold. The anchor is **G-Eval
(Liu et al. 2023, EMNLP), §3**, which reports ρ = 0.514 on the
SummEval benchmark as the SOTA-defining number for LLM-as-judge work.
0.4 is one floor below the SOTA — high enough to demonstrate the judge
tracks gold, low enough to be reachable on a 30-round corpus given the
verbosity-bias headwind documented in Li et al. (2025).

A judge below 0.4 is not necessarily broken; it is **not yet trusted to
overrule peers**. Keep it at weight 0.5 (tiebreaker) and revisit on the
next prompt-tuning iteration.

## How to run

```bash
uv run python scripts/calibrate_minimality_judge.py
```

Default behavior:

* Reads `tests/calibration/minimality_judge/gold_rankings.jsonl`.
* Mocks the judge (v1 stub: judge rank = gold rank).
* Prints a markdown report to stdout.
* Exits 0 if all acceptance criteria pass, 1 otherwise.

For machine-readable output:

```bash
uv run python scripts/calibrate_minimality_judge.py --report jsonl
```

For the live judge (when wired):

```bash
uv run python scripts/calibrate_minimality_judge.py \\
    --judge-cmd "uv run python -m agents.judge --role minimality_judge"
```

## Refresh cadence

Rerun the calibration on:

1. **Every minor model upgrade** for any judge in `cfg.judge_roles`.
   Per YapBench (Borisov et al. 2026), gpt-3.5-turbo (2023) achieves a
   better YapIndex than every 2025–2026 frontier model — verbosity is a
   post-training artifact, not a capability ceiling. A model bump may
   make `minimality_judge` *worse*, not better.
2. **Every prompt change** to `src/agents/prompts/minimality_judge.md`.
   Per Li et al. (2025) Table VII, prompt-template choice alone moves
   judge robustness up to 40 percentage points. Even a one-line
   directive change can move ρ outside the promotion band.
3. **Every change to judge cohort weights** in `src/config/defaults.py`
   (`judge_role_weights`).
4. **Quarterly**, even when nothing else changes — drift catches.

## Acceptance criteria

Each calibration run evaluates six criteria. The script exits 1 on any
failure:

| # | Criterion | Source |
|:-:|:--|:--|
| 1 | Spearman ρ ≥ 0.4 vs gold | G-Eval §3, SummEval ρ = 0.514 |
| 2 | Position bias: ≤ 1-of-N rank changes under candidate shuffle | Li et al. RQ4 |
| 3 | H8 long-suffix probe: no rank changes from `# additional notes` filler | Li et al. §4.2 H8 |
| 4 | H5 fake-reasoning probe: no rank improvements from "this is optimal" prepends | Li et al. §4.2 H5 |
| 5 | Self-preference: avg rank delta < 0.5 positions when generator matches judge | G-Eval §4 Fig. 2 |
| 6 | Progressive length: empirical inflection ≥ 800 chars | Li et al. Fig. 5 |

## Failure modes & remediation

### Spearman ρ < 0.4

The judge is not tracking gold. Possible causes:

* **Prompt regression.** Diff `src/agents/prompts/minimality_judge.md`
  against the previous version. Check for IAG step erosion, smell
  vocabulary churn, or directive paraphrasing (Bohr §3.2 — paraphrasing
  the verbatim directive collapses Cohen's d from -7.84 to roughly
  the examples-only -2.63).
* **Model regression.** Cross-check the same prompt on the previous
  model version. If the new model performs worse, pin the judge model
  for `minimality_judge` only.
* **Gold corpus drift.** Re-rate 5 random rounds with the careful
  rubric. If your fresh ratings disagree with the stored gold,
  re-collect with two-rater κ ≥ 0.6 inclusion.

### Position bias detected (criterion 2 fails)

The prompt has positional cues. Audit for:

* References to "the first candidate", "candidate A above", or
  ordinal language that anchors the judge to a presentation slot.
* Examples in §6 of `minimality_judge.md` that always show the lean
  case in the same position.

Fix: revise the IAG step to require the judge to read all candidates
*before* sketching its own minimal solution. The IAG step is the most
load-bearing prompt-engineering intervention in the verbosity-bias
literature (Li et al. Table VII: 40.28% → 17.50% Mistral-7B P-ASR).

### H5 / H8 probes fire (criteria 3 / 4 fail)

The IAG step is not doing its job — the judge is being swayed by
candidate text rather than by its own independent solution sketch.
Fix: review `minimality_judge.md` §2 for any softening language (e.g.,
"if you have time" or "consider" instead of "Before reading any
candidate") and restore the imperative.

### Self-preference detected (criterion 5 fails)

The judge prefers candidates from its own model family. Mitigations:

* **Enable the mixed-model cohort.** Per the cross-cutting decision in
  the plan: `judge`, `judge_explorer`, `minimality_judge` should run on
  *different* LLMs where the config allows. Single-model cohorts
  systematically overrate same-family outputs (G-Eval §4, Fig. 2).
* If mixed-model is not yet wired, log the bias in the longitudinal
  panel and reduce `minimality_judge` weight from 0.5 to 0.25 until
  fixed.

### Progressive-length inflection < 800 chars (criterion 6 fails)

The judge is brittle: it changes its rank at very small input-length
thresholds, indicating it has insufficient resistance to verbosity
inflation. Lower the QA gate's
`oversized_demotion_token_threshold` per the formula:

    new_threshold = inflection_chars / 4 / 2

(Where `/ 4` is the rough chars-per-token ratio for English code, and
`/ 2` is a safety margin so the gate triggers at half the empirically
observed inflection.) For example, if the judge inflects at 600 chars,
set `oversized_demotion_token_threshold = 75`.

## Borda integer-cast caveat

Per the Phase 4 implementation report and the calibration script's
**Borda integer-cast diagnostic**, the aggregator at
`src/tournament/voting.py:90-96` does:

```python
scores[label] += int((n - pos) * w)
```

With `weight = 0.5` and `n = 3` candidates (the standard A/B/AB triad),
the per-position contributions floor as follows:

| Position | Raw `(n-pos)*w` | `int()` floor | Loss to floor |
|:--------:|:---------------:|:-------------:|:-------------:|
| 0 (best) | 1.5             | **1**         | 0.5           |
| 1 (mid)  | 1.0             | **1**         | 0.0           |
| 2 (worst)| 0.5             | **0**         | 0.5           |

**Top-two collapse**: positions 0 and 1 both contribute 1 point. The
specialist cannot separate its first-place pick from its second-place
pick — it casts what is effectively a binary "approve / not approve"
vote.

**Bottom collapse**: position 2 contributes 0. The specialist's
last-place vote drops below the noise floor for the peer judges
(weight 1.0, last-place = 1).

### Why this still produces meaningful tiebreaking

Despite the collapse, the specialist can still flip outcomes when peer
judges are *near-tied* (not perfectly tied). The script's
`tiebreaker_demo` exercises a realistic scenario:

* Peer judges produce A=4, B=4, AB=5 (AB leads by 1).
* Specialist that ranks AB last contributes 0 to AB, +1 to its top
  pick, +1 to its middle pick.
* Final: A=5, B=5, AB=5 — the `tiebreak_winner='A'` priority hands the
  round to A. The specialist successfully demoted AB.

The diagnostic asserts this property and prints `meaningful_tiebreaker:
YES` when it holds.

### Remediation paths

If the calibration shows the int-collapse causing real damage (e.g.,
the specialist's vote routinely vanishes in tournament logs), pick one:

1. **Promote weight to 1.0** after Spearman ρ ≥ 0.4 calibration passes.
   At weight 1.0 the int-cast is a no-op (`int(3*1)=3`, `int(2*1)=2`,
   `int(1*1)=1`), and the specialist becomes a peer.
2. **Refactor `BordaAggregator`** at `src/tournament/voting.py:96` to
   accumulate `float` scores and only round at sort time. This
   preserves sub-integer contributions without changing the cohort
   weights. Out-of-scope for Phase 7.

The calibration script's diagnostic prints both options under the
"Remediation" line whenever int-collapse is detected.

## Expanding the gold corpus

Target: **30 historical AutoDev tournament rounds**. Workflow:

1. **Sample.** Pick 30 closed impl tournaments from production logs,
   stratified by smell category (per the v1 corpus distribution: 6
   speculative_generality, 6 dead_code, 6 doc-bloat, 6 single-call
   helpers, 6 ambiguous boundary cases).
2. **Rate.** Apply ONE of:
   * Single-rater careful rubric: a frontier model (Claude Opus or
     stronger) ranks twice with non-overlapping reads under an
     explicit minimality-only prompt. Both passes must agree on the
     full rank order. Drop disagreements.
   * Two-rater human agreement: two reviewers rank independently;
     compute Cohen's κ over pairwise agreement; require κ ≥ 0.6
     (substantial agreement, Landis & Koch 1977). Third-rater break
     ties; drop unbreakable rounds.
3. **Append.** Add each accepted round as a new line in
   `tests/calibration/minimality_judge/gold_rankings.jsonl` with
   `task_id = round_<tournament_id>_<round_idx>` and `rater_notes`
   citing at least one smell name from the closed vocabulary in
   `minimality_judge.md` §5.
4. **Rerun.** `uv run python scripts/calibrate_minimality_judge.py`.

Drop synthetic rounds (`task_id` prefix `synth_`) once the historical
corpus reaches 30 rounds.

## Cross-references

* **Prompt structure** — `src/agents/prompts/minimality_judge.md`
  (Phase 4): IAG (§2), auto-CoT eval (§3), verbosity-bias warning
  (§4), closed smell vocabulary + Bohr directive (§5), exemplars (§6),
  output format (§7).
* **Borda implementation** — `src/tournament/voting.py:74-105`
  (`BordaAggregator.aggregate`).
* **Default cohort weights** — `src/config/defaults.py:181-186`.
* **Calibration script** — `scripts/calibrate_minimality_judge.py`.
* **Gold corpus** —
  `tests/calibration/minimality_judge/gold_rankings.jsonl`.
* **Unit test** — `tests/test_calibration_runner.py`.
* **Integration test** —
  `tests/integration/test_minimality_calibration_baseline.py`.
