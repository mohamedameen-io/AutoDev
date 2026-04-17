# ADR-008: Deterministic FSM Orchestration

**Status:** Accepted
**Date:** 2026-04-17
**Deciders:** Mohamed Ameen
**Tags:** orchestrator, FSM, determinism, control-flow, agents
**Related ADRs:** ADR-009 (Pydantic strict validation constrains FSM payloads), ADR-010 (conservative tiebreak is a leaf decision within the FSM)

## Context

AutoDev orchestrates multi-agent coding workflows where several specialist LLM agents (explorer, architect, developer, reviewer, test_engineer, tournament roles) must execute in a precise order for each task. The central design question is: **who decides what agent to call next?**

Three architectures are possible: (A) let an LLM agent decide the next step dynamically, (B) encode the workflow as a deterministic Python FSM with LLM agents invoked as stateless leaf calls, or (C) express the workflow as a declarative DAG executed by an external engine. The decision affects predictability, debuggability, cost, testability, and crash recovery across the entire orchestrator, plan phase, and execute phase subsystems.

The execute phase has a rich state machine: a task progresses through `pending -> in_progress -> coded -> auto_gated -> reviewed -> tested -> tournamented -> complete`, with any in-flight state able to fall back to `in_progress` on retry or `blocked` on hard failure. This FSM must be enforced reliably regardless of LLM output quality.

## Options Considered

### Option A: LLM as Router (Agent Picks Next Agent Dynamically)

**Description:**
A "coordinator" LLM agent receives the current state and decides which specialist to invoke next. The LLM output includes both the task response and a routing instruction (e.g., "next: reviewer" or "next: rewrite"). This is the pattern used by systems like AutoGPT and some LangChain agent executors.

**Pros:**
- Maximum flexibility: the LLM can adapt the workflow to novel situations
- No hard-coded state machine to maintain
- Can handle unforeseen edge cases through reasoning

**Cons:**
- Non-deterministic: same inputs can produce different execution paths depending on LLM sampling
- Expensive: every routing decision is an additional LLM call (the router call itself produces no useful work)
- Difficult to debug: "why did the agent skip the reviewer?" requires inspecting LLM reasoning traces
- Crash recovery is fragile: must reconstruct the LLM's internal state to resume
- Difficult to test: cannot assert call sequences without mocking the LLM's routing logic
- Unpredictable cost: a routing hallucination can trigger infinite agent loops
- Hard to enforce mandatory gates (QA, review) — the LLM can simply skip them

### Option B: Deterministic Python FSM with LLM as Leaves (Current Choice)

**Description:**
A Python finite-state machine in `orchestrator/task_state.py` defines all legal transitions. The orchestrator (`__init__.py`) drives the plan phase (`plan_phase.py`) and execute phase (`execute_phase.py`) as explicit Python control flow. LLM agents are invoked as stateless leaf calls via `adapter.execute(inv)` — they produce output but never decide what happens next. The FSM prescribes every transition; agents cannot skip gates or alter the workflow.

**Pros:**
- Fully deterministic: same inputs always produce the same execution path through the FSM
- Debuggable: step through `execute_phase.py` with a standard Python debugger
- Cost-efficient: zero LLM calls wasted on routing decisions
- Testable: `StubAdapter` + mock agents allow asserting exact call sequences offline
- Crash recovery: FSM state is persisted in the append-only JSONL ledger; `resume()` picks up from the last checkpoint
- Mandatory gates enforced by code: QA gates, reviewer, and test_engineer cannot be skipped
- Platform-agnostic: the FSM works identically across Claude Code, Cursor, and Inline adapters

**Cons:**
- Less flexible: adding a new workflow step requires a code change to the FSM transitions and execute loop
- Cannot dynamically adapt to novel situations (e.g., "this task needs a designer review" must be pre-coded)
- The transition table must be maintained manually as the workflow evolves

### Option C: DAG-Based Workflow Engine (e.g., Airflow-Style)

