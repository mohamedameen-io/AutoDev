# 0049 — SWE-bench-Centric Portfolio (defer function-level suites)

* **Status:** Proposed (phase-1 scope; user-agreed 2026-07-06, explicitly revisitable in a later phase)
* **Date:** 2026-07-06
* **Deciders:** Mohamed Ameen
* **Tags:** benchmark, portfolio, scope, contamination
* **Related:** [ADR-0048](0048-benchmark-two-instrument-measurement-model.md) (measurement model — this follows from it); plan [`thoughts/shared/plans/2026-07-06-external-benchmark-regression.md`](../../thoughts/shared/plans/2026-07-06-external-benchmark-regression.md); glossary [`benchmarks/CONTEXT.md`](../../benchmarks/CONTEXT.md).

## Context

The project began as "benchmark AutoDev on LiveBench," then broadened to a 3–5 benchmark portfolio spanning function-level suites (LiveBench-coding, EvalPlus, LiveCodeBench, BigCodeBench) and repo-level SWE-bench. Once the measurement model settled on a **delta-based paired signal** (ADR-0048 — we compare *two AutoDev builds*, not AutoDev against a leaderboard), the function-level tier's rationale collapses for a **repo-level** agent.

## Decision

**Phase-1 portfolio = the SWE-bench family only:**
- **SWE-bench Verified** — the anchor, the citeable external headline, and the release full-set.
- **SWE-bench Lite** — source of the pinned sensitivity-slice (mid-difficulty instances).
- **SWE-bench-Live** — a contamination-fresh *repo-level* rotation for the release instrument.

**Defer** LiveBench, EvalPlus, LiveCodeBench, BigCodeBench, and multilingual repo-level suites to a later phase.

## Rationale (the non-obvious parts a future reader will question)

1. **Function-level can't measure "did it help?" for a repo agent** — it runs a *stripped* AutoDev (intake/framing/diagnosis/test-repair off) on single-function problems, blind to the repo-level capability we're gating on.
2. **Contamination-freshness cancels in a delta.** Function-level's headline virtue only distorts *absolute* scores. Our signal compares two builds on the *same* tasks; a contaminated task helps both equally and cancels in the paired/delta comparison. So it buys us nothing here.
3. **"Did it crash / still emit valid output"** is already caught by AutoDev's ~4,200 tests + E2E fake-binary suite — orders of magnitude cheaper than a benchmark.
4. **SWE-bench's real gap is language coverage** (Verified/Lite are Python-only). That gap is filled by *multilingual repo-level* suites (SWE-bench-Multilingual, Multi-SWE-bench, Aider Polyglot), **not** by Python-heavy function-level sets.
5. **SWE-bench Verified is itself famous and citeable**, so "we need a recognizable number" is already satisfied.

## Consequences

* Deletes the entire **sandbox** subsystem and the **untrusted-code-execution** risk class from phase 1, plus the `native_samples`/LiveBench adapters — roughly half the original build (former plan phases P3/P4 and most of P6).
* **Revisit triggers** for a later phase: (i) AutoDev's users are polyglot → add a multilingual *repo-level* suite; (ii) a marketing need for a famous *function-level* leaderboard number → add exactly one (LiveCodeBench, date-windowed), labelled non-load-bearing.

## Alternatives considered

* **Keep one function-level suite for external credibility** — deferred; SWE-bench Verified already provides a citeable number, so this is only worth it for a specific function-level-leaderboard audience.
* **Keep EvalPlus as a cheap per-commit smoke canary** — rejected; redundant with the existing test suite, which catches gross breakage far cheaper.
