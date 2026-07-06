# 0050 — Phase-1 Runtime Constraint: Mac + Claude Subscription → Coarse Local Signal

* **Status:** Accepted (Phase-1 governing decision; 2026-07-06)
* **Date:** 2026-07-06
* **Deciders:** Mohamed Ameen
* **Tags:** benchmark, constraint, runtime, rate-limits, throughput, macos, arm64, scope
* **Related:** governs Phase 1 and **defers** the rigorous design in [ADR-0048](0048-benchmark-two-instrument-measurement-model.md); portfolio scope [ADR-0049](0049-swebench-centric-portfolio.md); **Phase-1 plan** [`thoughts/shared/plans/2026-07-06-benchmark-phase1-coarse-tripwire.md`](../../thoughts/shared/plans/2026-07-06-benchmark-phase1-coarse-tripwire.md) (what to build now); Phase-2 continuation [`…-external-benchmark-regression.md`](../../thoughts/shared/plans/2026-07-06-external-benchmark-regression.md); glossary [`benchmarks/CONTEXT.md`](../../benchmarks/CONTEXT.md).

## Context

A hard runtime constraint surfaced late in design: the benchmark **must run on the maintainer's MacBook (Apple Silicon / ARM) using the personal Claude *subscription* — no API keys, no cloud compute for the agent.**

This **inverts the binding resource from dollars to throughput/quota**, and that inversion is fatal to the rigorous design:

* AutoDev **bursts many parallel `claude -p` calls per task** (plan/impl tournaments, `worktree_pool` `asyncio.gather`); a config comment already notes halving subprocess counts to "cut 429/529 rate-limit pressure" (`schema.py:974`).
* AutoDev **treats hitting the plan cap as a fatal infra failure**: `InfraFailureCircuitBreaker` trips on `{auth_failed, rate_limited, server_error, usage_limit_hit}` (`src/config/schema.py:1521`; `usage_limit_hit` = *monthly / plan cap*, `:1526`) → `InfrastructureCircuitOpenError` aborts the run. Field history matches (Synaptix runs died on a triple `LLMRateLimitError`).
* A subscription sustains only **~10–20 heavy tasks/day** before 5-hour/weekly caps throttle. The rigorous instruments need hundreds–thousands of runs (200-slice; 20×5×2 paired repeats) → **weeks–months of wall-clock**, and the monthly cap kills a long sweep outright.

Therefore the two-instrument, paired, repeated design (ADR-0048) is **unreachable** under this constraint. Quota, not money, is the wall — and it cannot be bought up without keys.

## Decision

Phase 1 is a **single, small, coarse, Mac-runnable regression tripwire**; the rigor is deferred.

* **Footprint:** a **fixed ~15–20 SWE-bench-Lite instance slice**, **single-run**, run **on-demand / overnight** on the Mac.
* **Solve:** **host-side arm64** — clone `repo@base_commit`, best-effort per-instance venv; AutoDev's `test_runner` self-repair engages where deps install on arm64 and **gracefully degrades** (gate off) where they don't. This is **not** the faithful x86 container (which defers with the rigor).
* **Score:** **sb-cli free cloud scoring** — `predictions.jsonl` → SWE-bench AWS (`pip install sb-cli`; free email-issued `SWEBENCH_API_KEY`; **not** an Anthropic key). Public-repo patches upload to an external service (accepted). No local Docker / x86 emulation. Fallback: local emulated x86 Docker (keyless, slow).
* **Throughput safety:** a **quota-aware pause/resume** wrapper around the serial solve loop catches `usage_limit_hit` / `rate_limited` aborts, sleeps until reset, and **re-runs** that instance (status **ERROR-until-complete**, never a false FAIL). Set `tournaments.max_parallel_subprocesses = 1` to cut within-task burst.
* **Honest claims:** catches only **large (~±20–30pp) regressions**; at best a **directional** improvement hint. **Not** the paired "did it help?" instrument.
* **Pilot (redefined) = the first deliverable:** discover which ~15–20 Lite instances actually run on arm-host + sb-cli; measure real per-instance wall-time and **sustainable instances/day** on the subscription; establish the first baseline. The measured throughput decides what cadence (if any) is even possible.

## Consequences

* **Deferred for Phase 1** (all remain designed in ADR-0048, unlockable): the sensitivity/paired-repeat instrument, the 200-instance nightly/per-release regression, faithful-container execution, the full-Verified-500 headline, and (already dropped in ADR-0049) function-level suites + the sandbox.
* Phase-1 usefulness is modest and we say so — a big-regression tripwire, not a capability meter.
* New external dependency: sb-cli (free token; uploads public-repo patches).
* The reusable core still lands (the `benchmarks/` solve/score adapter split, `predictions.jsonl` handoff, baseline + `score_benchmark_results` comparison), so unlocking rigor later is additive, not a rewrite.

## Unlock trigger

Permit **`ANTHROPIC_API_KEY` (or a higher-throughput plan) + a Linux x86_64 runner (or Modal)** → reactivate ADR-0048's rigorous two-instrument design.

## Alternatives considered

* **Run the rigorous design slowly over weeks on the subscription** — rejected: monthly cap aborts long sweeps; results go stale mid-run; model drifts.
* **Inject the subscription OAuth token into cloud/CI** — rejected: ToS + throttling; silently caps a sweep mid-run.
* **Local emulated x86 faithful container on the Mac** — rejected for Phase 1: Docker + Rosetta emulation is heavy on a laptop and per-instance AutoDev-in-emulated-container is fragile; sb-cli offloads the x86 work cleanly.
