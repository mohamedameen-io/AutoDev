# 0047 — Universal Blocker Resolver

* **Status:** Accepted
* **Date:** 2026-06-15
* **Deciders:** Mohamed Ameen
* **Tags:** orchestrator, execute, recovery, state, config, loop-safety
* **Related:** [0046-diagnosis-phase-reproduce-first.md](0046-diagnosis-phase-reproduce-first.md) (upstream — *what* / *how* to fix; the resolver recovers when execution of that plan dead-ends), [0045-intake-clarification-phase.md](0045-intake-clarification-phase.md) (the resolver's `ask_human` action reuses intake's single-batched clarify wire-contract), [0044-framing-altitude-phase.md](0044-framing-altitude-phase.md) (the resolver's `re_architect` / `re_plan` actions re-enter the framing/architect altitude machinery), companion design doc [`blocker_resolver_design.md`](../design_documentation/blocker_resolver_design.md), ADR-0008 (deterministic FSM), ADR-0002 (append-only CAS ledger)

## Context

AutoDev's execute phase **dead-ends** at terminal failures. The recovery audit of
`src/orchestrator/execute_phase.py` found **13 terminal sites** that, when a downstream agent or guardrail
hits a wall, either emit `blocked: user_decision_required` and stop, raise to the CLI driver, or silently
degrade — each with its own ad-hoc `blocked_reason` string (`"dag_invalid"`, `"guardrail_exceeded"`,
`"conflict_escalation:3way_failed"`, `"conflict_escalation:abandon"`, `"test_diagnosis_..."`, the
soft-blocker handoff rung, the infra circuit-breaker, the worktree-apply failure, the worker-exception
catch). The deterministic recovery ladder (retry → pivot → discard → soft-blocker) handles the *common*
case well, but at its **terminal rungs** there is no orchestrator-level actor that looks at the blocker as a
whole and asks "given everything tried, what is the one bounded move that re-enables this workflow?" The
run just stops.

The field evidence is direct. Across Runs 1–4 on the Synaptix benchmark the *dominant* blockers were never
"the model can't fix the bug" — they were **delivery-layer dead-ends**: a triple `LLMRateLimitError` that
opened the infra circuit and ended the run; a reviewer-MALFORMED loop; a `depends_on=[]` parallel-worktree
incoherence; a cross-phase dependency that failed validation. Each is *recoverable* (escalate the budget,
reroute the model, re-plan the DAG, split the task) but no single component owned the recovery decision, so
the orchestrator surfaced `user_decision_required` and halted on bugs a competent engineer would have
worked around without asking.

There is a second, subtler gap: **novel** failures. The recovery ladder only recognises the classes it was
coded for; a failure shape it has never seen falls straight through to a block. The system has no path that
*reasons* about an unrecognised terminal error instead of dead-ending on it.

The recovery *primitives* already exist and are battle-tested — `repetition_recovery.py` (`re_architect`,
pivot), budget escalation, model escalation (sonnet → opus), the A1 reviewer **soft-pass-with-evidence**
pattern, the corrective-task / split machinery, knowledge consult, and the ADR-0045 intake clarify channel.
What is missing is the **router**: one orchestrator-level function that, at any terminal site, assembles the
blocker context, chooses a bounded action from a fixed vocabulary, maps it onto an *already-existing*
primitive, and records the decision for audit and loop-safety. The brain isn't new; the **dispatcher** is.

**The tension to resolve:** a catch-all recovery actor is exactly the thing that can loop forever — resolve
→ retry → same blocker → resolve → … — or that, fired on every failure, multiplies LLM cost and erodes the
determinism baseline. The design must make recovery *universal* without making it *unbounded* or *expensive
on the common path*.

**Affected subsystems:** orchestrator (`src/orchestrator/blocker_resolver.py` *(new)*, `failure_classes.py`,
the 13 terminal sites in `execute_phase.py`), state (`src/state/schemas.py`: `BlockerContext`,
`ResolutionAction`; `src/state/ledger.py`: three audit ops), config (`src/config/schema.py`:
`ResolverConfig`).

The encouraging part (as with 0044/0045/0046): every recovery primitive the action vocabulary maps onto
already exists. This is mostly **a new router + a named failure taxonomy + three audit ledger ops + a
config**, gated by a per-blocker budget and a circuit-breaker — not a new recovery engine.

## Options Considered

### Option 1: A single orchestrator-level Universal Blocker Resolver, two-tier, loop-bounded (chosen)

**Description:** Add `resolve_blocker(ctx: BlockerContext, orch) -> ResolutionAction` plus a thin
`_maybe_resolve_blocker` shim wired into each of the **13 terminal sites** in `execute_phase.py`. The
resolver is **two-tier**: a **deterministic fast-path** maps a *known* `failure_class` (from the canonical
`failure_classes.py` taxonomy) to a bounded action with **zero LLM cost**; an **LLM resolver** handles the
catch-all `unknown` class (and, when `fast_path_only_on_known=False`, any routed blocker). The chosen action
is one of a **fixed vocabulary** (`retry_with_changes`, `split_task`, `narrow_scope`, `re_architect`,
`re_plan`, `reroute`, `repair_environment`, `relax_constraint`, `escalate_budget`, `escalate_model`,
`soft_pass_with_evidence`, `consult_knowledge`, `web_search`, `ask_human`, `fall_through`) — each mapping
onto an **existing recovery primitive**, never a new code path. Three **audit-only** ledger ops
(`blocker_escalated`, `resolution_chosen`, `resolution_outcome`) make the decision replayable and drive a
**per-blocker cycle budget** (B5 loop-safety) reconstructed on resume. Loop-safety has three layers: the
per-blocker budget (`max_cycles_per_blocker`, default 3), a global circuit-breaker, and a
resolver-self-failure rule (the resolver erroring falls through to a bounded `ask_human`). `fall_through`
returns the call site to its **prior** block/degrade behaviour, so the resolver is strictly additive.
Kill-switch `AUTODEV_RESOLVER_DISABLED=1` forces every site back to legacy behaviour (fail-safe).

**Pros:**
- Fixes the actual gap: terminal sites that *halted* now have one actor that chooses a bounded recovery —
  the delivery-layer dead-ends from Runs 1–4 become recoverable.
- Handles **novel** failures: the `unknown` class routes to the LLM resolver instead of dead-ending.
- Near-zero common-case cost: `fast_path_only_on_known=True` means the resolver only engages at terminal
  rungs / on unrecognised classes; known recoverable cases keep using the cheap deterministic ladder.
- Strictly additive + fail-safe: `fall_through` and the kill-switch preserve every prior behaviour exactly,
  so the determinism baseline is unchanged unless the resolver chooses to act.
- Reuses existing primitives only — every action maps to `repetition_recovery`, budget/model escalation, the
  A1 soft-pass, intake clarify, etc. No new recovery engine.

**Cons:**
- A catch-all recovery actor is the canonical place to introduce loops or cost blow-ups (mitigated, not
  eliminated, by the per-blocker budget + circuit-breaker + resolver-self-failure rule).
- 13 call-site edits in `execute_phase.py` (the sole-owner integration phase) widen the blast radius of a
  bad shim.
- The LLM resolver on the `unknown` path adds calls + a new non-determinism source on exactly the failures
  that are hardest to test.

### Option 2: Do nothing — keep per-site ad-hoc terminal handling

**Description:** Leave each terminal site's bespoke `blocked_reason` / raise / degrade as-is.

**Pros:** Zero added surface; determinism baseline unchanged; no loop risk.

**Cons:** The delivery-layer dead-ends recur on every run; recoverable blockers keep surfacing
`user_decision_required`; novel failures always dead-end; the recovery primitives stay un-orchestrated.

### Option 3: Add bespoke recovery to each terminal site individually

**Description:** Hand-write tailored recovery at each of the 13 sites (site-specific retry/escalate/re-plan).

**Pros:** Each recovery is maximally context-aware; no shared catch-all to loop.

**Cons:** No single failure vocabulary; 13 divergent recovery policies to maintain and keep loop-safe
*independently*; novel failures still dead-end (each site only knows its own class); no unified audit trail
for resolution decisions; the per-site budget/circuit-breaker logic gets duplicated 13×.

**Folded alternatives (prose):** **(4) Pure-LLM resolver on every blocker (no deterministic tier)** —
rejected as too costly and non-deterministic for the *common*, already-recoverable case; folded in as
`fast_path_only_on_known=False`, an opt-in evaluation mode. **(5) A separate "recovery phase" outside
execute** — rejected: blockers are intrinsically task-local and mid-execution; recovery must happen *at the
site*, with the live task/phase/worktree state, not in a downstream phase after the plan is frozen.

## Decision Drivers

- **Forward progress (don't dead-end):** a recoverable terminal blocker must yield a bounded recovery move,
  not `user_decision_required`.
- **Loop-safety (bounded recovery):** universal recovery must be impossible to run forever — per-blocker
  budget + circuit-breaker + resolver-self-failure → `ask_human`.
- **LLM Cost Efficiency:** the common, already-recoverable case must stay ≈ 0 added cost
  (`fast_path_only_on_known`); LLM reasoning only on terminal / `unknown` blockers.
- **Determinism & resume:** the chosen action + per-blocker budget persist as audit ops; resume re-reads
  them and never re-invokes the resolver for an already-resolved blocker.
- **Fail-safe / additive:** `fall_through` + `AUTODEV_RESOLVER_DISABLED` guarantee no regression vs prior
  per-site behaviour.
- **Reuse over reinvention:** every action maps to an existing recovery primitive.
- **Testability:** `StubAdapter`-driven; the deterministic fast-path is pure and unit-testable per class.

## Architecture Drivers Comparison

| Architecture Driver | Option 1: Universal Resolver | Option 2: Do Nothing | Option 3: Per-site Bespoke |
|---|---|---|---|
| **Forward progress (no dead-end)** | ⭐⭐⭐⭐⭐ | ⭐ | ⭐⭐⭐ |
| **Handles novel failures** | ⭐⭐⭐⭐⭐ | ⭐ | ⭐ |
| **Loop-safety (bounded)** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ |
| **LLM Cost** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ |
| **Determinism & resume** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ |
| **Fail-safe / additive** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ |
| **Reuse (no new engine)** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ |
| **Maintainability (one policy)** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐ |

**Rating Scale:** ⭐⭐⭐⭐⭐ Excellent · ⭐⭐⭐⭐ Good · ⭐⭐⭐ Average · ⭐⭐ Below Average · ⭐ Poor

## Decision Outcome

**Chosen Option:** Option 1 — a single, two-tier, loop-bounded **Universal Blocker Resolver** wired into the
13 terminal sites in `execute_phase.py`, on by default, fast-path-only, fail-safe.

**Rationale:** Only Option 1 scores full marks on the drivers that exist *because* of the field evidence —
*forward progress* and *handles novel failures*. Option 2 leaves every delivery-layer dead-end in place.
Option 3 buys context-awareness at the cost of 13 divergent, independently-loop-unsafe recovery policies and
*still* dead-ends on novel failures, because each site only knows its own class. The decisive risk — that a
catch-all recovery actor loops or blows up cost — is handled structurally rather than by hope: the
**two-tier** design keeps the common case on the cheap deterministic ladder
(`fast_path_only_on_known=True`), so LLM reasoning fires only at terminal rungs or on the `unknown` class;
the **per-blocker cycle budget** (reconstructed from the audit ledger on resume) plus the **circuit-breaker**
plus the **resolver-self-failure → `ask_human`** rule make unbounded recursion impossible; and
`fall_through` + the kill-switch make the whole feature strictly additive, so the determinism baseline is
untouched until the resolver actually chooses to act. Every action maps to an existing primitive, so the
resolver is a **router**, not a new brain.

**Key Factors:**
- Turns "dead-end at a terminal blocker" into "choose one bounded recovery move" — the difference between a
  halted run and a recovered one.
- Two-tier (deterministic fast-path + LLM catch-all) keeps the common case ≈ 0 cost while still reasoning
  about novel failures.
- Loop-safety is structural (budget + breaker + self-failure rule), and the feature is fail-safe by
  construction (`fall_through`, kill-switch).

## Consequences

### Positive Consequences
- Recoverable terminal blockers (rate-limit storms, reviewer-MALFORMED loops, DAG incoherence, conflict
  escalations) yield a bounded recovery instead of `user_decision_required` — the Run 1–4 dead-ends close.
- Novel/unseen failures route to the LLM resolver instead of dead-ending — the system degrades gracefully on
  the unknown.
- One canonical failure vocabulary (`failure_classes.py`) and one audit trail (`blocker_escalated` /
  `resolution_chosen` / `resolution_outcome`) replace 13 ad-hoc `blocked_reason` strings — observability and
  post-hoc analysis improve sharply.
- `ask_human` is now a *precise, last-resort* action (reusing intake's clarify channel) rather than the
  default outcome of any wall.

### Negative Consequences / Trade-offs
- A catch-all recovery actor is the natural home for loops and cost blow-ups; the per-blocker budget +
  circuit-breaker + self-failure rule bound it but cannot make it free.
- 13 call-site edits in `execute_phase.py` widen the blast radius; a buggy shim could mis-route a blocker
  (mitigated by `fall_through` defaulting to prior behaviour and the kill-switch).
- The LLM resolver adds calls and a non-determinism source on the `unknown` path — exactly the failures that
  are hardest to test deterministically.

### Neutral / Unknown Consequences (monitor)
- Fast-path vs LLM-path hit ratio in the field (how often `unknown` actually fires).
- Per-blocker budget exhaustion rate (how often recovery genuinely loops before `ask_human`).
- Whether the fixed action vocabulary needs new entries as novel failures accumulate (`params` is a free
  dict precisely so the vocabulary can grow without a schema migration).
- Overlap between `re_architect`/`re_plan` and the existing `repetition_recovery` pivot — whether the
  resolver should ever *replace* rather than *invoke* the ladder.

## Implementation Notes

**Files Affected:**
- `src/orchestrator/blocker_resolver.py` *(new)* — `resolve_blocker(ctx: BlockerContext, orch) ->
  ResolutionAction` (deterministic fast-path for known classes; LLM resolver for `unknown`) and the
  `_maybe_resolve_blocker` shim wired into the 13 terminal sites. Honours `cfg.resolver.enabled`,
  `fast_path_only_on_known`, `max_cycles_per_blocker`; emits the three audit ops; falls through on
  self-failure.
- `src/orchestrator/failure_classes.py` — the canonical taxonomy: one named class per terminal site
  (`dag_invalid`, `cross_phase_dag_invalid`, `edit_scope_violation`, `conflict_3way_failed`,
  `conflict_abandon`, `conflict_rewrite_cap_exceeded`, `guardrail_exceeded`, `test_diagnosis_hardfail`,
  `test_diagnosis_no_signal`, `worker_exception`, `infra_circuit_open`, `soft_blocker`,
  `worktree_apply_failed`), the phase-degrade class, the catch-all `unknown`, `STRUCTURAL_FAILURE_CLASSES`,
  and `classify` / `is_known`.
- `src/orchestrator/execute_phase.py` — the **13 terminal sites** call `_maybe_resolve_blocker` with the
  site's `BlockerContext` (failure class, raw error, failing role/task/phase, `attempt_history`,
  `recovery_already_tried`, `evidence_refs`, the site's `available_actions`) and apply the returned action,
  defaulting to the prior block/degrade on `fall_through`.
- `src/state/schemas.py` — `BlockerContext` (resolver input) and `ResolutionAction`
  (`action: ResolutionActionType`, `params: dict`, `rationale`), both `ConfigDict(extra="forbid")`; the
  `ResolutionActionType` 15-action `Literal`.
- `src/state/ledger.py` — the three **audit-only** ops `blocker_escalated`, `resolution_chosen`,
  `resolution_outcome` (they never mutate plan state; the chosen action applies via the regular
  `update_task_status` / `append_corrective_tasks` / `budget_escalation` ops). Resume re-reads them to
  reconstruct the per-blocker budget without re-invoking the resolver.
- `src/config/schema.py` / `defaults.py` — `ResolverConfig` (`enabled`, `max_cycles_per_blocker=3`,
  `fast_path_only_on_known=True`, `model`); `AutodevConfig.resolver` via `_default_resolver_cfg`
  (default-on). Kill-switch `AUTODEV_RESOLVER_DISABLED=1`.
- `tests/` — fast-path per-class mapping; `unknown` → LLM resolver via `StubAdapter`; per-blocker budget
  exhaustion → `ask_human`; resume re-reads audit ops (no re-invocation); kill-switch / `fall_through`
  preserves prior behaviour.

**Ledger/State Implications:**
- Three new **audit-only** ops: `blocker_escalated` `{task_id, phase_id, failure_class, failing_role,
  raw_error_excerpt, recovery_already_tried}`, `resolution_chosen` `{task_id, failure_class, action,
  rationale_excerpt, params}`, `resolution_outcome` `{task_id, action, outcome: "applied"|"fell_through"|
  "ask_human", reason}`.
- They are append-only and **never** mutate plan state — the resolver's effect lands through the existing
  state-mutating ops. On resume they reconstruct the per-blocker cycle budget (loop-safety + determinism)
  without re-running the resolver.

**General Guidance:**
- Keep the fast-path **pure** (no LLM, no I/O) so it is unit-testable per failure class and ≈ 0 cost.
- `STRUCTURAL_FAILURE_CLASSES` (`dag_invalid`, `cross_phase_dag_invalid`, `edit_scope_violation`,
  `infra_circuit_open`) are routed for observability but default to re-plan / legacy-block — do **not** do
  task-local recovery on them.
- The per-blocker key (task + failure class) must be stable across resume so the budget actually bounds the
  loop; never reset it on a retry that hits the same wall.
- `fall_through` must reproduce the call site's *exact* prior behaviour — the resolver is additive only.

## Evidence from Codebase

**Source References (verified at HEAD):**
- `src/orchestrator/failure_classes.py:23-103` — the canonical taxonomy: 13 terminal-site classes
  (`DAG_INVALID` … `WORKTREE_APPLY_FAILED`), `PHASE_DEGRADED`, the catch-all `UNKNOWN`, and
  `STRUCTURAL_FAILURE_CLASSES`; `classify` (`:106`) normalises any arbitrary string to `unknown` — the
  novel-failure path the resolver exists to handle.
- `src/state/schemas.py:945-961` — `ResolutionActionType`, the fixed 15-action vocabulary (each comment maps
  the action to its existing primitive: `escalate_model -> sonnet -> opus`, `soft_pass_with_evidence -> A1
  reviewer soft-pass`, `ask_human -> intake/clarify`, `fall_through -> legacy block/degrade`).
- `src/state/schemas.py:964-1011` — `BlockerContext` (resolver input: `failure_class`, `raw_error`,
  `failing_role`, `attempt_history`, `recovery_already_tried` for B5 loop-safety, `evidence_refs`,
  `available_actions`) and `ResolutionAction` (output: `action`, `params`, `rationale`), both
  `extra="forbid"`.
- `src/config/schema.py:523-570,1247-1248` — `ResolverConfig` (`enabled`, `max_cycles_per_blocker`,
  `fast_path_only_on_known`, `model`), the two-tier rationale + `AUTODEV_RESOLVER_DISABLED` kill-switch in
  the docstring, `_default_resolver_cfg` (default-on), and `AutodevConfig.resolver`.
- `src/config/defaults.py:311-318` — the on-by-default, fast-path-only resolver in `default_config()`.
- `src/state/ledger.py:600-616,961` — the three audit-only ops with documented payload shapes; the
  resume-time reconstruction of the per-blocker budget without re-invoking the resolver.
- `src/orchestrator/execute_phase.py` — the 13 terminal sites the shim wires into: `guardrail_exceeded`
  (`:604`), the `user_decision_required` halts (`:546,1405,1556`), the conflict-escalation rungs
  (`:1798,1808,1824,1854`), `dag_invalid` (`:2154`), the worker-exception / typed-halt paths
  (`:1908-1917,2219-2226,2305`).

**Test Coverage (to be added):**
- `tests/orchestrator/test_blocker_resolver.py` — deterministic fast-path per known class; `unknown` →
  StubAdapter LLM resolver; `fall_through` reproduces prior behaviour; kill-switch off-path.
- `tests/orchestrator/test_blocker_resolver_loop_safety.py` — per-blocker budget exhaustion → `ask_human`;
  circuit-breaker; resolver-self-failure → `ask_human`.
- `tests/orchestrator/test_blocker_resolver_resume.py` — resume re-reads the three audit ops and never
  re-invokes the resolver for an already-resolved blocker.

**Property-Based Tests (Hypothesis):**
- Optional: `resolve_blocker` is idempotent under replay — given the same `BlockerContext` and the same
  recorded budget, it never recommends more cycles than `max_cycles_per_blocker`.

## Related Design Documents

- [blocker_resolver_design.md](../design_documentation/blocker_resolver_design.md) — companion deep spec
  (the terminal-site → `_maybe_resolve_blocker` → `resolve_blocker` flow, the action vocabulary ↔ primitive
  mapping, schemas, the three audit ops, and the three-layer loop-safety).
- [diagnosis_phase_design.md](../design_documentation/diagnosis_phase_design.md) — upstream (ADR-0046): the
  resolver recovers when execution of the diagnosis-grounded plan dead-ends.
- [intake_clarification_phase_design.md](../design_documentation/intake_clarification_phase_design.md) — the
  `ask_human` action reuses intake's single-batched clarify wire-contract (ADR-0045).
- [framing_altitude_phase_design.md](../design_documentation/framing_altitude_phase_design.md) — the
  `re_architect` / `re_plan` actions re-enter the framing/architect altitude machinery (ADR-0044).
- [architecture.md](../architecture.md) — subsystem map.

## Monitoring and Review

- [ ] Review date: after **N = 20** real runs that hit at least one terminal blocker.
- [ ] Success criteria: on the Run-1–4 dead-end fixtures (rate-limit storm, reviewer-MALFORMED loop,
  `depends_on=[]` DAG incoherence, conflict escalation), the resolver chooses a bounded recovery action that
  re-enables the workflow instead of surfacing `user_decision_required`; an `unknown`-class failure routes to
  the LLM resolver; the per-blocker budget caps recovery at `max_cycles_per_blocker` before falling through
  to `ask_human`; resume reconstructs the budget without re-invoking the resolver.
- [ ] Metrics to track: fast-path vs LLM-path hit ratio, per-blocker budget exhaustion rate, recovered-vs-
  dead-ended terminal-blocker rate, added cost/run, `fall_through` frequency (additivity check).

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-06-15 | Mohamed Ameen | Initial ADR created (Proposed). A single orchestrator-level Universal Blocker Resolver wired into the 13 terminal sites in `execute_phase.py`; two-tier (deterministic fast-path + LLM catch-all over the `failure_classes.py` taxonomy), loop-bounded (per-blocker budget + circuit-breaker + resolver-self-failure → `ask_human`), fail-safe (`fall_through` + `AUTODEV_RESOLVER_DISABLED`). Motivated by the Run 1–4 delivery-layer dead-ends that surfaced `user_decision_required` on recoverable blockers. |
| 2026-06-15 | Mohamed Ameen | Implemented in v0.42.0 (`blocker_resolver.py`, `failure_classes.py` taxonomy, `BlockerContext`/`ResolutionAction` schemas, `ResolverConfig`, the three audit ledger ops, the 13 terminal-site shims); on by default, fast-path-only, fail-safe. Status Proposed → Accepted. |
