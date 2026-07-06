# 0048 — External-Benchmark Two-Instrument Measurement Model

* **Status:** **Deferred / aspirational** (2026-07-06) — the rigorous two-instrument design is **gated on keys/cloud throughput**. Phase 1 is governed by [ADR-0050](0050-phase1-mac-subscription-constraint.md), which right-sizes to a coarse Mac + subscription signal because AutoDev is throughput-bound (aborts on `usage_limit_hit`). Reactivate this design when throughput is unlocked; the design below stands as the target.
* **Date:** 2026-07-06
* **Deciders:** Mohamed Ameen
* **Tags:** benchmark, evaluation, regression-gate, statistics, nondeterminism, ci
* **Related:** implementation plan [`thoughts/shared/plans/2026-07-06-external-benchmark-regression.md`](../../thoughts/shared/plans/2026-07-06-external-benchmark-regression.md); reuses the existing `benchmarks/` harness (`benchmarks/runner/{task_runner,scorer,run_benchmark}.py`); mirrors the anti-vacuity + broken-control discipline of `ci/release_preflight_greps.sh::preflight_v100`. Glossary: [`benchmarks/CONTEXT.md`](../../benchmarks/CONTEXT.md).

## Context

We want an external, rigorous, contamination-resistant fitness signal so that changes to AutoDev can be judged as improvements or regressions against **real** coding tasks — not just our own 9 synthetic fixtures. Two facts constrain the design:

1. **AutoDev is nondeterministic and cannot be pinned deterministic.** It drives `claude -p` as a subprocess; `ClaudeCodeAdapter._build_command` (`src/adapters/claude_code.py:226`) exposes only `--model/--max-turns/--effort/--tools`, **no `--temperature` or `--seed`**, and `claude -p` has no sampling-temperature control. Default models are **aliases that auto-resolve to "latest"** (`src/config/defaults.py:113`), so the underlying model *drifts under us*; exact snapshots can only be pinned per-role (`AgentConfig.model` + a scatter of role fields), there is no global pin. On top of token-sampling variance, AutoDev runs multi-step agentic loops (tournaments, retries, worktrees) that add **path** variance. Conclusion: run-to-run variance is irreducible at the source and must be handled statistically.

2. **AutoDev is a repo-level agent.** Its value (localization + multi-file edit + iterative test-driven repair) is exercised by SWE-bench-shaped tasks (repo + issue → patch → hidden tests), and only weakly by function-level suites (single prompt → one function), where we must strip its pipeline to a "lightweight" profile.

The user's primary requirement is purpose **(b) "tell me if my change helped"** — the demanding master, because detecting a *small improvement* on a *noisy* agent needs statistical power, which costs money. A single-run pass-rate on ~50 stochastic tasks has a standard error around ±7%, which would drown a real +10pp change.

## Decision

Adopt **two distinct instruments**, each honest about its job:

