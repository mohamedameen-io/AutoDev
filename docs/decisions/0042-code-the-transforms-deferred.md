# 0042 — Defer Cummins-style "Code the Transforms" to v0.23.0+

* **Status:** Deferred (v0.22.0 scope)
* **Deciders:** AutoDev maintainers
* **Date:** 2026-05-09
* **Related:** Anti-Bloat / Code-Size Optimization plan (v0.22.0)

## Context

Cummins et al. (NeurIPS 2024, arXiv 2410.08806, "Don't Transform the Code, Code the Transforms") propose synthesising deterministic AST transformation functions from input/output examples instead of having an LLM rewrite each candidate directly. Their reported precision is dramatically higher: F1 0.97 (CTT) vs 0.75 (TTC) on a 480-program / 16-transform benchmark. The chain-of-thought scaffolding (describe → execute → introspect → fix) maps directly onto AutoDev's existing critic→revision→synthesis loop.

For AutoDev's anti-bloat capability, this approach would mean: when the longitudinal panel (Phase 6 of v0.22.0) surfaces a recurring bloat pattern across merged tasks, the orchestrator kicks off a Cummins-style synthesis loop that generates a deterministic AST rewrite for that pattern, validates it against ≥10 examples, and auto-applies it as a pre-judge normalisation step on new candidates. This shifts AutoDev from "judge bloat after the fact" to "actively shrink it before judging."

## Decision

**We defer adoption to v0.23.0+ and ship v0.22.0 without it.**

## Consequences (Why now is wrong, why later is right)

### Why not v0.22.0

1. **High effort.** Implementing the synthesis pipeline is ~2 weeks on top of the ~14-19 days already in the v0.22.0 plan.
2. **Chicken-and-egg with Phase 6.** Cummins-style synthesis needs a list of recurring patterns to target. We can't enumerate those until the longitudinal panel from Phase 6 has run for ~2 months and surfaced real-world bloat patterns from real AutoDev usage.
3. **Inference cost.** Cummins reports their pipeline averages 11.8 attempts on optimisation transforms. In AutoDev's hot path, this is prohibitive without careful caching.
4. **Risk concentration.** v0.22.0 already adds three new mechanisms (static gate, knowledge seeds, specialist judge) plus a calibration study. Adding a fourth would reduce the diagnostic value of the longitudinal panel — we wouldn't know which mechanism produced which observed change.

### Why later

1. **Strongest evidence base.** Cummins F1 0.97 vs LLM-rewriting F1 0.75 (Table 1) is the most precise rewrite mechanism in the literature.
2. **Architectural fit.** Maps 1:1 onto AutoDev's existing critic→revision→synthesis loop.
3. **Cost can be amortised.** Cheap models (8B–70B) can synthesise good transforms with more iterations (Cummins Fig. 3c). AutoDev could run synthesis on a cheap model in the speculative-execution path, transforms cached for reuse across tasks.

### Trigger conditions for revisiting

Revisit this ADR when ALL hold:
- v0.22.0 has been merged and operating for ≥2 months.
- Phase 6 longitudinal panel has surfaced ≥3 distinct recurring bloat patterns across merged tasks.
- At least one of those patterns has FP rate >20% from the static gate (i.e., the gate alone can't catch it cleanly — the pattern needs a richer rewrite).

If those conditions are not met after 6 months, downgrade this ADR from Deferred to Rejected.

## Alternatives considered

1. **Bundle into v0.22.0** — rejected: scope blowout + diagnostic confusion.
2. **Skip entirely** — rejected: the literature evidence is too strong.
3. **Defer indefinitely with no trigger** — rejected: no forcing function to revisit; ADR rots.

## References

- Cummins et al., "Don't Transform the Code, Code the Transforms: Towards Precise Code Rewriting using LLMs", arXiv 2410.08806, NeurIPS 2024.
- AutoDev v0.22.0 Anti-Bloat plan: see `/Users/mohamedameen/.claude/plans/create-an-ultra-detailed-plan-peaceful-kahn.md` Phase 8.
- Anti-bloat references doc: `autodev-anti-bloat-references.md` §2 entry "Don't Transform the Code, Code the Transforms".
