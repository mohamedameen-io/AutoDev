# `minimality_judge` calibration corpus

Gold-standard rank corpus used to validate the `minimality_judge` specialist
(see `src/agents/prompts/minimality_judge.md`) before its Borda weight is
promoted from the default **0.5** (tiebreaker-only) to **1.0** (peer to
`judge` and `judge_explorer`).

## Purpose

The cross-cutting decision in the v0.22.0 plan reads:

> `minimality_judge` weight = 0.5 by default, raised to 1.0 only if Phase 7
> calibration shows Spearman ρ > 0.4 against gold-standard human rank.

This directory holds the gold corpus that decision is gated on. The
calibration runner lives at `scripts/calibrate_minimality_judge.py`.

## Inclusion criteria

Each gold-rank entry must satisfy ONE of:

1. **Single-rater careful rubric.** A frontier model (Claude Opus or
   stronger) ranks the candidates twice with the careful-rubric prompt,
   non-overlapping reads, and an explicit minimality-only directive.
   Both passes must agree on the full rank order.
2. **Two-rater human agreement.** Two human raters independently
   produce a rank; Cohen's κ over their pairwise agreement must reach
   **≥ 0.6** (substantial agreement per Landis & Koch 1977). When raters
   disagree, a third rater breaks the tie or the round is dropped from
   the corpus.

The plan target is **30 historical AutoDev tournament rounds**. v1 ships
with **5 synthetic rounds** so the metric pipeline can be exercised
end-to-end; the real corpus is populated over time as historical impl
tournaments accumulate.

## Refresh cadence

Rerun the calibration script:

* On **every minor model upgrade** for any judge in `cfg.judge_roles`.
  YapBench's headline finding (gpt-3.5-turbo beats every 2025–2026
  frontier model on brevity) means a model bump may make the judge
  *worse*, not better.
* On **every prompt change** to `src/agents/prompts/minimality_judge.md`.
  Li et al. (2025) Table VII: prompt-template choice alone moves judge
  robustness up to 40 percentage points.
* On **every change to the judge cohort weights** in
  `src/config/defaults.py` (`judge_role_weights`).

## Schema

`gold_rankings.jsonl` — one JSON object per line:

```json
{
  "task_id": "synth_001",
  "task_spec": "Implement a function add(a, b) that returns a + b.",
  "candidates": [
    "<source code for candidate 0>",
    "<source code for candidate 1>",
    "<source code for candidate 2>"
  ],
  "gold_rank": [0, 1, 2],
  "rater_notes": "Free-form rationale citing smell vocabulary."
}
```

Field semantics:

* **`task_id`**: stable identifier; `synth_*` for synthetic rounds,
  `round_<tournament_id>_<round_idx>` for historical rounds.
* **`task_spec`**: the input shown to the candidates. For historical
  rounds, this is the impl tournament's task spec verbatim.
* **`candidates`**: list of raw candidate source code (or diff bundle)
  in the order they were presented to judges. Length must equal the
  length of `gold_rank`. v1 supports 3 candidates (matching the
  tournament's standard A / B / AB triad).
* **`gold_rank`**: list of indices into `candidates`, **best to worst**.
  `[0, 1, 2]` means candidate at index 0 is best, index 2 is worst.
* **`rater_notes`**: free-text rationale. Should cite at least one
  smell name from the closed vocabulary in `minimality_judge.md` §5.

## v1 synthetic corpus

The five seed rounds cover representative bloat patterns:

| `task_id`    | Bloat pattern                  | Smell category         |
| :----------- | :----------------------------- | :--------------------- |
| `synth_001`  | OOP scaffolding for arithmetic | `speculative_generality` |
| `synth_002`  | Defensive type-impossible guards | `dead_code`           |
| `synth_003`  | Single-call-site helper        | `speculative_generality` |
| `synth_004`  | Doc bloat (restated comments)  | `dead_code`            |
| `synth_005`  | Ambiguous: lean is wrong (None at API boundary) | `dead_code` (inverted) |

The fifth case is deliberately ambiguous and inverted — the lean
candidate drops a load-bearing None guard at a public boundary. A
calibrated judge should rank verbose first; a brittle "always prefer
short" judge will fail this case.

## Borda integer-cast caveat

Per the Phase 4 implementation report, the aggregator at
`src/tournament/voting.py:90-96` does `int((n - pos) * w)`, which floors
sub-integer contributions. With weight 0.5 and n = 3 candidates:

* Position 0 (best): `int(3 * 0.5)` = **1**
* Position 1 (middle): `int(2 * 0.5)` = **1**
* Position 2 (worst): `int(1 * 0.5)` = **0**

This means `minimality_judge` at weight 0.5 effectively casts a binary
"approve / not last" vote — a structural property the calibration
script's **Borda integer-cast diagnostic** explicitly tests. See
`docs/minimality_judge_calibration.md` for the remediation paths.