**Description:**
Express the workflow as a directed acyclic graph of tasks with dependencies, executed by a general-purpose workflow engine. Each node is an agent invocation; edges define ordering and retry/failure semantics.

**Pros:**
- Declarative: the workflow is a data structure, not imperative code
- Visual tooling: DAG engines typically offer web UIs for monitoring execution
- Mature retry/failure/timeout semantics built into the engine
- Supports parallelism naturally (independent nodes execute concurrently)

**Cons:**
- Heavy dependency: requires a workflow engine runtime (Airflow, Prefect, Temporal)
- Impedance mismatch: AutoDev's per-task retry loop with reviewer feedback cycles is stateful and cyclic (retry back to developer on review failure), which DAGs handle poorly without workarounds
- Over-engineered for a single-user CLI tool: AutoDev runs as `autodev execute`, not as a long-lived service
- Asyncio integration is non-trivial with most DAG engines
- Adds operational complexity (database, scheduler, worker processes) for a tool that should be pip-installable

## Decision Drivers

- **Determinism:** Same inputs must produce the same execution path through the FSM
- **Crash Safety:** Process-kill resilience via ledger-persisted FSM state
- **LLM Cost Efficiency:** Zero LLM calls spent on routing; every call produces useful work
- **Testability:** `StubAdapter` support for deterministic, offline testing of exact call sequences
- **Stateless Reproducibility:** Fresh subprocess per adapter call; no hidden mutable state in agents
- **Platform Portability:** Must work identically across Claude Code, Cursor, and Inline adapters

## Architecture Drivers Comparison

| Architecture Driver        | Option A: LLM Router | Option B: Python FSM (chosen) | Option C: DAG Engine | Notes |
|----------------------------|----------------------|-------------------------------|---------------------|-------|
| **Determinism**            | ⭐ (1/5)             | ⭐⭐⭐⭐⭐ (5/5)                 | ⭐⭐⭐⭐ (4/5)         | LLM sampling is inherently stochastic; DAG is deterministic but cycles need workarounds |
| **Crash Safety**           | ⭐⭐ (2/5)            | ⭐⭐⭐⭐⭐ (5/5)                 | ⭐⭐⭐⭐ (4/5)         | FSM state in ledger survives SIGKILL; DAG engines have their own persistence |
| **LLM Cost Efficiency**    | ⭐⭐ (2/5)            | ⭐⭐⭐⭐⭐ (5/5)                 | ⭐⭐⭐⭐⭐ (5/5)        | Option A wastes calls on routing; B and C call agents only for useful work |
| **Testability**            | ⭐⭐ (2/5)            | ⭐⭐⭐⭐⭐ (5/5)                 | ⭐⭐⭐ (3/5)          | FSM + StubAdapter = fully offline tests; DAG engines add test infrastructure overhead |
| **Asyncio Compatibility**  | ⭐⭐⭐ (3/5)          | ⭐⭐⭐⭐⭐ (5/5)                 | ⭐⭐ (2/5)           | Native async/await in FSM; most DAG engines are sync or need adapters |
| **Platform Portability**   | ⭐⭐⭐ (3/5)          | ⭐⭐⭐⭐⭐ (5/5)                 | ⭐⭐ (2/5)           | FSM is adapter-agnostic; DAG engine is an external dependency |
| **Complexity**             | ⭐⭐⭐ (3/5)          | ⭐⭐⭐⭐ (4/5)                  | ⭐⭐ (2/5)           | FSM is ~68 lines; DAG engine adds operational weight |
| **Flexibility**            | ⭐⭐⭐⭐⭐ (5/5)       | ⭐⭐⭐ (3/5)                   | ⭐⭐⭐⭐ (4/5)         | LLM can adapt dynamically; FSM requires code changes for new steps |

## Decision Outcome

**Chosen Option:** Option B: Deterministic Python FSM with LLM as Leaves

