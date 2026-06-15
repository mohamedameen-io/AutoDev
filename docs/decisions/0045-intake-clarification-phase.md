# 0045 — Intake & Clarification Phase: gather, enrich, and clarify under-specified specs before autonomous planning

* **Status:** Accepted
* **Date:** 2026-06-15
* **Deciders:** Mohamed Ameen
* **Tags:** orchestrator, planning, agents, config, state, adapters
* **Related:** [0044-framing-altitude-phase.md](0044-framing-altitude-phase.md) (immediate upstream neighbour in the plan pipeline), companion design doc [`intake_clarification_phase_design.md`](../design_documentation/intake_clarification_phase_design.md), ADR-0008 (deterministic FSM), ADR-0002 (append-only CAS ledger), ADR-0001 (stateless subprocess invocations)

## Context

AutoDev is meant to behave like an autonomous software expert, but its front door is **all-or-nothing**: a spec is either accepted and planned, or rejected outright. There is no step that does what a senior engineer does first — *read around the ticket, pull the linked issue, skim the code, and ask the two questions that actually matter* — before committing to a plan.

The current entry path, verified in `src/`:

1. **Binary front-gate, no enrichment.** `src/orchestrator/spec_validator.py` (`validate_spec` / `validate_spec_text`, v0.36.0 "G1") scans the intent for length, a scope marker, and an acceptance signal, and returns `SpecValidationResult(ok, reasons)`. On failure (`spec_too_short`, `spec_no_scope_markers`, `spec_no_acceptance_signal`, `spec_missing`, `spec_empty`) the CLI **rejects** and tells the operator to pass `--skip-spec-validation` (`spec_validator.py:11-12`). The gate detects under-specification but does nothing about it — it cannot gather, enrich, or ask.
2. **Symptom-anchored, take-it-as-given intake.** Whatever intent arrives is threaded verbatim into every planning agent — explorer, domain_expert, framing, architect (`plan_phase.py:705,754,780`; the same symptom-anchoring that motivated ADR-0044). If the ticket is a thin summary, the whole pipeline plans against a thin summary.
3. **Clarification exists in the architect but is gated off.** The architect prompt *has* a `CLARIFY` mode — "Request is ambiguous and cannot proceed without user input → ask up to 3 questions" (`architect.md:445`) — and a BRAINSTORM Phase-3 that ends "Present the approaches to the user" (`architect.md:477-481`). Both are **forbidden on the autonomous path** by the shared autonomy clause ("running unattended … no operator … do not ask clarifying questions", `architect.md:715,1368-1382`). So the *one* place that could ask is structurally muzzled, and it would ask in the wrong place anyway (mid-plan, after the altitude decision, with no resume story).

