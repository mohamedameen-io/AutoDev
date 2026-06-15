# Diagnosis Phase Design

> Companion deep spec for [ADR-0046](../decisions/0046-diagnosis-phase-reproduce-first.md). Adapts Matt
> Pocock's [`diagnose` skill](https://github.com/mattpocock/skills/blob/main/skills/engineering/diagnose/SKILL.md)
> into a gated, sandbox-aware phase. Models its structure on
> [`framing_altitude_phase_design.md`](framing_altitude_phase_design.md) (ADR-0044) and
> [`intake_clarification_phase_design.md`](intake_clarification_phase_design.md) (ADR-0045).

## 1. Overview

### 1.1 Purpose

Make AutoDev **reproduce a bug before it plans the fix.** Adopt the diagnose discipline — *build a feedback
loop → reproduce → hypothesise → instrument → fix + regression-test → cleanup + post-mortem* — as the
bug-fix workflow, so the fix is tied to a fast, deterministic, agent-runnable red→green signal on the user's
actual symptom rather than to a plausible code reading.

### 1.2 Scope

**In scope (Phases 1–4, as an orchestrator phase):** sandbox-aware feedback-loop construction; reproduction;
3–5 ranked falsifiable hypotheses; targeted instrumentation; a confirmed root cause; a seam-availability
finding; the synthetic-loop + delivered-live-repro fallback. **In scope (woven into execute, Phases 5–6):**
regression-test-before-fix wired as the task acceptance signal; the reproduce-gate and debug-tag cleanup
gate; the post-mortem architectural recommendation.

**Out of scope:** the altitude decision (ADR-0044 owns it — diagnosis *feeds* it); spec completeness
(ADR-0045 owns it); feature work (the phase is bug-only).

### 1.3 Context

Inserted into `run_plan_phase` after `explorer`/`domain_expert` and **before** `run_framing_phase`
(`plan_phase.py:744`). Gated on "is-bug-fix". The autonomous agent has no network/TTY/live-creds
(`architect.md:1108`), which reshapes how Phase 1 is run here.

## 2. Requirements

### 2.1 Functional Requirements
- **FR1 — Build a loop (sandbox-ordered).** Construct the strongest *sandbox-runnable* pass/fail signal,
  trying methods in AutoDev-appropriate order (§5.1). Treat the loop as a product: faster, sharper, more
  deterministic.
- **FR2 — Reproduce.** Run the loop; confirm it produces the *user's* failure mode (captured symptom), not a
  nearby one; confirm it's reproducible (or, for flaky bugs, raise the rate until debuggable).
- **FR3 — Hypothesise.** Generate 3–5 ranked, falsifiable hypotheses (each states its prediction) before
  testing any. Proceed with the ranking autonomously; surface to the operator only if ADR-0045 intake is
  interactive.
- **FR4 — Instrument.** One variable at a time; each probe maps to a Phase-3 prediction; tag debug logs
  `[DEBUG-xxxx]`; prefer debugger/REPL > targeted logs; for perf, measure-first (baseline → bisect).
- **FR5 — Confirm cause + seam finding.** Record the confirmed root cause and a `seam` verdict
  (`correct` | `shallow` | `none`); `none`/`shallow` is the architectural finding fed to framing.
- **FR6 — Sandbox fallback.** When only a *live* loop reproduces, build the best synthetic/replay loop for
  the autonomous signal, **deliver the live-repro as an artifact** (script + procedure), and label
  `loop_fidelity` honestly (`live`|`synthetic`|`replay`|`none`).
