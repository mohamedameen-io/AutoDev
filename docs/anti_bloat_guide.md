# Anti-Bloat Operator Guide (v0.22.0)

This guide is for operators turning the v0.22.0 anti-bloat layer on,
reading the longitudinal dashboard, and deciding whether to ship a
proposed change. It does **not** re-derive the literature foundation —
see `autodev-anti-bloat-references.md` and the v0.22.0 implementation
plan for that.

---

## Ship / no-ship criteria

These are the exact rules from the cross-cutting decisions in the v0.22.0
plan (item 6). Use them as a checklist before promoting any change that
touches the gate, the seeds, or the `minimality_judge` weight.

### Ship

All three must hold for at least one full week of merged tasks:

1. **Median LOC per merged task drops by ≥10%** vs. the calibration
   baseline (`tests/golden/anti_bloat_baseline_v0_21.jsonl`).
2. **Test-pass rate stays within ±2%** of the baseline (no correctness
   regression).
3. **Fewer than 3 false-positive `code_size` warnings per week** in
   reviewer feedback.

### No-ship

Either of these triggers an immediate revert (any one of the three kill
switches in the cross-cutting decisions):

- **Test-pass rate drops by >2%.**
- **Mutation-test kill-rate drops by >5%.**

Quick revert paths (no phase requires the others at runtime):

| Switch                                                                | Effect                                  |
| --------------------------------------------------------------------- | --------------------------------------- |
| `cfg.qa_gates.code_size = false`                                      | Disables the static-analysis gate       |
| Revert `cfg.tournaments.impl.judge_roles` to `["judge"]`              | Removes the `minimality_judge`          |
| Delete `seeds/anti_bloat_v1.jsonl` and `.autodev/seed_packs.json`     | Stops anti-bloat seed injection         |

---

## Reading the markdown report

Run:

```bash
autodev metrics anti-bloat --from <last-tag> --report markdown
```

You get a row per commit:

```
| commit  | task        | tokens | def_ratio | doc_dens | fns | loc | cc_max | dead | yap |
|---------|-------------|-------:|----------:|---------:|----:|----:|-------:|-----:|----:|
| abcdef1 | feat: x     | 412    | 0.04      | 0.83     | 6   | 102 | 5      | 0    | 102 |
| 9fedcba | refactor: y | 187    | 0.02      | 0.71     | 3   |  46 | 3      | 0    |  46 |
```

Column meanings:

| Column      | Source                          | What it means                                                                                       |
| ----------- | ------------------------------- | --------------------------------------------------------------------------------------------------- |
| `commit`    | git                             | First 7 chars of the merged SHA                                                                     |
| `task`      | git subject                     | First 60 chars of the commit subject (proxy for task scope)                                         |
| `tokens`    | Bohr §3.4                       | Whitespace-token count over all changed Python files. Order-of-magnitude proxy for input size.      |
| `def_ratio` | Bohr §3.4                       | `(try + None compares + assert) / sloc`. Higher = more defensive scaffolding.                       |
| `doc_dens`  | Bohr §3.4                       | `docstrings / (functions + classes)`. >1.0 means modules have docstrings too.                       |
| `fns`       | AST                             | Total functions (sync + async) in the changed files                                                 |
| `loc`       | radon raw                       | Executable LOC (non-blank, non-comment)                                                             |
| `cc_max`    | radon CC                        | Max cyclomatic complexity per file                                                                  |
| `dead`      | vulture (≥80% confidence)       | Count of dead symbols. Subprocess fallback — 0 if vulture isn't installed.                          |
| `yap`       | YapBench §3.3 (v1 placeholder)  | Currently equals aggregate `loc`. Phase 6.5 will swap in `loc − shortest_passing_candidate_loc`.    |

**What to look for:**

- A new commit with a higher `def_ratio` than its peers and no new
  exception path in the diff is a candidate for a reviewer "is this
  defensive scaffolding warranted?" comment.
- A jump in `doc_dens` for a refactor commit (no new entities) is the
  classic "comments restating the code" smell — Bohr §3.4 documents this
  directly.
- `dead > 0` is almost always a real finding — vulture's 80% confidence
  floor is conservative.

---

## The Bohr defensive-ratio caveat (Bohr §5.2)

Concise code can have a **higher defensive ratio** without being more
defensive *in absolute terms*. The denominator (`sloc`) shrinks faster
than the numerator (try / assert / None-compares) when an LLM compresses
straight-line logic but leaves error handling intact.

**Practical rule:** treat a rising `def_ratio` as a flag only when paired
with a rising `loc`. A `def_ratio` increase + `loc` decrease is usually
the *good* outcome of a refactor (less boilerplate around the same number
of guard rails).

---

## Refreshing the calibration baseline

The static-analysis gate ships at `severity="warn"`. Promotion of any
rule from `warn` → `block` requires measured precision ≥85% on a 50-PR
calibration sample (cross-cutting decision 1).