**Rationale:**
The FSM approach dominates on the three drivers that matter most for a multi-agent coding tool: determinism, testability, and cost efficiency. AutoDev's entire value proposition depends on reliable, repeatable workflows. If the orchestrator takes a different path on each run, debugging failures becomes impossible and cost becomes unpredictable. The FSM in `task_state.py` is only 68 lines and exhaustively tested (every valid and invalid transition is covered by parametrized tests), yet it governs the entire execution lifecycle.

The flexibility trade-off is acceptable: AutoDev's workflow is well-defined and evolves through explicit code changes, not ad-hoc LLM reasoning. When a new gate or step is needed (e.g., the planned `sast_scan` and `mutation_test` gates), it is added as a new state in the transition table and a new step in `execute_phase.py`. This is a small, reviewable change compared to debugging why an LLM router decided to skip a mandatory gate.

**Key Factors:**
- Zero wasted LLM calls: every `adapter.execute()` call produces useful output (code, review, tests)
- Crash recovery is trivial: the ledger records every state transition; `resume()` reads the ledger and re-enters the loop at the correct step
- The entire FSM is testable offline with `StubAdapter`, enabling tests like `test_orchestrator_task_state.py` that verify all 17 valid transitions and 11 invalid transitions without any LLM calls

## Consequences

### Positive Consequences
- Every execution of the same plan with the same adapter responses follows an identical code path
- The retry loop (developer -> QA gates -> reviewer -> test_engineer -> back to developer on failure) is expressed as a Python `while True` loop with explicit break conditions, making it trivially debuggable
- Cost is predictable: for a task with no retries, exactly 4 agent calls (developer + reviewer + test_engineer + tournament) plus QA gate subprocess calls
- `_find_in_progress_task()` can scan the plan for any non-terminal task to implement crash recovery without replaying LLM state

### Negative Consequences / Trade-offs
- Adding a new workflow step (e.g., "security review" between reviewer and test_engineer) requires modifying `TASK_TRANSITIONS`, adding a new `TaskStatus` literal, and updating `execute_phase.py`
- The FSM cannot adapt to unexpected situations (e.g., a task that needs a different set of specialists); such cases must be pre-coded or handled via the escalation path (critic_sounding_board)
- Cyclic retry paths (reviewer NEEDS_CHANGES -> back to developer) make the transition graph more complex than a simple linear pipeline

### Neutral / Unknown Consequences
- As the number of states grows, the transition table becomes harder to reason about visually; a diagram generator may be needed
- The `TODO(phase-7)` and `TODO(phase-8)` markers in the execute loop indicate that the FSM is still evolving; future states may require restructuring

## Implementation Notes

**Files Affected:**
- `src/orchestrator/task_state.py` - Defines `TASK_TRANSITIONS` dict and `can_transition()`/`assert_transition()` guard functions
- `src/orchestrator/__init__.py` - `Orchestrator` class wires config, adapter, state, and exposes `plan()`, `execute()`, `resume()`, `status()` high-level operations
- `src/orchestrator/plan_phase.py` - Sequential FSM for plan drafting: spec -> explorer -> domain_expert -> architect -> parse -> optional tournament -> save
- `src/orchestrator/execute_phase.py` - Per-task FSM loop: developer -> coded -> QA gates -> auto_gated -> reviewer -> reviewed -> test_engineer -> tested -> tournament -> tournamented -> complete
- `src/state/schemas.py` - `TaskStatus` Literal type defines the 10 legal states
- `tests/test_orchestrator_task_state.py` - Parametrized tests for all valid/invalid transitions
- `tests/test_orchestrator_execute_phase.py` - End-to-end execute loop tests with StubAdapter

**Ledger/State Implications:**
- Every FSM transition is recorded as an `update_task_status` operation in the append-only JSONL ledger
- The `LedgerEntry.op` field captures the operation type; `payload` carries the task ID and new status
- CAS hash chaining ensures ledger integrity: `entry[n].prev_hash == entry[n-1].self_hash`
- On `resume()`, the orchestrator replays the ledger to reconstruct the current Plan state, finds the first non-terminal task, and re-enters the execute loop