**Field evidence (this is not hypothetical).** On the Synaptix Mistral-429 benchmark, the repo's `bug.md` was a stripped summary that the G1 gate **rejected** for `spec_no_acceptance_signal`. The canonical GitHub issue #199 — the *same* bug — carried a full problem statement (three named mechanisms, a "what needs to be fixed" list) that the operator never saw because nothing pulls the linked issue. The operator's only sanctioned move was `--skip-spec-validation`, which *suppresses the signal* instead of acting on it. A manual intake (pull issue #199 → gather the call-path/contract facts from the repo → ask four constraint questions: provider-lock, contract-latitude, done-bar, altitude-latitude) produced a spec that passed G1 **naturally** and planned cleanly — while leaving the altitude decision untouched for ADR-0044's framing phase to make.

**Affected subsystems:** orchestrator (`src/orchestrator/plan_phase.py`, `spec_validator.py`, new `intake_phase.py`), agents (`src/agents/prompts/`), config (`src/config/`), state (`src/state/schemas.py`, `src/state/evidence.py`), adapters (external-source gather: `gh` / Jira-MCP).

The encouraging part, as with ADR-0044: most of the machinery exists — the explorer already gathers repo context, the evidence/ledger plumbing is reusable, and the architect's CLARIFY/BRAINSTORM contracts already describe the *question shape*. This is mostly **routing + a new front phase + a non-LLM source-gather**, not a new brain. The hard, high-value part is the **boundary discipline**: intake must elicit *constraints*, never *solutions* — or it silently pre-empts ADR-0044.

## Options Considered

### Option 1: Dedicated pre-plan Intake & Clarification phase (chosen)

**Description:** Insert an intake phase at the very front of `run_plan_phase`, before exploration's findings feed framing. It (a) runs a **completeness assessment** (extend `spec_validator` to return structured *gaps* rather than a binary reject); (b) when gaps exist, **gathers** context autonomously from the repo (reusing the explorer pass) and from external sources (referenced GitHub issues/PRs via `gh`, Jira via MCP, prior AutoDev sessions); (c) **enriches** the intent into a provenance-cited spec draft; (d) generates a **small, batched set of constraint-focused clarifying questions** (≤ `max_questions`, each with a recommended default); (e) **asks the operator once** (or, headless, applies recommended defaults under a logged policy); (f) **locks** the enriched+answered spec to `.autodev/spec.md`, records the Q&A + provenance + assumptions in the ledger, and proceeds fully autonomously. **On by default**, but a **no-op for well-formed specs** (completeness passes → 0 added LLM calls), so the common case is untouched.

**Pros:**
- Does the thing the front-gate only *detects*: turns "rejected, go rewrite it" into "gathered, enriched, asked, proceeding."
- The linked-issue insight (#199 ≫ `bug.md`) is captured for free — the canonical ticket is pulled, not the pasted summary.
- **Single human touchpoint, up front.** Preserves the autonomy contract: ask once, then hands-off — no mid-run prompts, resume-safe.
- Clean separation from ADR-0044: intake elicits *constraints*; framing still owns the *altitude* decision. The two compose instead of colliding.
- Common-case cost is **zero** added LLM calls (deterministic completeness gate; gather/enrich fire only on gaps).
- Headless degradation is a first-class mode (assume documented defaults / block / fail), so cron and CI don't deadlock.

**Cons:**
- Adds a human-interaction surface to a system whose whole pitch is autonomy — must be carefully bounded to "ask once, only what artifacts can't answer."
- New external-source surface (`gh`, Jira-MCP): network dependency, auth, and a contamination risk (could pull "the answer" PR) that needs a source allowlist.
- Enrichment can hallucinate facts into the spec unless constrained to cited evidence and verified.
- Shifts where `--skip-spec-validation` semantics live; the CLI surface and operator mental model change.

### Option 2: Do nothing — keep the binary validator

**Description:** Keep `spec_validator` as a reject-or-pass front-gate; operators rewrite thin specs by hand and pass `--skip-spec-validation`.

**Pros:**
- Zero new surface, zero network dependency, zero added cost, determinism baseline unchanged.
- No risk of asking the wrong question or contaminating the altitude decision.

**Cons:**
- The expert behaviour (gather + ask) is simply absent; under-specified tickets either bounce or get planned against ambiguity.
- `--skip-spec-validation` *suppresses* the under-specification signal — the exact wrong response, as the #199 run showed.
- The richer canonical source (linked issue/Jira) is never consulted; the operator carries all enrichment manually, every time.

### Option 3: Un-gate the architect's CLARIFY/BRAINSTORM mid-plan

**Description:** Let the architect ask clarifying questions during planning by relaxing the autonomy clause for the CLARIFY/BRAINSTORM modes that already exist (`architect.md:445,477-481`).

**Pros:**
- Reuses existing prompt contracts; smallest apparent code change.
- Questions are asked by the agent with the most context (it has read the repo).

**Cons:**
- **Breaks the autonomy contract mid-run:** a prompt can fire after planning has started, defeating headless/cron/CI and the "autonomous after start" guarantee.
- **No resume story:** a question raised mid-plan has nowhere deterministic to live; resume would re-ask or stall (cf. ADR-0044's "deterministic-on-resume" requirement).
- **Couples to ADR-0044 backwards:** the architect asks *after* the altitude decision, so its questions tend to be solution-shaped ("should I refactor X?") — precisely the contamination Option 1 is built to avoid.
- Two interaction points (front-gate + mid-plan) with inconsistent semantics.

**Folded alternatives (prose):** **(4) Enrich-only, never ask** — gather + enrich fully autonomously and never involve the human. Rejected as the *sole* design: some constraints (provider lock, deadline, risk/altitude latitude, data sensitivity) are genuinely undeterminable from artifacts, so an enrich-only system guesses silently. But it is a *valid headless fallback* and is folded in as the `on_unanswered = assume_defaults` policy (defaults logged as assumptions). **(5) Make intake a separate manual command only** (`autodev intake` that an operator runs, reviews, then `autodev plan`) — rejected as the *default* (it just relocates the manual burden) but **retained as an opt-in surface** for operators who want to review the enriched spec before planning.

## Decision Drivers

- **Autonomy contract:** ask at most once, up front; after spec-lock the run is fully hands-off (no mid-run prompts). This is the property the whole product rests on.
- **Headless-safety:** cron / CI / `--yes` runs must never deadlock waiting for a human — a documented `on_unanswered` policy is mandatory.
- **Determinism & resume:** clarification happens *before* the spec_hash is fixed; on resume the locked spec is re-read, never re-asked (mirrors ADR-0044's deterministic-on-resume).
- **LLM Cost Efficiency:** well-formed specs add **zero** LLM calls; the gather/enrich/clarify path fires only when the completeness gate finds gaps.
- **Does-not-pre-empt-altitude** *(custom, the ADR-0044 coupling)*: intake elicits *constraints/preferences*, never *solution strategies* — the altitude decision stays with the framing phase.
- **Provenance / no-hallucination:** every enriched fact carries a source ref (file:line / issue URL / session id); the enricher is constrained to gathered evidence.
- **Testability:** `StubAdapter`-driven, deterministic intake evidence; the #199-style "thin summary → enriched spec" replay becomes a merge gate.
- **Pydantic-Boundary Strictness:** `IntakeEvidence` / `ClarifyingQuestion` validated `extra="forbid"`; malformed gather/enrich output fails safe to "ask the operator" or "block," never silently fabricates.

## Architecture Drivers Comparison

| Architecture Driver | Option 1: Intake & Clarification Phase | Option 2: Do Nothing | Option 3: Un-gate architect mid-plan | Notes |
|---|---|---|---|---|
| **Autonomy contract (ask once, then hands-off)** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐ | Opt-1 batches one front round then locks. Opt-3 can prompt mid-run. |
| **Headless-safety** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ | Opt-1 needs the `on_unanswered` policy; Opt-3 can deadlock a CI run. |
| **Determinism & resume** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ | Opt-1 locks spec_hash post-answers, re-reads on resume. Opt-3 has no resume home for a mid-plan question. |
| **LLM Cost** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | Opt-1 common case = +0 (gate is deterministic); gap path +1–2. |
| **Does-not-pre-empt-altitude** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ | Opt-1 asks constraints only; Opt-3's late questions skew solution-shaped. |
| **Solves under-specification** | ⭐⭐⭐⭐⭐ | ⭐ | ⭐⭐⭐ | The whole point. Opt-2 only detects it. |
| **New failure surface** | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ | Opt-1 adds gather/network + enrich-hallucination risk (mitigated). |

**Rating Scale:** ⭐⭐⭐⭐⭐ Excellent · ⭐⭐⭐⭐ Good · ⭐⭐⭐ Average · ⭐⭐ Below Average · ⭐ Poor

## Decision Outcome

**Chosen Option:** Option 1 — a dedicated **Intake & Clarification phase** at the front of `run_plan_phase`, **on by default** (`intake.enabled=True`), **no-op for well-formed specs**, with a **headless `on_unanswered` policy** and a **hard constraints-not-solutions boundary** with the framing phase.

**Rationale:** Only Option 1 scores full marks on *"Solves under-specification"* without sacrificing the *"Autonomy contract"* — Option 3 buys mid-plan context at the cost of the very property (hands-off after start, resume-safety) the product depends on, and it asks questions on the wrong side of the altitude decision. Option 1's costs are real but bounded by construction: the completeness gate is the same cheap deterministic scan that exists today (so well-formed specs pay nothing), clarification is a single front-loaded round that locks the spec before the autonomous run begins, and the `on_unanswered` policy makes headless runs first-class rather than deadlock-prone. The ADR-0044 coupling — the subtle, high-value risk — is handled the same way ADR-0044 handled minimality: **scope the dangerous thing to exactly one step.** ADR-0044 suspends minimality *only* at the altitude-choosing step; ADR-0045 confines clarification to *constraints only* and forbids the clarifier from enumerating or selecting solution strategies, leaving altitude wholly to framing.

**On-by-default is the deliberate stance** (consistent with ADR-0044): an expert always reads around the ticket and asks the few questions that matter before committing. Making intake opt-in would mean AutoDev plans against thin tickets by default and only gathers when an operator already knew to ask. The honest cost (a human touchpoint, a network gather, an enrichment step) is offset by: the zero-cost no-op on well-formed specs, the single-round/lock-then-autonomous contract, headless degradation, provenance-cited enrichment, a source allowlist against contamination, and the #199-style replay as a merge gate.

**Key Factors:**
- Turns the front-gate from a *detector* of under-specification into a *resolver* of it.
- Preserves the autonomy contract by confining interaction to one front-loaded, lockable round.
- Composes cleanly with ADR-0044 (constraints vs altitude), reusing the explorer + evidence/ledger machinery — small net-new brain.

## Consequences

### Positive Consequences
- Under-specified tickets are **enriched and clarified** instead of bounced; the canonical linked issue/Jira (often richer than the pasted summary) is pulled in automatically.
- The operator answers a few **constraint** questions once, up front, then the run is fully autonomous — the workflow users actually expect.
- `--skip-spec-validation` stops being the de-facto handling of thin specs; the signal is *acted on*, not suppressed.
- Framing/altitude (ADR-0044) receives a richer, constraint-bounded spec **without** having its altitude decision pre-empted.
- A provenance-cited, locked `.autodev/spec.md` improves the audit trail and gives the plan-critic something concrete to verify against.

### Negative Consequences / Trade-offs
- **New human-interaction + network surface.** External-source gather (`gh`, Jira-MCP) adds auth, latency, and failure modes; must degrade gracefully when a source is unavailable.
- **Enrichment-hallucination risk.** The enricher could assert facts not in evidence; mitigated by requiring source refs and a constrained/verified enrich step, but it is net-new risk.
- **Contamination risk in benchmarks.** Autonomous gather could read the solution PR; mitigated by `intake.exclude_globs` / source allowlist, but operators must set it for fair benchmarking.
- **CLI/operator-model change.** Intake reframes `--skip-spec-validation` and adds `--assume-defaults` / `--no-intake`; docs and the `/autodev` dispatch contract must change in lockstep.

### Neutral / Unknown Consequences (monitor)
- **Question quality / over-asking rate** — how often intake asks, and whether the questions are genuinely constraint-shaped (not solution-shaped). Tracked via a constraints-not-solutions audit on the question corpus.
- **Default-assumption accuracy** in headless mode — how often `assume_defaults` guesses wrong vs a human answer.
- **Interaction with the architect's now-redundant CLARIFY mode** (`architect.md:445`) — whether it should be retired now that clarification lives upstream.
- **spec_hash churn** — the locked enriched spec changes the hash vs the raw intent; confirm caching/resume keys on the *locked* spec consistently.

## Implementation Notes

**Files Affected (high level — see companion design doc for detail):**
- `src/orchestrator/intake_phase.py` *(new)* — `run_intake_phase(orch, intent) -> IntakeOutcome` (assess → gather → enrich → question → ask/default → lock). Invoked at the top of `run_plan_phase` (`plan_phase.py:667`), before the explorer envelope (`:682`) feeds framing.
- `src/orchestrator/spec_validator.py` — extend to surface **structured gaps** (which dimension is missing: scope / acceptance / constraints / concrete-touchpoints) consumed by intake, not just a binary `ok`. Keep `validate_spec`/`validate_spec_text` back-compatible.
- `src/orchestrator/plan_phase.py` — call `run_intake_phase` first; thread the **locked enriched spec** (not the raw intent) into the explorer/domain_expert/framing/architect envelope `context` (the `intent`/`spec` keys at `:705,754,780`). Persist via `write_evidence(cwd, "plan-intake", …)` mirroring the explorer/domain_expert pattern.
- `src/state/schemas.py` — add `IntakeEvidence(_BaseEvidence)` (`_BaseEvidence` at `:441`) to the `Evidence` discriminated union (`:675`); new `GatheredFact`, `ClarifyingQuestion`, `ClarifyingAnswer` models (`extra="forbid"`).
- `src/config/schema.py` — new `IntakePhaseConfig` mirroring `FramingPhaseConfig` (`:389-406`); top-level `intake` field on `AutodevConfig` via `default_factory` (mirror `framing` at `:1051-1054`).
- `src/config/defaults.py` — `_default_intake_cfg()` with `enabled=True`, `max_questions=4`, `sources=["repo","github","jira"]`, `on_unanswered="assume_defaults"`, `exclude_globs=[]`.
- `src/agents/prompts/intake_enricher.md`, `src/agents/prompts/intake_clarifier.md` *(new)* — the clarifier prompt carries the **constraints-not-solutions guard** (may capture an altitude-latitude *preference* as a constraint; must not enumerate/select solution strategies — that is framing's job).
- `src/cli/` — `autodev plan` runs intake automatically; add `--assume-defaults` / `--no-intake`; optional standalone `autodev intake` that emits the enriched spec for review. Define the machine-readable question contract (JSON block) that the `/autodev` dispatch layer renders to the operator (e.g. via the host's question UI).
- `src/agents/prompts/architect.md` — note that clarification now lives upstream; the autonomy clause (`:715,1368-1382`) stays; the CLARIFY mode (`:445`) is superseded for the autonomous path.
- `tests/` — #199-style "thin summary → enriched spec" replay (merge gate); constraints-not-solutions audit on generated questions; headless `on_unanswered` policy matrix; contamination-guard (exclude_globs) test.

**Ledger/State Implications:**
- New evidence kind: `intake` (file `plan-intake-intake.json` via `write_evidence`, `state/evidence.py:47`).
- New ledger ops (mirroring ADR-0044's `framing_classified` / `framing_strategy_chosen`): `intake_assessed`, `intake_gathered`, `intake_enriched`, `intake_questions_posed`, and exactly one of `intake_answered` / `intake_defaults_assumed`, then `spec_locked` (records the locked spec_hash).
- On resume, re-read `plan-intake` evidence and the locked spec — never re-gather or re-ask. Deterministic-on-resume, zero extra LLM/network calls.

**General Guidance:**
- The completeness gate must stay **cheap and deterministic** (extend the existing scan); do **not** spend an LLM call deciding whether to enrich.
- The clarifier prompt is the load-bearing artifact: a single audit — "does any question name or choose a solution approach?" — gates merges. A question may ask *"how much latitude on change size?"* (a constraint) but must never ask *"should I use an artifact store or trim strings?"* (a solution).
- Reuse the explorer's evidence for the repo-gather rather than a second exploration pass (cost + consistency).
- Treat external sources as untrusted input: bound sizes, validate, and degrade to "ask the operator" when a source is unreachable.

## Evidence from Codebase

**Source References (verified at HEAD):**
- `src/orchestrator/spec_validator.py:50-57,90-91` — `_ACCEPTANCE_MARKERS` and the `spec_no_acceptance_signal` reason: the gate detects missing acceptance signal but only rejects.
- `src/orchestrator/spec_validator.py:11-12` — docstring: operators "who genuinely want to dispatch on a laconic spec pass `--skip-spec-validation`" — the current (suppress-the-signal) escape hatch.
- `src/orchestrator/spec_validator.py:65-70,96-129` — `SpecValidationResult(ok, reasons)` + `validate_spec`/`validate_spec_text`: the gate to extend into a structured-gaps assessor.
- `src/orchestrator/plan_phase.py:667` — `async def run_plan_phase(orch, intent)`: the entry; intake runs first here.
- `src/orchestrator/plan_phase.py:682-696` — explorer envelope + `write_evidence(cwd, "plan-explore", …)`: pattern to mirror for `plan-intake`, and the repo-gather to reuse.
- `src/orchestrator/plan_phase.py:705,754,780` — `intent`/`spec` threaded verbatim into explorer/domain_expert/architect envelope `context`: the symptom-anchored intake that the locked enriched spec replaces.
- `src/orchestrator/plan_phase.py:744-760` — `run_framing_phase` invocation (ADR-0044): intake runs *before* this; the two compose (constraints → altitude).
- `src/state/schemas.py:441` — `class _BaseEvidence(BaseModel)` with `extra="forbid"` (base for `IntakeEvidence`).
- `src/state/schemas.py:675` — `Evidence` discriminated union (new `intake` variant added here).
- `src/state/evidence.py:47` — `write_evidence(cwd, task_id, evidence)` → `evidence/{task_id}-{kind}.json`.
- `src/config/schema.py:389-406` — `FramingPhaseConfig` + `_default_framing_cfg` (template for `IntakePhaseConfig`).
- `src/config/schema.py:1051-1054` — `framing: FramingPhaseConfig = Field(default_factory=_default_framing_cfg)` on `AutodevConfig` (pattern for the `intake` field).
- `src/agents/prompts/architect.md:445` — CLARIFY mode ("ask up to 3 questions") — exists but gated; superseded upstream by intake.
- `src/agents/prompts/architect.md:477-481` — BRAINSTORM Phase-3 "Present the approaches to the user" — the question/dialogue shape to relocate (constraints only) into the clarifier.
- `src/agents/prompts/architect.md:715,1368-1382` — shared AUTONOMY clause ("no operator … do not ask clarifying questions") — stays in force for the architect; intake is the single sanctioned interaction point.
- `docs/decisions/0044-framing-altitude-phase.md` — the immediately-upstream phase; this ADR reuses its evidence/ledger/config-mirroring patterns and respects its altitude ownership.

**Test Coverage (to be added):**
- `tests/orchestrator/test_intake_phase.py` — the #199 replay (thin `bug.md` → pulls issue #199 + repo facts → enriched spec passes G1); headless `on_unanswered` matrix (`assume_defaults`/`block`/`fail`); `exclude_globs` contamination guard.
- `tests/agents/test_intake_clarifier_constraints_only.py` — corpus audit: no generated question names/selects a solution approach (the ADR-0044 boundary).
- `tests/state/test_intake_schemas.py` — round-trip + `extra="forbid"` rejection for `IntakeEvidence` / `ClarifyingQuestion` / `ClarifyingAnswer`.

**Property-Based Tests (Hypothesis):**
- Optional: `validate_spec`-gaps monotonicity (adding an acceptance line never *introduces* a gap).

## Related Design Documents

- [intake_clarification_phase_design.md](../design_documentation/intake_clarification_phase_design.md) — the companion deep spec (phase FSM, schemas, gather adapters, prompts, headless policy, cost/test matrix).
- [framing_altitude_phase_design.md](../design_documentation/framing_altitude_phase_design.md) — the downstream neighbour; the constraints-vs-altitude boundary is defined against it.
- [orchestrator_design.md](../design_documentation/orchestrator_design.md) — host pipeline; intake inserts at the front of the plan phase.
- [architecture.md](../architecture.md) — subsystem map (orchestrator, agents, config, state, adapters).

## Monitoring and Review

- [ ] Review date: after **N = 20** real autonomous runs that pass through the intake phase (at least 10 that triggered the gather/clarify path).
- [ ] Success criteria: on the #199 replay, intake **pulls the richer linked issue**, **enriches** the spec so it passes G1 without `--skip`, **asks only constraint questions** (zero solution-shaped), and **locks** a spec that framing then classifies at the *same* altitude as the manual run — i.e. intake improved the input **without** changing the altitude decision.
- [ ] Metrics to track: enrich/clarify trigger rate, questions-per-run, constraints-not-solutions violation rate (target 0), headless default-assumption rate + downstream correction rate, added LLM/network calls per plan.

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-06-15 | Mohamed Ameen | Initial ADR created (Proposed). Motivated by the Synaptix Mistral-429 benchmark run, where the G1 gate rejected the thin `bug.md` and a manual gather→enrich→clarify (pulling the richer issue #199) was required before autonomous planning. |
| 2026-06-15 | Mohamed Ameen | Implemented end-to-end in v0.41.0 (`intake_phase.py`, `intake_sources/`, `spec_validator.assess`, `intake_enricher`/`intake_clarifier` prompts, CLI flags + `autodev intake`); on by default, headless `assume_defaults`, fail-safe degrade. Status Proposed → Accepted. |
