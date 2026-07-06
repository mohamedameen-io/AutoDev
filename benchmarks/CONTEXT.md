# Benchmark subsystem — glossary (bounded context)

Ubiquitous language for the external-benchmark regression signal. This file is a **glossary only** — no implementation details. Decisions live in `docs/decisions/` (see [ADR-0048](../docs/decisions/0048-benchmark-two-instrument-measurement-model.md)); the build plan lives in `thoughts/shared/plans/2026-07-06-external-benchmark-regression.md`.

## Core terms

- **Solve** — the half of a benchmark run that drives AutoDev (`init` → `plan` → `execute`) on a prepared workspace and recovers its change as a diff/program. Deterministic *inputs*, nondeterministic *output*.
- **Score** — the half that judges a solved artifact using the benchmark's **official** scorer (SWE-bench Docker harness; LiveBench/EvalPlus test execution). Decoupled from Solve via a serialized handoff file.
- **Variant** — a specific AutoDev build under test, pinned as `wheel@<commit>` and stamped `--autodev-version "<ver>-<branch>-<sha>"`. A comparison is always *between variants*.
- **Task** — one benchmark problem. For SWE-bench the upstream term is **instance** (`instance_id`); we use "task" generically and "instance" when SWE-bench-specific.

## Measurement model

- **Sensitivity instrument** — the **PR-time** benchmark. Answers *"did this change help?"*. Small task slice, **repeated k times per variant**, paired. Runs on large PRs.
- **Regression instrument** — the **release-time** benchmark. Answers *"did we break anything?"*. Larger task set, breadth over repetition. Runs on release tags.
- **Reference build** — the *live, side-by-side* run of **old** AutoDev (merge-base / `main`) recomputed on every PR check. The sensitivity instrument's baseline. Exists to hold the drifting model constant. **Not to be confused with** the Release baseline.
- **Release baseline** — the *stored* `baselines/<benchmark>/<version>.json` scorecard from the prior release. The regression instrument's baseline. Coarse and drift-exposed; trusted only for *large* regressions.
- **Flip** — a task changing outcome (PASS↔FAIL) between two runs. A flip can be caused by a real capability change *or* by run-to-run variance; distinguishing the two is the whole measurement problem.
- **Run-to-run variance** — the same variant producing different pass/fail on the same task across repeats, because AutoDev is nondeterministic (no temperature/seed; agentic path variance). Irreducible at the source; handled by repeats + pairing.
- **Discordant pair** — in the paired comparison, a task where the two variants disagree (one PASS, one FAIL). Only discordant pairs carry signal about *which* variant is better (McNemar).
- **Inconclusive** — a first-class verdict of the sensitivity instrument: the paired confidence interval straddles zero, so there is no detectable effect at the current `k`/`N`. Distinct from "no change."

## AutoDev-execution profiles

- **Full pipeline** — AutoDev with intake/framing/diagnosis + `test_runner` QA gate on. Used where the repo's real test env is present (SWE-bench faithful container), so iterative test-repair engages.
- **Lightweight profile** — AutoDev with those phases + `test_runner` disabled (via env kill-switches + a `config_patch`). Used for function-level suites where there are no local hidden tests. Tests only a *stripped* AutoDev.
- **Faithful container** — running the Solve step *inside* a SWE-bench per-instance Docker image (deps present) with Claude Code CLI + auth injected. The high-fidelity execution mode for the anchor tier.
- **Native scorer** — a benchmark's own scorer that executes untrusted model code (LiveBench/EvalPlus/LiveCodeBench/BigCodeBench). Always run inside the **sandbox** (`--network none`, non-root, ro-FS, ulimits, wall-clock kill).

## Status vocabulary (per task)

- **PASS / FAIL** — the behavioural verdict (official scorer). A real capability outcome.
- **ERROR** — infrastructure failure (harness crash, timeout, or a **quota abort** — AutoDev's circuit breaker tripping on `usage_limit_hit`/`rate_limited`) — *not* an AutoDev regression. Kept distinct from FAIL so infra/quota flakiness can't masquerade as a capability drop, and so a gate can fail on excessive ERROR-rate (anti-vacuity).

## Phase-1 reality (ADR-0050) — what we actually build first

The rigorous vocabulary above is the *target*. Under the Phase-1 constraint (Mac + Claude subscription, no keys → **throughput-bound**), most of it is **deferred**. Phase-1-specific terms:

- **Coarse regression tripwire** — the Phase-1 instrument: a fixed ~15–20 SWE-bench-Lite slice, single-run, on-demand. Catches only *large* (~±20–30pp) regressions; a *directional* improvement hint at best. The honest, small stand-in for the deferred **Sensitivity** + **Regression** instruments.
- **Host-arm64 solve** — Phase-1 execution: AutoDev runs on the Mac in a best-effort arm64 venv (self-repair where deps install, `test_runner` off where they don't). The deferred alternative is **Faithful container** (x86, gated on a Linux runner).
- **Quota-aware pause/resume** — the wrapper that keeps a sweep alive on a rate-limited subscription: catch a quota/rate abort → sleep until reset → re-run the instance (ERROR-until-complete, never a false FAIL). Also sets `tournaments.max_parallel_subprocesses = 1` to cut within-task burst.
- **sb-cli cloud scoring** — Phase-1 scoring: `predictions.jsonl` → SWE-bench AWS via a *free* `SWEBENCH_API_KEY` (not an Anthropic key). Avoids local x86 Docker on ARM; uploads public-repo patches.
- **Throughput-bound** — the Phase-1 binding constraint: the limiter is Claude *quota per unit time* (weekly/monthly caps), not dollars — and it can't be bought up without keys. This is *why* the rigorous design is deferred.
