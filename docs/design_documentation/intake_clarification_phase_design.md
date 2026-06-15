# Intake & Clarification Phase Design

> Companion deep spec for [ADR-0045](../decisions/0045-intake-clarification-phase.md). Where the ADR
> argues *why*, this document specifies *how*: the phase FSM, schemas, gather adapters, prompts, the
> headless policy, and the test matrix. Models its structure on
> [`framing_altitude_phase_design.md`](framing_altitude_phase_design.md) (ADR-0044), the immediately
> downstream neighbour in the plan pipeline.

## 1. Overview

### 1.1 Purpose

Give AutoDev the front-of-pipeline behaviour of a senior engineer handed a thin ticket: **read around it,
pull the linked issue, skim the code, and ask the two or three questions that actually matter** — then
commit to a plan and execute autonomously. Today the pipeline either accepts a spec or rejects it
(`spec_validator.py`); there is no step that *resolves* under-specification. The intake phase adds:
**assess → gather → enrich → clarify (once) → lock → autonomous.**

### 1.2 Scope

**In scope:** a new pre-plan phase (`run_intake_phase`); a structured-gaps upgrade to the spec validator;
autonomous context gather from the repo + external sources (GitHub issues/PRs, Jira, prior sessions);
LLM-assisted enrichment with provenance; constraint-focused clarifying-question generation; a single
batched operator interaction with a headless fallback policy; spec-lock + ledger/evidence persistence.

**Out of scope:** the altitude (patch-vs-architecture) decision — that is owned by the framing phase
(ADR-0044) and must **not** be pre-empted here; any mid-plan or post-plan interaction (the autonomy clause
stays in force for every downstream agent); auto-fixing the bug (intake produces a *spec*, not a diff).

### 1.3 Context

The plan pipeline is `explorer → domain_expert → framing → architect → plan-tournament` inside
`run_plan_phase` (`plan_phase.py:667`). The spec validator (`spec_validator.py`, v0.36.0 "G1") is a cheap
deterministic front-gate that **rejects** thin specs and points at `--skip-spec-validation`. On the
Synaptix Mistral-429 benchmark, the repo `bug.md` was rejected for `spec_no_acceptance_signal` while the
canonical GitHub issue #199 — same bug — carried a full problem statement nobody pulled. Intake exists to
turn that detect-and-reject into gather-and-resolve, while preserving the autonomy contract and leaving
altitude to framing.

## 2. Requirements

### 2.1 Functional Requirements

- **FR1 — Completeness assessment.** Deterministically classify the intent's gaps along named dimensions
  (scope, acceptance/success signal, constraints, concrete repo touchpoints). Reuse/extend
  `spec_validator` so a well-formed spec is a 0-cost pass-through.
- **FR2 — Autonomous gather.** For each gap, gather facts from configured sources: the repo (reuse the
  explorer pass), referenced GitHub issues/PRs (`gh`), Jira (MCP), and prior AutoDev sessions/ledgers.
  Each fact carries a source ref.
- **FR3 — Enrichment.** Produce an enriched spec draft = raw intent + gathered facts, with provenance
  inline; never assert a fact absent from gathered evidence.
- **FR4 — Clarifying questions.** From residual (gather-unresolved) gaps, generate ≤ `max_questions`
  questions, each **constraint-focused**, each with 2–4 options and a recommended default.
- **FR5 — Single batched ask.** Present all questions at once to the operator; collect answers; then lock.
  No further questions for the remainder of the run.
- **FR6 — Headless fallback.** With no operator (cron/CI/`--yes`), apply `on_unanswered` policy:
  `assume_defaults` (use recommended options, log as assumptions) | `block` (stop, emit the question set)
  | `fail` (non-zero exit).
- **FR7 — Spec lock + persist.** Write the enriched, answered spec to `.autodev/spec.md`; record gather
  provenance, Q&A, and assumptions as `intake` evidence + ledger ops; compute the locked spec_hash.