1. **Sensitivity instrument** — fires **manually / on-demand** (`workflow_dispatch` + a CLI; there is no PR-review event in this branch→merge workflow), run deliberately during deep-pipeline optimization work; answers *"did this change help?"*. Achieves power via **repeated runs + a paired comparison against a side-by-side reference build**:
   * Run **old AutoDev (merge-base / `main`)** and **new AutoDev (PR head)** *contemporaneously*, on the *same* fixed task slice, **k times each**, then compare **paired** (per-task pass-probability; McNemar / bootstrap CI). Side-by-side is mandatory: it holds the drifting model constant so the delta is attributable to the AutoDev change, not to Anthropic's release cadence. Pin exact model snapshots across roles where practical as secondary hardening.
   * The slice is drawn from the tier that actually exercises AutoDev's core (SWE-bench-Lite micro-slice), **not** the function-level tier.
   * A verdict of **inconclusive** is first-class — when the CI straddles zero, the honest output is "no detectable effect at this sample size," never a forced binary.
   * **Target resolution: MDE = ±10pp** on the paired pass-rate. **Verdict via a bootstrap CI on the mean per-task paired difference** (new − old, each task's pass-rate over its k repeats): *helped* if the CI is entirely > 0, *hurt* if entirely < 0, *inconclusive* if it straddles. Chosen over McNemar/t-test because it handles small N + repeats and yields an interpretable effect size. The CI level (hence `k`) is tuned so a ±10pp check fits the **~$800 / <2h per-check ceiling**. Detecting a smaller effect (e.g. ±5pp) is a separate, ~4× more expensive decision (cost ~ 1/MDE²).

2. **Release regression instrument** — fires on each **`0.x.0` tag, debounced to ≤ once/day**; answers *"did we break anything?"*. A **fixed 200-instance SWE-bench slice, single-run**, compared to the **stored prior-release baseline** (`benchmarks/baselines/<benchmark>/<version>.json`) with a **~13% drop threshold** (sized to N=200's noise floor — deliberately *not* the harness default 0.10, which is below noise at this N); FAILs on anti-vacuity (0 or `< expected_min` tasks) and excessive ERROR-rate. The **first** gated release establishes the baseline (no gate on run 1). **Full Verified-500** is a *separate*, **manual, milestone-only** headline/citeable run.

Supporting decisions:
* **Adapter + scorer layer over the existing `benchmarks/` harness** (keep the solve-half of `run_task`, swap the score-half; defer scoring to each benchmark's *official* scorer — mandatory for SWE-bench Docker and LiveBench's server-side private tests).
* **A pilot variance study is the first deliverable and a go/no-go gate.** ~20 candidate SWE-bench-Lite instances × ~6 repeats of current `main` (~120 runs, **capped ~$500–1k**). Outputs: (1) the pinned **sensitive-band slice** (per-task pass-probability ≈ 0.2–0.8 — only flippable tasks carry paired signal); (2) the run-to-run **flip rate** → a power calc for `k`/`N`. **Go/no-go:** if a ±10pp check exceeds the ~$800 ceiling, (b) is declared unaffordable on SWE-bench — fall back to the release regression gate and demote "did it help?" to the noisy function-level proxy, rather than shipping a PR gate that is secretly noise.
* Function-level suites (LiveBench/EvalPlus/…) are **cheap canaries**, explicitly **not** the authority on "did it help?".

## Consequences

* The sensitivity instrument costs **2 variants × k repeats × N tasks** per check — acceptable only because it fires *manually* during optimization work, not automatically.
* **Standing cost envelope** (at current ~8×`0.x.0`/month cadence): sensitivity **~$800/on-demand check**; regression **~$1.5k per gated `0.x.0` (~$12k/mo)**; full-Verified-500 milestone **~$3.75k**. Claude spend is **real** and flows through the account; cost is a *warn*, never a hard fail. This supersedes the earlier "release-tag-only, per-PR-never" framing.
* We cannot report a trustworthy "did it help?" number until the pilot gives us a variance estimate to size the instrument; any pre-pilot delta is directional only.
* Two notions of "baseline" now coexist and must not be conflated: the **reference build** (live, side-by-side, PR instrument) vs the **release baseline** (stored JSON, release instrument). See glossary.
* Real Claude spend flows through the account on every run; cost is a **warn**, never a hard fail.

## Alternatives considered

* **Stored-baseline-only comparison for the PR instrument** — rejected: confounded by model drift (a drop may be the model changing, not AutoDev).
* **Function-level tier as the sensitivity instrument** — rejected: cheap, but runs a *stripped* AutoDev on single-function problems, so it is blind to exactly the deep-pipeline improvements (b) cares about.
* **Reduce nondeterminism at the source (temperature=0 / seed)** — impossible: no such knob exists in AutoDev or `claude -p`.
* **Single-run, no repeats** (the current `benchmarks/` behaviour) — insufficient for (b); noise ≈ the effects we want to detect.

## Open questions (pending grilling + pilot)

* `k`/`N` for the sensitivity slice — quantified by the pilot (approach/MDE/verdict/ceiling decided).
* **Compute substrate** — where the faithful-container solve + SWE-bench score run (self-hosted Linux x86_64 VM vs Modal vs GH large runners); the maintainer's Mac (ARM) cannot host it.
* **Claude billing model in CI** — API key (pay-per-token; makes the $ envelope real API spend, possibly higher) vs an injected subscription OAuth token (ToS / rate-limit risk). Governs whether the cost envelope holds.
* **SWE-bench image source** for the faithful path (prebuilt pull vs local build).