- **FR7 — Regression test before fix (execute).** Turn the minimised repro into a failing test at the
  correct seam; watch it fail; apply the fix; watch it pass; re-run the Phase-1 loop on the un-minimised
  scenario. If no correct seam: document it (don't fake a shallow one).
- **FR8 — Cleanup + post-mortem (execute wrap-up).** Original repro no longer reproduces; regression passes
  (or seam-absence documented); all `[DEBUG-...]` removed; throwaway prototypes deleted; correct hypothesis
  recorded in the commit/PR; "what would have prevented this?" → architectural recommendation.

### 2.2 Non-Functional Requirements
- **NFR1 — Gate, not advice:** planning-the-fix cannot start without a believed-in loop OR a recorded
  live-only finding + synthetic loop + delivered artifact.
- **NFR2 — Sandbox-compat:** never assumes network/TTY/creds; degrades to synthetic + artifact.
- **NFR3 — Autonomy contract:** no mid-run human prompts (Phase-3 proceeds with the ranking).
- **NFR4 — Determinism/resume:** loop + hypotheses + cause persist; resume re-reads, never re-instruments.
- **NFR5 — Honesty:** `loop_fidelity` must never report `live` on a network-less run.
- **NFR6 — Cost:** bug-only; feature tasks skip the phase entirely.

### 2.3 Constraints
- Runs before framing (the altitude decision needs the confirmed cause + seam signal).
- Reuses the explorer evidence (no second exploration pass) to locate the bug code path.
- Tagged-instrumentation cleanup is mandatory before a task leaves `tested`.

## 3. Architecture

### 3.1 High-Level Design
```
run_plan_phase: intake(0045) → explorer → domain_expert
   └─ is-bug-fix? ── no ─────────────────────────────► framing(0044) → architect → ...
      │ yes
      └─ run_diagnosis_phase(spec, explore_ev):
           1 BUILD LOOP   sandbox-ordered (§5.1)
           2 REPRODUCE    run loop; confirm user's symptom
           3 HYPOTHESISE  3–5 ranked, falsifiable
           4 INSTRUMENT   one var at a time; [DEBUG-xxxx]; confirm cause
           5 SEAM FINDING correct | shallow | none
           gate: loop believed-in OR (live-only ⇒ synthetic loop + delivered artifact + fidelity label)
      └─ framing(0044)  ◄── confirmed_cause + seam signal (recurrence_at_seam / no_correct_seam)
      └─ architect → plan → execute:
           Phase 5 regression-test-before-fix  (loop = acceptance signal; reproduce-gate red→green)
           Phase 6 cleanup (debug-tag gate) + post-mortem → architectural recommendation
```
Flag-guarded and fail-safe: an uncaught error degrades to "skip diagnosis, proceed with a logged note"
(diagnosis must never block planning outright — but a *clean* "couldn't reproduce" is recorded, not hidden).

### 3.2 Component Structure
- `src/orchestrator/diagnosis_phase.py` *(new)* — `run_diagnosis_phase(...) -> DiagnosisOutcome`.
- `src/agents/prompts/diagnostician.md` *(new)* — Phases 1–4 + sandbox ordering + fallback branch.
- `src/qa/reproduce_gate.py`, `src/qa/debug_tag_gate.py` *(new)* — Phase 5/6 gates.
- `developer.md` / `test_engineer.md` *(extend)* — Phase-5 regression-first, Phase-6 cleanup.

### 3.3 Data Models
`extra="forbid"`, in `src/state/schemas.py` (mirroring `FramingEvidence`):
```python
class FeedbackLoop(BaseModel):
    method: Literal["failing_test","replay_trace","throwaway_harness","property_fuzz",
                    "differential","bisection","cli_snapshot","dev_server_curl","headless_browser","hitl"]
    command: str                       # how to run it (agent-runnable)
    fidelity: Literal["live","synthetic","replay","none"]
    deterministic: bool
    runtime_s: float | None

class Hypothesis(BaseModel):
    rank: int
    statement: str
    prediction: str                    # "if X, then changing Y makes it disappear" (falsifiable)
    status: Literal["untested","supported","refuted"] = "untested"

class DiagnosisEvidence(_BaseEvidence): # kind="diagnosis"; added to the Evidence union (schemas.py:675)
    loop: FeedbackLoop | None
    reproduced: bool
    symptom: str                       # exact captured failure mode
    hypotheses: list[Hypothesis]
    confirmed_cause: str | None
    seam: Literal["correct","shallow","none"]
    live_repro_artifact: str | None    # path to delivered script/procedure when fidelity != live
```
`DiagnosisOutcome` (in-memory): `{ evidence, structural_signals: list[str] }` where signals feed framing
(e.g. `recurrence_at_seam`, `no_correct_seam`).

### 3.4 State Machine
`GATE_IS_BUG → BUILD_LOOP → (REPRODUCED | LIVE_ONLY) → HYPOTHESISE → INSTRUMENT → CONFIRM → SEAM → DONE`.
- `LIVE_ONLY`: build synthetic/replay loop + emit live-repro artifact + label fidelity, then continue.
- `DEGRADE` on uncaught error: record "diagnosis incomplete" + proceed (logged), never silently green.
- Resume: read persisted `DiagnosisEvidence` → `DONE`.

### 3.5 Protocol / Interface Contracts
- **In:** clarified spec (ADR-0045 output), explorer evidence, `orch`.
- **Out:** `plan-diagnosis-diagnosis.json`, `DiagnosisOutcome`, structural signals to framing, an optional
  live-repro artifact under the repo (e.g. `scripts/repro/`), ledger ops.
- **Reproduce-gate contract:** given the persisted `FeedbackLoop.command`, the gate asserts *fail on the
  pre-fix tree* and *pass on the post-fix tree*; a loop that passes pre-fix is rejected as not-reproducing.

### 3.6 Interfaces
- Config `orch.cfg.diagnosis` (`DiagnosisPhaseConfig`). Kill-switch `AUTODEV_DIAGNOSIS_DISABLED=1`.

## 4. Design Decisions

### 4.1 Key Decisions
- **KD1 — Reproduce before plan, as a gate** (not advice): the anchoring failure the skill warns about is
  prevented structurally, like ADR-0044 made the altitude decision a phase rather than a hope.
- **KD2 — Sandbox loop-ordering.** Re-rank the skill's 10 methods for a network-less, TTY-less agent (§5.1).
- **KD3 — Synthetic-loop + delivered-artifact fallback** for live-only bugs — the skill's "cannot build a
  loop" branch, made into a deliverable instead of a deadlock; fidelity labelled honestly.
- **KD4 — Seam-finding feeds framing.** "No correct seam" is the architectural signal, routed to ADR-0044.
- **KD5 — Bug-only & conditional.** Feature work skips diagnosis (cost + relevance).
- **KD6 — Loop as the acceptance signal.** Execute's reproduce-gate uses the persisted loop red→green —
  stronger than "suite passes".

### 4.2 Trade-offs
- A diagnosis phase costs LLM calls + wall-clock on every bug run vs proven-not-plausible fixes. Accepted.
- The synthetic proxy can disagree with reality; mitigated by the delivered live-repro + fidelity label.
- New QA gates add surface; the reproduce-gate must tolerate the loop's stated nondeterminism bound.

## 5. Implementation Details

### 5.1 Sandbox-ordered loop construction (the core, FR1)
Re-rank the skill's methods for the autonomous sandbox (no network/TTY/creds):
1. **Failing test at a seam** (unit/integration) — first choice.
2. **Replay a captured trace** — save the real payload/event (e.g. a recorded oversized observation) to a
   fixture; replay it through the code path. *Ideal for the Mistral-429 class.*
3. **Throwaway harness** — minimal in-process subset, mocked deps, single function call.
4. **Property / fuzz loop** — for "sometimes wrong output".
5. **Differential loop** — same input through two configs/versions, diff outputs.
6. **Bisection harness** — `git bisect run` when the bug appeared between known states.
7. **CLI-snapshot** — invoke a CLI with a fixture, diff stdout. *(Available if no live service needed.)*
8. **Dev-server curl / headless browser / HITL** — only when the agent can boot the service *in-sandbox*;
   otherwise these become the **live-repro artifact** (§5.2), not the autonomous loop.
Iterate on the loop: pin time, seed RNG, isolate fs, narrow scope; target a ≤ few-second deterministic loop.

### 5.2 Live-only fallback (FR6)
When the believed-in loop would require a live API/service/credentials (Mistral-429: a real key + connected
mailbox), the diagnostician: (a) builds the best **replay/synthetic** loop for the autonomous pass/fail
signal (replay a captured oversized payload + a stubbed `429` to drive the de-amplified-retry assertion);
(b) writes a **live-repro script + documented procedure** to `scripts/repro/` (real `principal_id`,
`limit=15`) for a human; (c) sets `loop_fidelity="synthetic"|"replay"` and records the artifact path. This
is the ADR-0045 "both" done-bar realised.

### 5.3 Hypotheses (FR3) & instrumentation (FR4)
`diagnostician` emits ≤ `max_hypotheses` ranked, falsifiable hypotheses with predictions; instruments one
variable at a time, each probe tied to a prediction, logs tagged `[DEBUG-<id>]`. Perf branch: baseline
measurement + bisect, never "log everything".

### 5.4 Phase 5 in execute (FR7)
The persisted `FeedbackLoop` is handed to `developer`/`test_engineer`: write the failing regression test at
the `correct` seam first → watch fail → fix → watch pass → re-run the loop. If `seam == none|shallow`, skip
the false-confidence test and rely on the loop + the recorded finding (already routed to framing).

### 5.5 Phase 6 gates (FR8)
- **reproduce-gate** (`src/qa/reproduce_gate.py`): loop fails pre-fix, passes post-fix.
- **debug-tag cleanup gate** (`src/qa/debug_tag_gate.py`): scan for `[DEBUG-...]`; block if any remain
  (reuse the secret-scan scanning machinery).
- Post-mortem: emit the "what would have prevented this?" recommendation; if architectural, file it for
  `re_architect` / a follow-up plan — *after* the fix is in.

### 5.6 Configuration
```python
class DiagnosisPhaseConfig(BaseModel):              # mirror FramingPhaseConfig
    enabled: bool                                   # default True via _default_diagnosis_cfg
    bug_only: bool = True
    max_hypotheses: int = Field(default=5, ge=3, le=5)
    loop_methods: list[str] = [...]                 # the §5.1 ordered allowlist
    require_loop_to_plan: bool = True
    on_no_live_loop: Literal["synthetic_plus_artifact","block"] = "synthetic_plus_artifact"
    diagnostician_model: str | None = None
# AutodevConfig.diagnosis: DiagnosisPhaseConfig = Field(default_factory=_default_diagnosis_cfg)
```

## 6. Integration Points
- **6.1 Depends on:** explorer evidence, the adapter (diagnostician dispatch), `write_evidence`, the ledger.
- **6.2 Adapter contract:** `diagnostician` is a stateless subprocess role (ADR-0001), `StubAdapter`-tested;
  any in-sandbox service boot uses repo shell access (no external network).
- **6.3 Ledger emissions:** `diagnosis_loop_built`, `bug_reproduced` / `repro_unavailable_live`,
  `hypotheses_ranked`, `cause_confirmed`, `seam_finding`.
- **6.4 Depended on by:** framing (cause + seam signal), execute (loop = acceptance), the reproduce/cleanup
  gates, the post-mortem recommendation.
- **6.5 External systems:** none on the autonomous path; live-repro artifacts target a human's environment.

## 7. Testing Strategy
- **7.1 Unit:** loop-method selection under a network-less probe; `loop_fidelity` honesty; hypothesis
  falsifiability validation (reject "vibe" hypotheses with no prediction).
- **7.2 Integration (merge gate):** #199 replay — build a replay loop (captured oversized payload +
  stubbed 429) that goes red pre-fix / green post-fix; deliver a live-repro script; emit a `seam` finding
  consumed by framing.
- **7.3 Gate tests:** reproduce-gate red→green + reject a loop that passes pre-fix; debug-tag gate blocks a
  leftover `[DEBUG-...]`.
- **7.4 Test data:** a captured oversized-observation fixture + a stubbed Mistral-429 response.

## 8. Security Considerations
- Captured traces/payloads (HAR, logs) may contain secrets/PII — run them through the secret-scan before
  they land as fixtures; redact mailbox contents in replay fixtures.
- Instrumentation is removed by the cleanup gate; no `[DEBUG-...]` or throwaway harness ships.

## 9. Performance Considerations
- Feature tasks: phase skipped (0 cost).
- Bug tasks: loop-build + reproduce + hypothesise + instrument ≈ a bounded set of diagnostician calls
  (reusing explorer evidence); the loop itself is engineered to run in seconds.
- For perf-regression bugs, the loop is a timing/profiler harness (measure-first), not logs.
- Resume: 0 added calls (evidence re-read).