To refresh:

```bash
uv run python scripts/calibrate_code_size.py
```

The corpus lives at `tests/calibration/code_size/`. Each PR is a subdir
holding `diff.patch` + `label.json` (the human "bloat / not-bloat"
verdict). The script runs the gate against each diff and reports per-rule
precision; rules that drop below 85% on a calibration refresh get
auto-demoted from `block` to `warn`.

---

## Rejecting a noisy seed

If the `seeds/anti_bloat_v1.jsonl` injection is producing false positives
in critic / reviewer output:

1. Identify the offending seed by ID in the reviewer transcript.
2. Use the existing knowledge-ledger reject path:
   ```bash
   autodev plugins knowledge reject --id <seed-id>
   ```
   This appends a tombstone to `.autodev/knowledge.jsonl` so the
   bigram-Jaccard dedup at threshold 0.6 will skip it on future runs.
3. If the rejection rate exceeds 3 per week, consider removing the seed
   from `seeds/anti_bloat_v1.jsonl` and reseeding (the
   `seed_pack_if_missing` helper is idempotent via
   `.autodev/seed_packs.json`).

---

## Populating the benchmark caches

Phase 0 ships READMEs only; the actual datasets are too large to commit.
To populate:

| Cache                            | Source                                                              | Use                                              |
| -------------------------------- | ------------------------------------------------------------------- | ------------------------------------------------ |
| `tests/benchmarks/yapbench/`     | `huggingface.co/datasets/tabularisai/yapbench_dataset` (304 prompts)| `scripts/yap_regression.py` longitudinal panel   |
| `tests/benchmarks/shortercode/`  | `github.com/DeepSoftwareAnalytics/ShorterCode` (828 pairs)          | Phase 7 `minimality_judge` calibration           |
| `tests/benchmarks/enamel/`       | `github.com/q-rz/enamel` (HumanEval problems #10/#31/#36/#55)       | Phase 7 adversarial test cases                   |

See each directory's `README.md` for the exact download command.

Once `tests/benchmarks/yapbench/yapbench_dataset.parquet` (or `.jsonl`)
exists, `scripts/yap_regression.py` will switch from its v1 stub mode to
live scoring (TODO comment in the script marks the integration point).

---

## Recency ≠ brevity (YapBench warning)

YapBench's headline finding (Borisov et al. 2026, §3.3): **gpt-3.5-turbo
(2023) achieves YapIndex 22.7 — better than every newer 2025–2026
frontier model.** Verbosity is a post-training artefact, not a capability
ceiling, and a model upgrade may make bloat *worse*, not better.

**Operational implication:** when `cfg.platform_model` changes (or any
sub-agent gets reassigned to a new model), inspect the `yap` column trend
in the next week of merged tasks. A monotonic rise is the canonical
"new model is more verbose" signature, not a regression in your code.

Phase 6.5 will add a per-model breakdown to the markdown report so this
attribution is automatic; for v0.22.0 it requires reading commit logs in
parallel with the longitudinal panel.

---

## Example report (small)

This is what `autodev metrics anti-bloat --from v0.21.0 --report markdown`
might emit for a 4-commit window — useful for a PR description or weekly
ops review:

```
| commit  | task                                  | tokens | def_ratio | doc_dens | fns | loc | cc_max | dead | yap |
|---------|---------------------------------------|-------:|----------:|---------:|----:|----:|-------:|-----:|----:|
| 4d3c2b1 | feat(orchestrator): retry budget      |    301 |      0.05 |     0.80 |   4 |  72 |      4 |    0 |  72 |
| 5e4d3c2 | refactor(critic): inline 1-call helper|    142 |      0.04 |     0.83 |   2 |  31 |      2 |    0 |  31 |
| 6f5e4d3 | feat(qa): dead_code rule              |    498 |      0.06 |     0.78 |   7 | 124 |      6 |    1 | 124 |
| 70a1b2c | docs: refresh README                  |      0 |      0.00 |     0.00 |   0 |   0 |      0 |    0 |   0 |
```

Reading order:

1. The `docs:` row is all zeros — no Python changed (sanity check).
2. The `refactor:` row's `loc` dropped relative to the `feat:` rows; this
   is the kind of trend the anti-bloat layer is trying to encourage.
3. The `feat(qa)` row introduced a `dead=1` finding — investigate before
   merging.

---

## Honest scope reminder

Per Token Sugar §I (Sun et al. 2025): only ~25% of GPT-4 Python tokens
are syntax. The static-analysis gate (Phase 1) catches deterministic
*syntactic* bloat — that's necessary but only ~25% of headroom. The
remaining ~75% (semantic redundancy, gratuitous abstractions, repeated
idioms) lives in the `minimality_judge` tournament role (Phase 4) and
the longitudinal panel itself (this report). Don't expect the gate to
catch what it cannot.