- **FR8 — Deterministic resume.** On resume, re-read `plan-intake` evidence + the locked spec; never
  re-gather or re-ask.

### 2.2 Non-Functional Requirements

- **NFR1 — Autonomy contract:** at most one interaction, before the autonomous run begins.
- **NFR2 — Cost:** well-formed spec → +0 LLM calls; gap path → +1 enrich (+ optional +1 question-gen if
  not folded), reusing the explorer pass; external gather is non-LLM.
- **NFR3 — Determinism/resume** (NFR mirror of FR8): same inputs + same answers → same locked spec_hash.
- **NFR4 — No-pre-empt-altitude:** generated questions contain zero solution-shaped items (audited).
- **NFR5 — Crash-safety:** persist `intake` evidence atomically before threading the locked spec downstream.
- **NFR6 — Pydantic strictness:** all intake models `extra="forbid"`; malformed gather/enrich → fail safe
  to ask/block, never silent fabrication.

### 2.3 Constraints

- Must run **before** the explorer findings feed framing and must hand framing the *locked enriched spec*,
  not the raw intent.
- Must **reuse** the explorer for repo gather (no second exploration pass).
- External-source access must be allowlistable and benchmark-safe (`exclude_globs` / `sources`).
- The clarifier must never enumerate or select solution strategies (ADR-0044 boundary).

## 3. Architecture

### 3.1 High-Level Design

```
run_plan_phase(orch, intent)
  └─ run_intake_phase(orch, intent) ──────────────────────────────┐
       1. ASSESS     spec_validator.assess(intent) -> Gaps        │  (deterministic, cheap)
       2. gate: gaps empty? ── yes ─────────────► lock(intent) ───┤  (no-op fast path)
       3. GATHER     repo (reuse explorer) + gh/jira/sessions     │  (non-LLM external + 1 explorer)
       4. ENRICH     intake_enricher(intent, gathered) -> draft   │  (+1 LLM call)
       5. QUESTION   intake_clarifier(draft, residual_gaps) -> Qs │  (constraints-only; folded w/ 4)
       6. ASK        interactive ? operator : on_unanswered policy │  (≤1 human round)
       7. LOCK       write spec.md + intake evidence + spec_hash  │  (atomic, ledgered)
  ◄────────────────────── locked enriched spec ───────────────────┘
  └─ explorer → domain_expert → run_framing_phase → architect → tournament   (fully autonomous)
```

The phase is **flag-guarded and fail-safe**: any uncaught error degrades to "use the raw intent +
proceed" (logged), exactly as framing degrades to `local_patch` — intake must never block planning.

### 3.2 Component Structure

- `src/orchestrator/intake_phase.py` *(new)* — `run_intake_phase(orch, intent) -> IntakeOutcome`; the
  six-step FSM above; fail-safe wrapper.
- `src/orchestrator/spec_validator.py` *(extend)* — add `assess(text) -> SpecGaps` returning the named
  missing dimensions; keep `validate_spec`/`validate_spec_text` as thin back-compat wrappers.
- `src/orchestrator/intake_sources/` *(new)* — pluggable gather adapters: `repo.py` (wraps the explorer
  evidence), `github.py` (`gh issue/pr view`), `jira.py` (MCP), `sessions.py` (prior ledgers). Each
  implements a `GatherSource` protocol (cf. ADR-0005 protocol-based plugins).
- `src/agents/prompts/intake_enricher.md`, `intake_clarifier.md` *(new)*.

### 3.3 Data Models

All `extra="forbid"`, in `src/state/schemas.py` (mirroring `FramingEvidence` / `SolutionApproach`):