**General Guidance:**
- Always call `assert_transition()` before updating task status to catch illegal transitions at development time
- New states should be added to both `TaskStatus` in `schemas.py` and `TASK_TRANSITIONS` in `task_state.py`; the test `test_every_status_is_a_key()` will catch missing keys
- LLM agents must remain stateless: they receive a fully-rendered prompt and return a result; they never read the ledger or decide what step comes next

## Evidence from Codebase

**Source References:**
- `src/orchestrator/task_state.py:23-37` - `TASK_TRANSITIONS` dict defining all 10 states and their legal outgoing transitions
- `src/orchestrator/task_state.py:40-51` - `can_transition()` with explicit self-loop handling (only `in_progress -> in_progress` is allowed)
- `src/orchestrator/execute_phase.py:90-301` - `_execute_one()` implements the full FSM loop: developer(L102) -> coded(L148) -> QA gates(L151) -> auto_gated(L165) -> reviewer(L168) -> reviewed(L212) -> test_engineer(L215) -> tested(L263) -> tournament(L266) -> tournamented(L290) -> complete(L293)
- `src/orchestrator/execute_phase.py:304-357` - `_try_retry_or_escalate()` handles the retry-back-to-in_progress fallback and critic_sounding_board escalation
- `src/orchestrator/__init__.py:230-242` - `_find_in_progress_task()` scans for any task in a non-terminal in-flight state for crash recovery
- `src/orchestrator/plan_phase.py:53-168` - `run_plan_phase()` implements the sequential plan FSM: spec -> explorer -> domain_expert -> architect -> parse (with retry) -> optional tournament -> save

**Test Coverage:**
- `tests/test_orchestrator_task_state.py::test_valid_transitions_allowed` - Parametrized over 17 valid transitions; verifies `can_transition()` and `assert_transition()` accept each
- `tests/test_orchestrator_task_state.py::test_invalid_transitions_raise` - Parametrized over 11 invalid transitions (e.g., `pending -> coded`, `complete -> in_progress`); verifies ValueError raised
- `tests/test_orchestrator_task_state.py::test_terminal_states_have_no_outgoing_transitions` - Asserts `complete` and `skipped` have empty transition sets
- `tests/test_orchestrator_task_state.py::test_every_status_is_a_key` - Structural invariant: every status mentioned anywhere in the transition table exists as a key
- `tests/test_orchestrator_execute_phase.py` - End-to-end execute loop with `StubAdapter`, testing happy path and retry/escalation scenarios

**Property-Based Tests (Hypothesis):**
- N/A for the FSM itself, though the tournament Borda aggregation used within the FSM has extensive Hypothesis tests (see ADR-010)

## Related Design Documents

- [adapters.md](../../docs/design_documentation/adapters.md) - Defines the `PlatformAdapter` protocol that the FSM invokes as leaf calls; adapter-agnostic design enables the same FSM to work across Claude Code, Cursor, and Inline
- [agents.md](../../docs/design_documentation/agents.md) - Describes the 14 specialist agents invoked by the FSM; each is stateless and single-purpose
- [tournaments.md](../../docs/design_documentation/tournaments.md) - Tournament is a step within the FSM (the `tournamented` state); the tournament itself has its own convergence loop but is invoked deterministically by the FSM
- [cost.md](../../docs/design_documentation/cost.md) - Cost predictability depends directly on the deterministic FSM: fixed number of calls per task with known retry bounds

## Monitoring and Review

- [ ] Review date: 2026-10-17
- [ ] Success criteria: No incidents caused by non-deterministic execution paths; all failures reproducible from ledger replay
- [ ] Metrics to track: Frequency of escalations (tasks reaching critic_sounding_board), average retry count per task, proportion of tasks completing in a single pass through the FSM

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-04-17 | Mohamed Ameen | Initial ADR created |
