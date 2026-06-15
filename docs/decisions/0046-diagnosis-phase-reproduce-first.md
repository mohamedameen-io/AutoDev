# 0046 — Diagnosis Phase: a reproduce-first bug-fix workflow (feedback loop before plan)

* **Status:** Accepted
* **Date:** 2026-06-15
* **Deciders:** Mohamed Ameen
* **Tags:** orchestrator, planning, agents, qa-gates, state
* **Related:** [0045-intake-clarification-phase.md](0045-intake-clarification-phase.md) (immediate upstream — *what* to fix), [0044-framing-altitude-phase.md](0044-framing-altitude-phase.md) (immediate downstream — *how big* a fix; consumes diagnosis's seam/cause signal), companion design doc [`diagnosis_phase_design.md`](../design_documentation/diagnosis_phase_design.md), ADR-0008 (deterministic FSM), ADR-0002 (append-only CAS ledger)
* **Adapted from:** Matt Pocock's `diagnose` skill — <https://github.com/mattpocock/skills/blob/main/skills/engineering/diagnose/SKILL.md> (the 6-phase reproduce → minimise → hypothesise → instrument → fix → regression-test discipline).

## Context

AutoDev fixes bugs **without ever reproducing them.** The plan pipeline is
`intake → explorer → domain_expert → framing → architect → plan-tournament → execute`. Every step reasons
about the bug from the *report* and from *reading the code* — none of them runs the bug, builds a pass/fail
signal, or confirms that the fix addresses the symptom the user actually reported. The regression test, if
any, is written *during* the fix (by `developer`/`test_engineer`), not *before* it, so it tends to encode
the fix the model already chose rather than the bug as it actually manifests.

The cost of this is visible in the field. On the Synaptix Mistral-429 benchmark, **neither** the human PR
#200 nor AutoDev's prior PR #201 shipped a real-principal / live-Mistral / E2E reproduction test — both
were offline-only, a shared blind spot. AutoDev's diagnosis was *correct* by reasoning, but nothing
*proved* it: there was no fast, deterministic, agent-runnable signal that went red before the fix and green
after, tied to the specific 429-after-bloated-fetch symptom.

Matt Pocock's `diagnose` skill names the missing discipline precisely:

> "**This is the skill.** … If you have a fast, deterministic, agent-runnable pass/fail signal for the bug,
> you will find the cause. … If you don't have one, no amount of staring at code will save you."

Its 6 phases: **(1) build a feedback loop** (10 ranked construction methods), **(2) reproduce** (confirm
it's the *user's* failure mode), **(3) hypothesise** (3–5 ranked, falsifiable, shown to the user),
**(4) instrument** (one variable at a time; tagged `[DEBUG-xxxx]` logs; debugger > logs), **(5) fix +
regression test** (test *before* fix — *but only if a correct seam exists*; **"no correct seam" is itself
an architectural finding**), **(6) cleanup + post-mortem** ("what would have prevented this?" →
architectural handoff, *after* the fix).

Two of its rules map *directly* onto AutoDev's existing machinery:
- **Phase 5's "no correct seam = architectural finding"** is a `realized_design_failure` signal — exactly
  the kind of structural input ADR-0044's framing classifier consumes (alongside `recurrence_at_seam`).
- **Phase 6's "what would have prevented this?"** is the constructive, post-success twin of the existing
  `re_architect` recovery action (`repetition_recovery.py`).

**The tension to resolve:** AutoDev's autonomous agent runs in a sandbox with **no network beyond package
registries, no interactive TTY, no live credentials, no human eyes-on** (`architect.md:1108`). The skill's
loop-construction methods that need a running service, a live API, a real browser session, or a human
(curl/HITL) are unavailable on the autonomous path — and the skill says "do **not** proceed to hypothesise
without a loop." A naïve adoption would deadlock AutoDev on exactly the bugs (like Mistral-429) that only a
*live* environment truly reproduces. The design must reconcile "reproduce-first" with "headless sandbox."

**Affected subsystems:** orchestrator (`src/orchestrator/plan_phase.py`, new `diagnosis_phase.py`), agents
(`src/agents/prompts/`: `developer.md`, `test_engineer.md`, new `diagnostician.md`), QA gates (`src/qa/`),
config (`src/config/`), state (`src/state/schemas.py`).

The encouraging part (as with 0044/0045): the roles already exist (`developer`, `test_engineer` in
`REQUIRED_AGENT_ROLES`), the TDD skill already favours test-first, and the QA test gate already runs the
suite. This is mostly **a new gated phase + a reproduce/cleanup QA gate + prompt discipline + a sandbox
adaptation**, not a new brain.

## Options Considered

### Option 1: A gated Diagnosis Phase adopting the 6-phase discipline, adapted to the sandbox (chosen)

**Description:** Insert a **Diagnosis Phase** on the bug-fix path, after `explorer`/`domain_expert` and
**before** framing, that runs the skill's phases 1–4: build the strongest **sandbox-runnable** feedback
loop, reproduce, generate 3–5 ranked falsifiable hypotheses, and instrument to confirm the root cause.
It is a **gate**: planning-the-fix may not begin until there is either (a) a believed-in offline loop, or
(b) an explicit, recorded finding that only a *live* loop reproduces — in which case AutoDev builds the
strongest **synthetic/replay** loop it can for the autonomous pass/fail signal **and delivers the live-repro
as a first-class artifact** (script + documented procedure). Phase 5's regression-test-before-fix is wired
into `execute` (the failing loop becomes the task's acceptance signal, run red-before / green-after); a new
**reproduce-gate** and **debug-tag-cleanup-gate** join the QA gates. Phase 5's "no correct seam" and Phase
6's "what would have prevented this" feed the framing altitude decision and a post-success architectural
recommendation. **Conditional**: runs only when the task is a bug/regression (skipped for features), like
framing.

**Pros:**
- Fixes the actual gap: AutoDev stops fixing bugs it never reproduced; the fix is tied to the *symptom*.
- The Phase-1 loop becomes a **stronger acceptance signal** than "the suite passes" — red-before/green-after
  on the specific failure mode.
- Closes the shared blind spot: even when the sandbox can't run the live bug, AutoDev *delivers* the
  live-repro procedure + a synthetic regression loop (the "both" done-bar).
- Composes cleanly: the seam/cause findings feed ADR-0044 (altitude) and the post-mortem feeds
  `re_architect` — reuses existing machinery.
- Reproduce-first is TDD done right; AutoDev already has the test_engineer role + TDD skill to build on.

**Cons:**
- Adds a phase (LLM calls + wall-clock) on every bug-fix run; loop construction can itself fail.
- The sandbox genuinely cannot reproduce some bugs live — the synthetic/replay loop may be an imperfect
  proxy, risking a fix that passes the proxy but not reality (mitigated by the delivered live-repro + a
  honesty gate that labels the loop's fidelity).
- New QA gates add failure surface (a flaky reproduce-gate would block good fixes).

### Option 2: Do nothing — keep reasoning-only diagnosis

**Description:** Keep planning fixes from the report + code-reading; write tests during the fix.

**Pros:** Zero added cost/surface; determinism baseline unchanged.

**Cons:** The #200/#201 blind spot recurs on every bug; fixes are never empirically tied to the symptom;
"no test seam" is never surfaced as the architectural signal it is.

### Option 3: Encode the discipline in prompts only (no phase, no gate, no evidence)

**Description:** Add the diagnose discipline to `developer.md`/`test_engineer.md` and hope the roles follow it.

**Pros:** Smallest change; no orchestration work; no determinism shift.

**Cons:** No **gate** — a model that skips reproduction is not stopped; no `DiagnosisEvidence` for
resume/audit; the seam-finding never structurally reaches framing; "reproduce before plan" becomes advisory,
which is exactly the failure mode (models anchor on the first plausible fix). Prompts without a gate are how
the discipline gets skipped under time pressure.

**Folded alternatives (prose):** **(4) Require a true live loop, reject autonomous fixes without one** —
faithful to the skill's "do not proceed without a loop," but it would deadlock AutoDev on every
network/credential-bound bug in the sandbox. Folded in as the **artifact-delivery + synthetic-loop fallback**
(the skill's own "when you genuinely cannot build a loop" branch, adapted: build the best offline proxy,
deliver the live loop as a script+procedure, and label fidelity). **(5) Put diagnosis inside execute (per
task)** — rejected: the loop and root-cause must inform *planning* (and the altitude decision), so it must
run before the architect, not per-task after the plan is fixed. The regression *test* (Phase 5) does live in
execute; the diagnosis (Phases 1–4) does not.

## Decision Drivers

- **Empirical grounding (fix the right bug):** the fix must be tied to the user's actual symptom via a
  red-before/green-after signal, not to a plausible-looking code reading.
- **Sandbox compatibility:** the autonomous path has no network/TTY/live-creds (`architect.md:1108`); the
  phase must produce a usable signal *within* that, and degrade honestly when only a live loop reproduces.
- **Autonomy contract:** no mid-run human prompts; Phase-3 "show hypotheses to user" proceeds with the
  ranking when AFK (and surfaces via ADR-0045 intake only if interactive).
- **Determinism & resume:** the loop + hypotheses + confirmed cause persist as evidence; resume re-reads,
  never re-instruments.
- **Feeds-the-altitude-decision:** "no correct seam" / `recurrence_at_seam` must reach the framing classifier.
- **Testability:** `StubAdapter`-driven; the #199-style "build offline loop + deliver live-repro" is a gate.
- **LLM Cost Efficiency:** conditional on bug-fix tasks; feature work skips it entirely.

## Architecture Drivers Comparison

| Architecture Driver | Option 1: Gated Diagnosis Phase | Option 2: Do Nothing | Option 3: Prompts-only |
|---|---|---|---|
| **Empirical grounding** | ⭐⭐⭐⭐⭐ | ⭐ | ⭐⭐⭐ |
| **Sandbox compatibility** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
| **Autonomy contract** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| **Determinism & resume** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ |
| **Feeds the altitude decision** | ⭐⭐⭐⭐⭐ | ⭐ | ⭐⭐ |
| **Enforced (not skippable)** | ⭐⭐⭐⭐⭐ | ⭐ | ⭐⭐ |
| **LLM Cost** | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
| **New failure surface** | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ |

**Rating Scale:** ⭐⭐⭐⭐⭐ Excellent · ⭐⭐⭐⭐ Good · ⭐⭐⭐ Average · ⭐⭐ Below Average · ⭐ Poor

## Decision Outcome

**Chosen Option:** Option 1 — a **gated Diagnosis Phase** that adopts Matt Pocock's 6-phase discipline as
AutoDev's bug-fix workflow, **adapted to the headless sandbox**, **conditional on bug-fix tasks**, feeding
the framing altitude decision.

**Rationale:** Only Option 1 scores full marks on the drivers that exist *because* of the field evidence —
*empirical grounding* and *enforced/not-skippable*. Option 3 (prompts-only) loses precisely where it
matters: without a gate, the discipline is advisory and gets skipped under the same anchoring pressure the
skill warns about, and the seam-finding never structurally reaches framing. The decisive risk — that the
sandbox cannot reproduce network/credential-bound bugs — is handled the way the skill itself prescribes for
"cannot build a loop," made concrete for AutoDev: **build the best offline/synthetic loop for the autonomous
signal, deliver the live-repro as an artifact, and label the loop's fidelity honestly.** That converts the
deadlock into a deliverable (and is exactly the "both" done-bar that produced a clean spec on the Mistral-429
run). The phase is conditional (bugs only) so feature work pays nothing, and it sits *before* framing so the
altitude decision is grounded in a confirmed cause and a real seam-availability signal rather than a code
reading.

**Key Factors:**
- Turns "diagnosis by reasoning" into "diagnosis by a red→green signal" — the difference between a plausible
  fix and a proven one.
- The sandbox adaptation (synthetic loop + delivered live-repro + fidelity label) makes reproduce-first
  *possible* headlessly instead of a deadlock.
- Reuses existing seams: feeds ADR-0044 (altitude), reuses `test_engineer`/TDD, joins the QA gates.

## Consequences

### Positive Consequences
- Bug fixes are tied to the user's actual symptom via a reproducible red→green signal; "fixed a nearby bug"
  is caught at the reproduce step.
- The delivered live-repro artifact + synthetic regression loop closes the #200/#201 blind spot.
- "No correct test seam" becomes a first-class **architectural finding** that sharpens framing's altitude
  call, instead of silent false confidence from a shallow test.
- The post-mortem ("what would have prevented this?") produces a constructive architectural recommendation
  *after* the fix, when information is highest.

### Negative Consequences / Trade-offs
- Adds a phase (LLM + wall-clock) to every bug-fix run; loop construction can fail and must degrade cleanly.
- The synthetic/replay loop is a *proxy* for live-only bugs — a fix can pass the proxy yet miss reality;
  mitigated by the delivered live-repro and an explicit fidelity label, not eliminated.
- New QA gates (reproduce-gate, debug-tag cleanup) add surface; a flaky reproduce-gate could block good fixes.
- Determinism baseline shifts for bug-fix pipelines (new phase, new evidence, new gates).

### Neutral / Unknown Consequences (monitor)
- Loop-construction success rate in the sandbox (how often a believed-in offline loop is achievable).
- Synthetic-loop fidelity (how often the proxy disagrees with the delivered live-repro when a human runs it).
- Overlap with `test_engineer`'s existing remit — whether `diagnostician` should be a distinct role or a
  mode of `test_engineer`.

## Implementation Notes

**Files Affected (high level — see companion design doc for detail):**
- `src/orchestrator/diagnosis_phase.py` *(new)* — `run_diagnosis_phase(orch, spec, explore_ev) -> DiagnosisOutcome`
  (build-loop → reproduce → hypothesise → instrument → confirm-cause). Invoked in `run_plan_phase` after the
  explorer/domain_expert evidence and **before** `run_framing_phase` (`plan_phase.py:744`), gated on
  "is-bug-fix" (reuse the spec's scope markers / a classifier signal).
- `src/orchestrator/plan_phase.py` — call site; thread the confirmed root cause + `no_correct_seam` /
  `recurrence_at_seam` structural signals into the framing inputs; persist via
  `write_evidence(cwd, "plan-diagnosis", …)`.
- `src/state/schemas.py` — `DiagnosisEvidence(_BaseEvidence)` (kind `diagnosis`) into the `Evidence` union
  (`:675`): `{ loop: FeedbackLoop, reproduced: bool, symptom: str, hypotheses: list[Hypothesis],
  confirmed_cause: str | None, seam: Literal["correct","none","shallow"], loop_fidelity:
  Literal["live","synthetic","replay","none"], live_repro_artifact: str | None }`; `FeedbackLoop`,
  `Hypothesis` models (`extra="forbid"`).
- `src/agents/prompts/diagnostician.md` *(new)* — runs Phases 1–4; the sandbox loop-ordering (prefer
  failing-test / replay-trace / throwaway-harness / property-fuzz / differential / bisection over
  live-curl / browser / HITL); the "cannot build a live loop → build synthetic + deliver artifact" branch.
  Fold Phase-5 (regression-test-before-fix) into `developer.md` / `test_engineer.md`; fold Phase-6 cleanup
  into the wrap-up.
- `src/qa/` — new **reproduce-gate** (the persisted loop fails on the pre-fix tree and passes on the post-fix
  tree) and **debug-tag cleanup gate** (no `[DEBUG-...]` left; reuse the secret-scan-style scan).
- `src/config/schema.py` / `defaults.py` — `DiagnosisPhaseConfig` (mirror `FramingPhaseConfig`):
  `enabled`, `bug_only=True`, `max_hypotheses=5`, `loop_methods` (ordered allowlist), `require_loop_to_plan`
  (default True), `on_no_live_loop="synthetic_plus_artifact"`. Kill-switch `AUTODEV_DIAGNOSIS_DISABLED=1`.
- `tests/` — #199-style replay (build an offline loop replaying a captured oversized observation + stubbed
  429; deliver a live-repro script); reproduce-gate red→green; "no seam → finding" → framing signal;
  debug-tag cleanup gate.

**Ledger/State Implications:**
- New evidence kind `diagnosis` (`plan-diagnosis-diagnosis.json`).
- New ledger ops: `diagnosis_loop_built`, `bug_reproduced` (or `repro_unavailable_live`),
  `hypotheses_ranked`, `cause_confirmed`, `seam_finding` — mirroring 0044's `framing_classified`.
- On resume, re-read `plan-diagnosis` evidence; never re-instrument.

**General Guidance:**
- The loop is the product (per the skill): prefer a 2-second deterministic offline loop over a 30-second
  flaky one; pin time / seed RNG / isolate fs.
- **Honesty over green:** if only a live loop truly reproduces, label `loop_fidelity` accurately and deliver
  the live-repro artifact — never dress a synthetic proxy up as a live repro.
- Tagged `[DEBUG-...]` instrumentation must be removed by the cleanup gate before a task can leave `tested`.

## Evidence from Codebase

**Source References (verified at HEAD):**
- `src/config/schema.py:11-17` — `REQUIRED_AGENT_ROLES` includes `developer`, `test_engineer` (the roles
  the Phase-5 regression discipline folds into); no `diagnostician` exists yet.
- `src/orchestrator/plan_phase.py:744-760` — `run_framing_phase` invocation: the Diagnosis Phase inserts
  immediately *before* this so the confirmed cause + seam signal feed the altitude classifier.
- `src/orchestrator/plan_phase.py:682-696,713` — explorer/domain_expert evidence + `write_evidence` pattern
  the diagnosis phase reuses (`plan-diagnosis`).
- `src/agents/prompts/architect.md:1108` — autonomy capability boundary ("no network beyond package
  registries, no interactive TTY, no human eyes-on"): the constraint the sandbox loop-ordering adapts to.
- `src/agents/prompts/architect.md:1372` — "no operator … do not ask clarifying questions": why Phase-3's
  "show hypotheses to user" proceeds with the ranking on the autonomous path (surfaced only via ADR-0045
  intake if interactive).
- `src/agents/prompts/developer.md`, `src/agents/prompts/test_engineer.md` — existing roles to carry the
  regression-test-before-fix and instrumentation-cleanup discipline.
- `src/qa/` (`test_runner` via `env.py`, `detect.py`, `_io.py`) — the QA gate framework the reproduce-gate
  and debug-tag-cleanup gate join.
- `src/state/schemas.py:441,675` — `_BaseEvidence` (`extra="forbid"`) and the `Evidence` union for the new
  `diagnosis` variant.
- `docs/decisions/0044-framing-altitude-phase.md` — the `recurrence_at_seam` structural signal + altitude
  classifier that the "no correct seam" finding feeds.

**Test Coverage (to be added):**
- `tests/orchestrator/test_diagnosis_phase.py` — offline-loop build + reproduce + ranked hypotheses; the
  `no_correct_seam → framing` signal; resume re-read.
- `tests/qa/test_reproduce_gate.py` — red-before/green-after on a fixture; flaky-loop rejection.
- `tests/qa/test_debug_tag_cleanup_gate.py` — blocks a tree with a leftover `[DEBUG-...]` line.

**Property-Based Tests (Hypothesis):**
- Optional: loop-fidelity labelling never reports `live` when the run had no network.

## Related Design Documents

- [diagnosis_phase_design.md](../design_documentation/diagnosis_phase_design.md) — companion deep spec
  (phase FSM, sandbox loop-ordering, schemas, the two new QA gates, the synthetic-loop + artifact fallback).
- [intake_clarification_phase_design.md](../design_documentation/intake_clarification_phase_design.md) —
  upstream (ADR-0045): supplies the clarified spec + done-bar (offline tests + live-repro procedure).
- [framing_altitude_phase_design.md](../design_documentation/framing_altitude_phase_design.md) — downstream
  (ADR-0044): consumes the confirmed cause + seam signal for the altitude decision.
- [architecture.md](../architecture.md) — subsystem map.

## Monitoring and Review

- [ ] Review date: after **N = 20** real bug-fix runs through the diagnosis phase.
- [ ] Success criteria: on the #199 replay, the phase **builds an offline loop** that goes red pre-fix /
  green post-fix on the bloated-fetch-429 symptom, **delivers a live-repro artifact**, generates ≥3 ranked
  falsifiable hypotheses, and (when applicable) emits a `seam` finding that framing uses — i.e. the fix is
  tied to the symptom and the altitude call is grounded in a real seam analysis.
- [ ] Metrics to track: offline-loop construction success rate, synthetic-vs-live fidelity disagreement
  rate, reproduce-gate red→green pass rate, `no_correct_seam`→`design_fix` correlation, added cost/run.

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-06-15 | Mohamed Ameen | Initial ADR created (Proposed). Adapts Matt Pocock's `diagnose` skill into a gated, sandbox-aware Diagnosis Phase; motivated by the Synaptix Mistral-429 benchmark, where both PRs shipped without a live/E2E reproduction (the shared blind spot). |
| 2026-06-15 | Mohamed Ameen | Implemented end-to-end in v0.41.0 (`diagnosis_phase.py`, `diagnostician.md`, `qa/reproduce_gate.py` + `qa/debug_tag_gate.py`, developer/test_engineer regression-first folds, seam signal wired into framing); on by default, bug-gated, fail-safe, honest `loop_fidelity`. Status Proposed → Accepted. |