```python
class SpecGaps(BaseModel):           # from the upgraded validator
    ok: bool
    missing: list[Literal["scope", "acceptance", "constraints", "touchpoints"]]

class GatheredFact(BaseModel):
    source: Literal["repo", "github", "jira", "session"]
    ref: str                          # "file.py:120-134" | "github:org/repo#199" | "PROJ-123" | session-id
    summary: str

class ClarifyingQuestion(BaseModel):
    id: str
    question: str
    kind: Literal["constraint", "environment", "done_bar", "risk_latitude", "compat"]
    options: list[str]                # 2..4
    recommended: str                  # must be one of options

class ClarifyingAnswer(BaseModel):
    question_id: str
    answer: str
    source: Literal["operator", "default_assumed"]

class IntakeEvidence(_BaseEvidence):  # kind="intake"; added to the Evidence union (schemas.py:675)
    raw_intent: str
    gaps: SpecGaps
    gathered: list[GatheredFact]
    enriched_spec: str
    questions: list[ClarifyingQuestion]
    answers: list[ClarifyingAnswer]
    assumptions: list[str]            # defaults applied when unanswered (headless)
    locked_spec_hash: str
    sources_used: list[str]
    excluded_globs: list[str]
```

`IntakeOutcome` (in-memory return): `{ enriched_spec: str, locked_spec_hash: str, evidence: IntakeEvidence }`.

### 3.4 State Machine

States: `ASSESS → (PASS_THROUGH | GATHER) → ENRICH → QUESTION → (ASK | DEFAULT | BLOCK) → LOCK → DONE`.
- `PASS_THROUGH` (gaps empty): straight to `LOCK` with the raw intent as the spec; no LLM/network.
- `ASK` only when an interactive operator is present; otherwise `DEFAULT` (assume) / `BLOCK` / `fail` per
  `on_unanswered`.
- Any state may transition to `DEGRADE` on uncaught error → lock the raw intent, log, proceed.
- On resume: skip straight to `DONE` by reading persisted `IntakeEvidence` + the locked spec.

### 3.5 Protocol / Interface Contracts

