# 0044 — Framing/Altitude Phase for Autonomous Patch-vs-Architecture Decisions

* **Status:** Accepted
* **Date:** 2026-06-14
* **Deciders:** Mohamed Ameen
* **Tags:** orchestrator, planning, tournament, agents
* **Related:** [0043-huge-repo-mode.md](0043-huge-repo-mode.md), companion design doc [`framing_altitude_phase_design.md`](../design_documentation/framing_altitude_phase_design.md), ADR-008 (deterministic FSM), ADR-003 (Borda tournament)

## Context

AutoDev is meant to be a fully-autonomous software expert, but it **systematically under-reaches**: it produces localized patches even when an architectural change is warranted, and — the deeper problem — it **never poses the patch-vs-architecture decision at all**. The plan phase walks explorer → domain_expert → architect → tournament, and minimality pressure is applied uniformly at every altitude. Three mechanisms, verified in `src/`:

1. **Symptom-anchored intake.** The bug report (symptom + the user's hypothesis) is passed verbatim as `spec`/`intent` into every planning agent — `run_plan_phase` threads `intent` into the explorer envelope (`plan_phase.py:687`), the domain_expert envelope (`:703`), and the architect envelope (`:755`). Nothing separates symptom from hypothesis or tests it. A "bloat" hypothesis yields a "trim" task by construction.
2. **One plan, refined — never competing strategies.** The architect emits exactly one plan markdown (`architect_result.text` at `plan_phase.py:968`); the tournament refines that single seed in place — "the tournament IS the gate" (`plan_phase.py:12`). When `num_branches > 1` (`:1125`), `run_multi_branch_plan_tournament` (`:1153`) fans out RNG-seeded re-runs of the *same* plan (each branch seeded `int(spec_hash, 16) + branch_index`, per `defaults.py:138-147`), producing within-altitude variance — not divergent strategies.
3. **Minimality applied at every altitude — including where it's wrong.** Minimality is correct when *implementing* a chosen approach but wrong when *choosing between* approaches. AutoDev applies it uniformly via five levers (the 1.3× length penalty at `tournament/prompts.py:117-121`; oversize demotion at `core.py:1142-1152`; the `minimality_judge` weight 0.5 at `defaults.py:186-190`; its prompt; and the `anti_bloat_v1` seed pack at `config/schema.py:924-925`). These suppress the architectural option at generation and plan-judging time.

**Field evidence.** On a Mistral-429 bug, AutoDev shipped a tool-observation *trim* patch (PR #201). A human, same bug, chose an architectural fix — control/data-plane separation with opaque handles (PR #200), whose ADR framed it as *"the realized failure of this design."* AutoDev's own dissenting plan-judge flagged the trim as a band-aid but had **no alternative strategy to vote for**.

**Affected subsystems:** orchestrator (`src/orchestrator/plan_phase.py`), tournament engine (`src/tournament/`), agents (`src/agents/prompts/`), config (`src/config/`), state (`src/state/schemas.py`, `src/state/evidence.py`).

The encouraging part: the expert machinery largely **already exists but is gated off** the autonomous path — BRAINSTORM's Phase-3 multi-approach contract (`architect.md:477-481`) and the `architect_b`/`re_architect` roles (`execute_phase.py:899`, `repetition_recovery.py:118`). So this is mostly *routing + an altitude-scoped value function*, not a new brain.

## Options Considered

### Option 1: Framing/Altitude Phase (chosen)

**Description:** Insert a framing/altitude phase between exploration and planning that (a) challenges the hypothesis and classifies the defect as *local-defect* vs *realized-design-failure* via a **hybrid** classifier (deterministic signals gate + seed a conservative LLM judge); (b) when warranted, autonomously generates 2–3 altitude-diverse strategies (one minimal patch, one design fix); (c) selects among them with **minimality pressure suspended**, on the rubric *"does this eliminate the failure class or merely bound it?"*; (d) hands the winner to the existing architect, where minimality resumes. **On by default** (`framing.enabled=True`), with a conservative classifier and fail-safe degradation as the offset.

**Pros:**
- Poses the patch-vs-architecture decision explicitly — the missing step, not a louder version of an existing one.
- The dissenting judge finally has a real alternative to vote for; design fixes survive to the architect.
- Reuses existing machinery (BRAINSTORM Phase-3 contract, Borda panel, evidence/ledger plumbing) — low new surface area.
- Common-case cost is **one** extra LLM call (local-defect path skips generation + panel).
- Minimality is suspended *only* at the choosing altitude; it resumes for implementation (the five levers revert downstream).

**Cons:**
- On by default changes plan outputs for **all** pipelines — the determinism baseline shifts and golden-plan tests must be regenerated.
- Adds ≥1 LLM call per plan unconditionally; the design-failure path adds 4.
- New failure surface: architect coupling (the architect's own minimality conditioning can silently shrink a design fix back to a patch unless explicitly told not to re-litigate).

### Option 2: Do Nothing

**Description:** Keep the current symptom-anchored, single-plan, uniformly-minimal pipeline.

**Pros:**
- Zero added cost, zero new failure surface, determinism baseline unchanged.
- Maximally conservative on the anti-bloat axis (over-engineering is impossible if architecture is never proposed).

**Cons:**
- The patch-vs-architecture decision is *never posed* — the #201 outcome recurs on every realized-design-failure bug.
- The dissenting judge's signal is permanently wasted (no option to vote for).
- AutoDev stays structurally incapable of the #200-class fix.

### Option 3: Always-Multi-Strategy (no classifier)

**Description:** Always generate 2–3 altitude-diverse strategies and always run the altitude panel — drop the classifier gate entirely.

**Pros:**
- Simplest control flow (no branch on classification); always poses the decision.
- No false-negative risk on the classifier (every bug gets the architectural option considered).

**Cons:**
- Pays the full 4-call design-failure cost on **every** plan, including trivial typo fixes — the over-engineering guardrail evaporates.
- High false-positive risk: invites architectural churn on bugs that are genuinely local, the exact over-reach failure inverted.
- Determinism baseline shifts for every run with a larger, noisier surface than the gated path.

**Folded alternatives (prose):** **(4) Un-gate BRAINSTORM with human-in-loop** — rejected: BRAINSTORM's Phase 3 (`architect.md:477-481`) ends with "Present the approaches to the user and recommend one… The user can pick" (`:480`); the autonomous orchestrator has no operator (`architect.md:1372` AUTONOMY clause), so the dialogue/transition phases must be stripped and the selection automated regardless — which *is* Option 1. **(5) Globally reweight minimality** — rejected: lowering the five levers everywhere would un-suppress bloat at *implementation* altitude (the correct place for minimality), trading one failure mode for its opposite.

## Decision Drivers

- **LLM Cost Efficiency:** Minimize added subscription/API calls per plan; the common case must stay cheap.
- **Determinism:** Same inputs produce the same execution path through the FSM; resume must not re-invoke the classifier.
- **Testability:** `StubAdapter` support for byte-identical `FramingEvidence`; the #201/#200 replay must be a deterministic merge gate.
- **Crash Safety:** Persist framing evidence before the architect envelope; SIGKILL mid-phase leaves no corrupted state.
- **Pydantic-Boundary Strictness:** `SolutionApproach` / `FramingEvidence` validated with `extra="forbid"`; malformed parse fails safe, never silently.
- **Avoids over-engineering** *(custom)*: the anti-bloat guardrail — the phase must not invite architectural churn on genuinely local bugs.
- **Poses the altitude decision** *(custom)*: the phase must actually surface, and be able to select, the architectural option when warranted.

## Architecture Drivers Comparison

| Architecture Driver | Option 1: Framing/Altitude Phase | Option 2: Do Nothing | Option 3: Always-Multi-Strategy | Notes |
|---|---|---|---|---|
| **LLM Cost** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ | Opt-1 common case = +1 call (local_defect skips panel); design path +4. Opt-3 pays +4 on every plan. |
| **Determinism** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | Opt-1 shifts baseline once but is deterministic-on-resume (re-reads `plan-framing` evidence). Opt-3 shifts a larger, noisier surface. |
| **Testability** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | Opt-1 adds the #201/#200 gate + conservatism corpus; StubAdapter-friendly. Opt-3 harder to pin (panel always fires). |
| **Crash Safety** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | All persist evidence before the architect envelope; atomic write via `state.evidence._atomic_write`. |
| **Avoids over-engineering** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐ | Opt-1's conservative classifier (prior=local, ≥0.7 + structural signal) protects the guardrail. Opt-3 removes it entirely. |
| **Poses the altitude decision** | ⭐⭐⭐⭐⭐ | ⭐ | ⭐⭐⭐⭐⭐ | The whole point. Opt-2 never poses it. |

**Rating Scale:** ⭐⭐⭐⭐⭐ Excellent · ⭐⭐⭐⭐ Good · ⭐⭐⭐ Average · ⭐⭐ Below Average · ⭐ Poor

## Decision Outcome

**Chosen Option:** Option 1 — the Framing/Altitude Phase, **on by default** (`framing.enabled=True`), with a **hybrid** classifier.

**Rationale:** Only Option 1 and Option 3 score full marks on *"Poses the altitude decision"* — the driver that exists *because* of the #201 failure. Between them, the deciding axis is *"Avoids over-engineering"*: Option 3 removes the anti-bloat guardrail (⭐) and pays the full design-failure cost on every plan (⭐⭐ cost), while Option 1 keeps the guardrail via a conservative classifier (⭐⭐⭐⭐) at near-baseline common-case cost (⭐⭐⭐⭐). The phase "poses the decision with an unbiased value function" — the `altitude_judge` rubric scores *eliminate-vs-bound the failure class*, with minimality suspended *only at this step* — while the conservative classifier (prior `local_defect`; flip to `realized_design_failure` only at `confidence ≥ 0.7` **and** ≥1 structural signal) protects against architectural churn.

**On-by-default is a deliberate autonomy stance**, not an oversight. AutoDev's mandate is to behave like an expert; an expert *always* asks "is this the right altitude?" Making it opt-in would mean AutoDev under-reaches by default and only reaches correctly when an operator already suspected the answer. The honest cost of this stance (baseline shift, +1 call, architect coupling) is offset by: the conservative classifier, fail-safe degrade to `local_defect` + single `local_patch`, determinism-on-resume, a kill-switch (`framing.enabled=false` and/or `AUTODEV_FRAMING_DISABLED=1`, following the `AUTODEV_INDEX_DISABLED` precedent at `state/file_index.py:423`), and the **#201/#200 benchmark + false-positive corpus as a merge gate**.

**Key Factors:**
- The decision the pipeline was structurally unable to pose is now posed — and selectable.
- The anti-bloat guardrail is preserved by construction (conservative classifier), not by avoiding the feature.
- Most of the machinery already exists; this is routing + a value function, keeping new failure surface small.

## Consequences

### Positive Consequences
- The patch-vs-architecture decision is finally **posed** on every plan, and the architectural option is **selectable** when warranted.
- The dissenting plan-judge's "band-aid" signal now has an alternative strategy to back — the #201 dead-end is removed.
- Design fixes survive to the architect at full altitude instead of being shrunk to patches at generation time.
- Minimality is restored to its correct altitude (implementation), not abandoned.

### Negative Consequences / Trade-offs
- **Determinism baseline shifts for all pipelines.** Because the phase is on by default and changes plan inputs, plan outputs change globally; golden-plan / determinism baselines **must be regenerated**.
- **Unconditional +1 LLM call per plan** (the classifier), and +4 on the design-failure path. Small relative to the existing plan tournament's tens of calls, but non-zero on every run.
- **New failure surface: architect coupling.** The architect's own minimality conditioning can silently re-litigate and shrink a chosen design fix back to a patch unless `architect.md` is told *"if `chosen_strategy` is present, implement THAT strategy at THAT altitude; do not re-litigate patch-vs-redesign."*

### Neutral / Unknown Consequences (monitor)
- **False-positive rate** of the classifier (local bugs misread as design failures) — tracked as the second merge gate.
- **Cost delta** in practice (how often the design-failure path fires on real workloads).
- Interaction with `architect_b` (wired-but-unused) and `re_architect` (execute-only) — this design supersedes/complements them; whether they should be retired is a later question.

## Implementation Notes

**Files Affected (high level — see companion for detail):**
- `src/orchestrator/framing_phase.py` *(new)* — `run_framing_phase(...) -> AltitudeDecision`, invoked from `run_plan_phase` right after the index query (`plan_phase.py:715-735`), before the architect envelope (`:737`).
- `src/orchestrator/plan_phase.py` — call site; thread `chosen_strategy` + `framing_classification` into `architect_env.context` (`:753-758`); persist via `write_evidence(cwd, "plan-framing", …)` mirroring `:694`/`:713`.
- `src/state/schemas.py` — add `FramingEvidence(_BaseEvidence)` (`_BaseEvidence` at `:441`) to the `Evidence` union (`:675`); new `SolutionApproach`.
- `src/config/schema.py` — new `FramingPhaseConfig` mirroring `TournamentPhaseConfig` (`:152`); top-level `framing` field on `AutodevConfig` (`:928`); add `framing`/`altitude_judge` to `denylist_roles` (`:885-893`).
- `src/config/defaults.py` — `default_factory` with `enabled=True` (within `default_config`, `:93-241`).
- `src/agents/prompts/framing.md`, `src/agents/prompts/altitude_judge.md` *(new)*; coupling note appended to `architect.md`.
- `tests/` — #201/#200 regression benchmark + conservatism corpus + lever-suspension/determinism tests.

**Ledger/State Implications:**
- New evidence kind: `framing` (file `plan-framing-framing.json` via `write_evidence`).
- Two new ledger ops appended via `orch.plan_manager.ledger_append` (`plan_manager.py:1497`): `framing_classified` and `framing_strategy_chosen`.
- On resume, re-read `plan-framing` evidence instead of re-invoking the classifier — deterministic-on-resume, zero extra LLM calls.

**General Guidance:**
- Do **not** reuse `multi_branch_tournament.py` RNG seeding for approach generation — it produces within-altitude variance, the exact failure being fixed.
- The five minimality levers must be suspended *scoped to the altitude step only* and revert for the downstream plan tournament. The easy-to-miss lever is the seed-pack denylist (`config/schema.py:885-893`): `framing`/`altitude_judge` must be added there or `anti_bloat_v1` lessons leak into the altitude cohort.

## Evidence from Codebase

**Source References (verified at HEAD):**
- `src/orchestrator/plan_phase.py:12` — `"6. … The tournament IS the gate."` (single-seed refinement).
- `src/orchestrator/plan_phase.py:687,703,755` — `intent` threaded verbatim into explorer/domain_expert/architect envelope `context` dicts (symptom-anchored intake).
- `src/orchestrator/plan_phase.py:715-735` — index query (`IndexQuery(...).get_candidates_for_spec`); insertion seam for `run_framing_phase` is the gap before the architect envelope at `:737`.
- `src/orchestrator/plan_phase.py:753-758` — `architect_env.context` dict (`spec`, `explorer_findings`, `domain_expert_findings`, `candidate_files`); the two new keys thread in here.
- `src/orchestrator/plan_phase.py:968` — `plan_md = architect_result.text` (architect emits exactly one plan).
- `src/orchestrator/plan_phase.py:694,713` — `write_evidence(cwd, "plan-explore"/"plan-domain_expert", …)` pattern to mirror.
- `src/orchestrator/plan_phase.py:1124-1153` — tournament block; `num_branches` (`:1125`), `run_multi_branch_plan_tournament` (`:1153`).
- `src/state/schemas.py:441` — `class _BaseEvidence(BaseModel)` with `model_config = ConfigDict(extra="forbid")`.
- `src/state/schemas.py:675-687` — `Evidence = Annotated[Union[...], Field(discriminator="kind")]` (new variant added here).
- `src/state/evidence.py:47` — `write_evidence(cwd, task_id, evidence)` → `evidence/{task_id}-{kind}.json`.
- `src/config/schema.py:152` — `class TournamentPhaseConfig(BaseModel)` (template for `FramingPhaseConfig`).
- `src/config/schema.py:885-893` — `denylist_roles` default `["explorer","judge","critic_t","architect_b","synthesizer"]` (add `framing`/`altitude_judge`).
- `src/config/schema.py:924-925` — `seed_packs_enabled = True`, `seed_packs = ["anti_bloat_v1"]`.
- `src/config/schema.py:928` — `class AutodevConfig(BaseModel)` (top-level `framing` field).
- `src/config/defaults.py:93-241` — `default_config(...)`; plan tournament defaults `:107-148` (`num_branches=3` at `:147`).
- `src/config/defaults.py:186-190` — impl `judge_role_weights` `{"judge":1.0,"judge_explorer":1.0,"minimality_judge":0.5}` (lever 3).
- `src/tournament/prompts.py:117-121` — `MANDATORY LENGTH PENALTY` ("more than 1.3× the length … MUST be ranked LAST") in `JUDGE_RANK_3_PROMPT` (`:104`) — lever 1.
- `src/tournament/core.py:1142-1152` — `_demote_oversized_winner(... token_threshold=getattr(self.cfg, "oversized_demotion_token_threshold", 0))` — lever 2.
- `src/tournament/core.py:1244-1248` — judge-roles resolution seam (`effective_judge_roles = list(judge_roles_cfg)` else `["judge"] * num_judges`).
- `src/tournament/core.py:1107-1118` + `src/tournament/voting.py` — Borda weight application path; `BordaAggregator` class defined in `tournament/voting.py` (imported, not defined in `core.py`).
- `src/agents/prompts/architect.md:477-481` — BRAINSTORM Phase-3 APPROACHES contract (name/summary/tradeoff/risk/integration-surface; `:480` "Present … to the user").
- `src/agents/prompts/architect.md:1368-1382` — shared AUTONOMY clause ("running unattended … no operator … do not ask clarifying questions").
- `src/state/file_index.py:423` — `AUTODEV_INDEX_DISABLED=1` env-override precedent for the `AUTODEV_FRAMING_DISABLED` kill-switch.

**Test Coverage (to be added):**
- `tests/orchestrator/test_framing_phase.py` — #201/#200 regression benchmark (merge gate) + conservatism corpus (false-positive gate).
- `tests/orchestrator/test_framing_levers.py` — altitude cohort never loads `minimality_judge.md`; `anti_bloat_v1` not injected into `framing`/`altitude_judge`.
- `tests/state/test_framing_schemas.py` — round-trip + `extra="forbid"` rejection for `SolutionApproach` / `FramingEvidence`.

**Property-Based Tests (Hypothesis):**
- N/A for v1 (deterministic corpus-based gates are the primary signal; Hypothesis round-trip on the schemas is optional).

## Related Design Documents

- [framing_altitude_phase_design.md](../design_documentation/framing_altitude_phase_design.md) — the companion deep spec (schemas, classifier, prompts, lever-suspension table, cost, test matrix).
- [orchestrator_design.md](../design_documentation/orchestrator_design.md) — host pipeline; framing phase inserts between exploration and planning.
- [tournaments.md](../design_documentation/tournaments.md) — Borda panel + judge-roles mechanics reused by the altitude panel.
- [architecture.md](../architecture.md) — subsystem map (orchestrator, tournament, agents, config, state).

## Monitoring and Review

- [ ] Review date: after **N = 20** real autonomous runs that pass through the framing phase.
- [ ] Success criteria: on the #201/#200 replay, the phase **surfaces** the architectural option (`classification == realized_design_failure`, `confidence ≥ 0.7`, `approaches` contains both a `local_patch` and a `design_fix`) **and** the `altitude_judge` panel **selects** the `design_fix`.
- [ ] Metrics to track: design-failure classification rate, false-positive rate (local bugs misclassified), cost delta (extra LLM calls / plan).

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-06-14 | Mohamed Ameen | Initial ADR created (Proposed). |
| 2026-06-14 | Mohamed Ameen | Implemented end-to-end (Phases 0–6) on `feat/framing-altitude-phase`; on by default with the conservative classifier + fail-safe degrade. Status Proposed → Accepted. |