**The clarifying-question wire contract** (how questions reach a human across the `/autodev` dispatch
boundary). The CLI emits a machine-readable block the host renders (e.g. Claude Code's question UI):

```json
{ "autodev_intake_questions": [
  { "id": "provider", "question": "...", "kind": "constraint",
    "options": ["Stay on Mistral", "Swap allowed", "Let AutoDev decide"], "recommended": "Stay on Mistral" }
], "max_questions": 4, "on_unanswered": "assume_defaults" }
```

The host returns `{ "answers": [ { "question_id": "provider", "answer": "Stay on Mistral" } ] }`. Absent a
host (headless), the CLI applies `on_unanswered`. This keeps the intake phase UI-agnostic.

### 3.6 Interfaces

- **In:** `intent: str` (raw CLI positional or `.autodev/spec.md`), `orch` (adapter, config, ledger).
- **Out:** locked `.autodev/spec.md`, `IntakeOutcome`, `plan-intake-intake.json` evidence, ledger ops.
- **Config:** `orch.cfg.intake` (`IntakePhaseConfig`).

## 4. Design Decisions

### 4.1 Key Decisions

- **KD1 — Constraints, not solutions (the load-bearing rule).** The clarifier may capture an
  *altitude-latitude preference* ("let AutoDev decide" / "prefer minimal" / "prefer thorough") as a
  **constraint**, but must never enumerate or choose solution strategies — that is framing's job (ADR-0044).
  This is the analogue of ADR-0044 suspending minimality *only* at the choosing step: intake confines
  interaction to *constraints only*. Enforced by the clarifier prompt + a merge-gate corpus audit (NFR4).
- **KD2 — Cheap deterministic gate.** The decision to enrich is made by the (extended) deterministic spec
  scan, never by an LLM call — so well-formed specs cost nothing.
- **KD3 — Single front-loaded round, then lock.** One `AskUserQuestion`-style batch up front; the locked
  spec_hash anchors the rest of the run. Preserves the autonomy contract + resume.
- **KD4 — Headless is first-class.** `on_unanswered` makes cron/CI runs deterministic, not deadlock-prone.
- **KD5 — Reuse the explorer.** Repo gather rides the existing explorer evidence; only external sources are
  net-new I/O.
- **KD6 — Source allowlist.** `sources` + `exclude_globs` make gather benchmark-safe (exclude the solution
  PR) and production-rich (include prior solutions).

### 4.2 Trade-offs

- **+1 LLM call on the gap path** (enrich; +1 more if question-gen isn't folded into the enrich call) vs a
  far richer, provenance-cited spec. Accepted; folded enrich+question into one call where feasible.
- **Network dependency** (gh/Jira) vs autonomy: degrade to "ask the operator" / "proceed with repo-only
  gather" when a source is unreachable.
- **Operator touchpoint** vs pure autonomy: bounded to one front round; headless mode removes it entirely.

## 5. Implementation Details

### 5.1 Completeness assessor (extend `spec_validator`)
Add `assess(text) -> SpecGaps`: run the existing scans (`_MIN_NONWS_CHARS`, `_SCOPE_MARKERS`,
`_ACCEPTANCE_MARKERS`) plus a constraints/touchpoints heuristic; return the *set* of missing dimensions.
`validate_spec_text` becomes `SpecGaps.ok`. Back-compatible: existing callers still get a boolean.

### 5.2 Gather (FR2)
Iterate `cfg.intake.sources`. `repo`: reuse the explorer evidence (`plan-explore`), extract the
bug-relevant call-path/contract facts. `github`: detect `#NNN` / PR references in the intent and repo,
`gh issue view` / `gh pr view` the **issue** (often richer than the pasted summary — the #199 lesson),
honouring `exclude_globs` (never pull a PR matching an excluded pattern, e.g. the solution branch).
`jira`: MCP `jira_get_issue` when a key is present. `session`: prior `.autodev` ledgers on the same files.
Each yields `GatheredFact`s with refs. Bound total gathered size; validate; skip unreachable sources with
a logged note.

### 5.3 Enrichment (FR3)
`intake_enricher` (text role, low temperature, 1 turn) receives `(raw_intent, gathered_facts)` and emits an
enriched spec that **merges** them with inline provenance and an explicit *Success criteria* section
(so the result passes G1). Prompt guard: "Use only the supplied facts; cite each; do not invent." A
post-parse check rejects facts with no `ref`.

### 5.4 Question generation (FR4) — constraints only
`intake_clarifier` receives the enriched draft + residual gaps and emits ≤ `max_questions`
`ClarifyingQuestion`s. The prompt hard-codes KD1: ask about provider/compat/done-bar/risk-latitude/
deadline/data-sensitivity; **never** about which fix to build. Each question gets 2–4 options + a
recommended default. A validation pass drops/flags any question whose text matches solution-shaped
patterns (an explicit denylist + an LLM self-check) before it can reach the operator.

### 5.5 Ask / headless (FR5/FR6)
Interactive: emit the §3.5 JSON block; collect answers. Headless (`not isatty()` or `--assume-defaults`):
apply `on_unanswered`. `assume_defaults` records each unanswered question's recommended option as a
`ClarifyingAnswer(source="default_assumed")` and appends a human-readable line to `assumptions`.

### 5.6 Lock + persist (FR7) & resume (FR8)
Render the final spec (enriched draft + an *Answered constraints* section from the answers). Atomic-write
`.autodev/spec.md` (`state.evidence` atomic write), `write_evidence(cwd, "plan-intake", IntakeEvidence)`,
append the ledger ops, compute `locked_spec_hash`. `run_plan_phase` threads the **locked spec** into all
downstream envelopes. On resume, `run_intake_phase` short-circuits if `plan-intake` evidence exists.

### 5.7 Configuration
```python
class IntakePhaseConfig(BaseModel):           # mirror FramingPhaseConfig (schema.py:389-406)
    enabled: bool                              # default True via _default_intake_cfg
    max_questions: int = Field(default=4, ge=1, le=4)   # host question-UI cap
    sources: list[str] = ["repo", "github", "jira"]
    exclude_globs: list[str] = []              # benchmark contamination guard
    on_unanswered: Literal["assume_defaults", "block", "fail"] = "assume_defaults"
    enricher_model: str | None = None
    clarifier_model: str | None = None
    reuse_explorer_evidence: bool = True
# AutodevConfig.intake: IntakePhaseConfig = Field(default_factory=_default_intake_cfg)  # mirror :1051-1054
```
Kill-switch: `intake.enabled=false` and/or `AUTODEV_INTAKE_DISABLED=1` (precedent: `AUTODEV_FRAMING_DISABLED`,
`AUTODEV_INDEX_DISABLED` at `state/file_index.py:423`).

## 6. Integration Points

### 6.1 Dependencies on Other Components
- `spec_validator` (assessment), the explorer evidence (repo gather), `state.evidence.write_evidence`
  (`:47`), the ledger (`plan_manager.ledger_append`), the adapter (enricher/clarifier dispatch).

### 6.2 Adapter Contract Dependency
- Enricher/clarifier are stateless subprocess roles (ADR-0001), `StubAdapter`-supported for tests. External
  gather uses `gh`/MCP via the runtime, not the LLM adapter.

### 6.3 Ledger Event Emissions
- `intake_assessed`, `intake_gathered`, `intake_enriched`, `intake_questions_posed`, then one of
  `intake_answered` / `intake_defaults_assumed`, then `spec_locked` (with `locked_spec_hash`).

### 6.4 Components That Depend on This
- `run_framing_phase` and the architect consume the **locked enriched spec** (richer input, unchanged
  altitude ownership). The plan-critic verifies against the locked spec's *Success criteria*.

### 6.5 External Systems
- GitHub (`gh`), Jira (MCP). Both optional, allowlisted, and degradable.

## 7. Testing Strategy

### 7.1 Unit Tests
- `spec_validator.assess` gap detection per dimension; back-compat of `validate_spec_text`.
- Each gather adapter against a stub (gh/jira fixtures); `exclude_globs` filtering.
- Headless `on_unanswered` matrix (`assume_defaults`/`block`/`fail`).

### 7.2 Integration Tests — Merge Gates
- **#199 replay (primary gate):** thin `bug.md` in → intake pulls issue #199 + repo call-path facts →
  enriched spec passes G1 without `--skip` → framing classifies at the *same* altitude as the manual run
  (intake improved input **without** changing the altitude decision).
- StubAdapter end-to-end: deterministic `IntakeEvidence`, byte-stable locked spec on fixed answers.

### 7.3 Constraints-not-solutions audit (NFR4 gate)
- Corpus of generated questions; assert **zero** name or select a solution approach (denylist + LLM judge).

### 7.4 Test Data Requirements
- `bug.md` (thin) + issue #199 fixture; a well-formed spec (pass-through, +0 calls); an unreachable-source
  fixture (degrade path).

## 8. Security Considerations
- External sources are **untrusted input**: bound sizes (cf. `_MAX_READ_BYTES`), validate, never execute.
- Secrets: gathered issue/PR text may contain tokens; route through the existing secret-scan before it
  lands in `spec.md`/evidence.
- `exclude_globs` prevents pulling restricted/solution content; default-deny for sources not in `sources`.

## 9. Performance Considerations
- Well-formed spec: deterministic scan only (sub-second), 0 LLM/network.
- Gap path: 1 explorer (already paid) + 1 enrich (+ folded question-gen) + bounded external fetches.
- Resume: 0 added calls (evidence re-read).
- The completeness gate keeps a hard ceiling on intake's own scan (reuse `spec_validator`'s bounded read).
