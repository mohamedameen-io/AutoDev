# ADR-001: Stateless Subprocess Invocations

**Status:** Accepted
**Date:** 2026-04-17
**Deciders:** Mohamed Ameen
**Tags:** adapters, subprocess, stateless, determinism, crash-recovery
**Related ADRs:** ADR-002 (ledger provides the continuity that subprocesses do not)

## Context

AutoDev orchestrates multi-agent coding workflows where each agent invocation translates to a call against an LLM platform CLI (Claude Code, Cursor, or an inline adapter). The central question is how the orchestrator communicates with the LLM: should it maintain a persistent conversational session across invocations, call the CLI fresh each time, or bypass the CLI entirely and hit the HTTP API?

The adapter subsystem (`src/adapters/`) defines a `PlatformAdapter` ABC with two key methods: `execute(inv: AgentInvocation) -> AgentResult` for single calls and `parallel(invs, max_concurrent)` for batched concurrent calls. Every adapter concrete class must implement this contract. The orchestrator's FSM dispatches work through these adapters; the adapter's execution model directly determines crash recovery, testability, cost predictability, and platform portability.

The `ClaudeCodeAdapter` currently spawns a fresh `claude -p` subprocess per invocation via `asyncio.create_subprocess_exec`. The comment in `_build_command` is explicit: "We deliberately do NOT pass `--continue`; every call is fresh." Continuity between agent steps lives entirely in `.autodev/` state files (the JSONL ledger, plan.json, evidence bundles), not in any LLM session.

## Options Considered

### Option A: Persistent Sessions with `--continue`

**Description:**
Maintain a long-lived conversational session per agent role. Each subsequent invocation passes `--continue --session-id <uuid>` to resume the prior context. The LLM sees the full history of its previous turns within a task.

**Pros:**
- LLM retains conversational context across turns, reducing prompt length for follow-up instructions
- Potentially higher quality for multi-turn tasks where the model benefits from seeing its own prior reasoning
- Fewer tokens re-sent per invocation (the platform caches the conversation)

**Cons:**
- Session state lives inside the platform's internal storage, invisible to AutoDev's crash-recovery mechanisms
- A killed process leaves an orphaned session in an unknown state; resuming it may produce corrupted or stale context
- Not all platforms support `--continue` (Cursor agent mode has no equivalent), breaking platform portability
- Non-deterministic: replaying the same orchestrator FSM trace with the same inputs will not reproduce the same LLM context because the session accumulates side effects
- Harder to test: `StubAdapter` would need to simulate session memory to produce realistic results
- Cost is unpredictable: resumed sessions may include arbitrarily large context from prior turns, inflating per-call token usage
- Coupling between orchestrator state and platform session state creates a two-source-of-truth problem

### Option B: Fresh Subprocess per Invocation (Stateless)

**Description:**
Every `adapter.execute()` call spawns a new subprocess with no reference to any prior session. The full prompt is self-contained. Continuity between agent steps is maintained exclusively through `.autodev/` state files (ledger, plan.json, evidence bundles) that are composed into each prompt by the orchestrator.

**Pros:**
- Crash-safe: a killed subprocess leaves no orphaned session; the orchestrator restarts from the last committed ledger entry
- Deterministic: same prompt + same model = same execution path through the FSM (modulo LLM stochasticity)
- Platform-portable: works identically across Claude Code (`claude -p`), Cursor (`cursor agent --print`), and inline adapters
- Testable: `StubAdapter` can return canned `AgentResult` values with zero infrastructure
- Cost-predictable: each call's token usage is bounded by the prompt size, which the orchestrator controls
- Simple concurrency model: `parallel()` just spawns N independent subprocesses under a semaphore
- Clean separation: the adapter knows nothing about plans, tasks, or ledger state

**Cons:**
- Every invocation must include full context in the prompt (no conversational memory), increasing prompt size
- Multi-turn reasoning within a single task requires the orchestrator to explicitly compose prior outputs into subsequent prompts
- No benefit from platform-level prompt caching across turns (though subscription CLIs typically don't expose this anyway)

### Option C: API-Based Client (Direct HTTP Calls)

**Description:**
Bypass the platform CLI entirely. Call the Anthropic/OpenAI HTTP API directly from Python using an async HTTP client. Manage authentication, rate limiting, and response parsing in-process.

**Pros:**
- Full control over request parameters (temperature, max_tokens, system prompts)
- No dependency on CLI binary installation or version
- Can implement streaming, retry, and backoff logic natively
- Access to features not exposed by CLI flags (function calling schemas, tool use, etc.)

**Cons:**
- Requires API keys and per-token billing; incompatible with subscription-based CLI access (Claude Max, Cursor Pro) which most individual developers use
- Must reimplement file editing, tool use, permission handling, and safety guardrails that the CLI provides for free
- Loses platform-native agent features (Claude Code's file editing, Cursor's codebase indexing)
- Significant implementation complexity for parity with what `claude -p` gives out of the box
- Testing requires mocking HTTP responses rather than using a simple `StubAdapter`
- Not portable across platforms: each API has different schemas, auth, and capabilities

## Decision Drivers

- **Crash Safety:** Process-kill resilience, atomic writes, no corrupted state after SIGKILL
- **Determinism:** Same inputs produce the same execution path through the FSM
- **Testability:** `StubAdapter` support for deterministic, offline testing
- **Platform Portability:** Must work across Claude Code, Cursor, and Inline adapters
- **LLM Cost Efficiency:** Minimize subscription/API calls; predictable token usage per call
- **Stateless Reproducibility:** Fresh subprocess per adapter call; no hidden mutable state
- **Asyncio-Friendliness:** No blocking the event loop; all I/O via `async`/`await`

## Architecture Drivers Comparison

| Architecture Driver        | Option A: Persistent Sessions | Option B: Fresh Subprocess | Option C: API Client | Notes |
|----------------------------|-------------------------------|----------------------------|----------------------|-------|
| **Crash Safety**           | ⭐⭐                          | ⭐⭐⭐⭐⭐                    | ⭐⭐⭐⭐               | A leaves orphan sessions on SIGKILL; B is stateless so nothing to corrupt; C can retry but must handle partial responses |
| **Determinism**            | ⭐⭐                          | ⭐⭐⭐⭐⭐                    | ⭐⭐⭐⭐               | A accumulates hidden state across turns; B is fully prompt-determined; C is prompt-determined but retry/backoff adds variance |
| **Testability**            | ⭐⭐                          | ⭐⭐⭐⭐⭐                    | ⭐⭐⭐                 | A requires session simulation in stubs; B works with trivial StubAdapter; C requires HTTP mocking |
| **Platform Portability**   | ⭐                            | ⭐⭐⭐⭐⭐                    | ⭐⭐                  | A depends on `--continue` which not all CLIs support; B only requires `-p` flag; C requires per-platform API integration |
| **LLM Cost Efficiency**    | ⭐⭐⭐⭐                       | ⭐⭐⭐                       | ⭐⭐⭐⭐⭐              | A reuses cached context (fewer re-sent tokens); B re-sends full prompt each time; C has finest-grained control over tokens |
| **Asyncio Compatibility**  | ⭐⭐⭐                         | ⭐⭐⭐⭐⭐                    | ⭐⭐⭐⭐⭐              | A must manage long-lived session handles; B is fire-and-forget subprocess; C is native async HTTP |
| **Complexity**             | ⭐⭐                          | ⭐⭐⭐⭐⭐                    | ⭐⭐                  | A adds session lifecycle management; B is the simplest model; C requires reimplementing CLI features |
| **Subscription Access**    | ⭐⭐⭐⭐                       | ⭐⭐⭐⭐⭐                    | ⭐                   | A and B work with subscription CLIs; C requires API keys and per-token billing |

## Decision Outcome

**Chosen Option:** Option B: Fresh Subprocess per Invocation

**Rationale:**
The stateless subprocess model scored highest on the drivers that matter most for AutoDev: crash safety, determinism, testability, and platform portability. The only driver where it loses is LLM cost efficiency (re-sending full prompts each time), but this trade-off is acceptable because:

1. Subscription-based CLI access (Claude Max, Cursor Pro) is the primary deployment target, where per-call cost is not token-metered.
2. The orchestrator already composes focused, bounded prompts per agent step, keeping prompt sizes manageable.
3. The cost of debugging non-deterministic session corruption far exceeds the cost of slightly larger prompts.

**Key Factors:**
- Crash recovery becomes trivial: the orchestrator simply re-reads the ledger and re-dispatches from the last committed entry. No session cleanup needed.
- The `StubAdapter` pattern enables the entire test suite (70+ test files) to run offline in under 10 seconds with zero LLM calls.
- Platform portability is a hard requirement: Cursor's agent mode has no `--continue` equivalent, and the inline adapter is a pure Python in-process call.

## Consequences

### Positive Consequences
- The orchestrator's FSM is fully decoupled from platform session semantics. Adding a new adapter (e.g., for a future Windsurf CLI) requires only implementing `execute()` with a subprocess spawn.
- The `parallel()` method on `PlatformAdapter` trivially parallelizes N independent calls under a semaphore, with no session-affinity constraints.
- Test suites are fast, deterministic, and require no network access. The `StubAdapter` handles FIFO responses, callable handlers, and default fallbacks.
- Crash recovery relies solely on the append-only ledger (ADR-002), creating a single source of truth for all state.

### Negative Consequences / Trade-offs
- Every agent invocation must include its full context in the prompt. For multi-turn tasks, this means the orchestrator must explicitly compose prior outputs, evidence, and lessons into the prompt.
- No benefit from platform-level prompt caching. If the same preamble is sent across multiple calls within a task, the tokens are re-processed each time.
- Subprocess startup latency (~100-300ms per invocation) adds overhead compared to a persistent connection. This is mitigated by the `parallel()` method running multiple subprocesses concurrently.

### Neutral / Unknown Consequences
- If platform CLIs introduce prompt-caching features accessible without `--continue` (e.g., via a cached system-prompt flag), the stateless model could gain cost efficiency without changing the adapter contract.
- The `--output-format json` parsing currently extracts only the final `result` field. Future stream-json parsing could provide richer tool-call data without changing the stateless model.

## Implementation Notes

**Files Affected:**
- `src/adapters/base.py` - Defines the `PlatformAdapter` ABC with `execute()` and `parallel()` contracts
- `src/adapters/claude_code.py` - `ClaudeCodeAdapter._build_command()` deliberately omits `--continue`; `execute()` spawns subprocess via `asyncio.create_subprocess_exec`
- `src/adapters/types.py` - `AgentInvocation` and `AgentResult` Pydantic models with `extra="forbid"` at the boundary
- `tests/stub_adapter.py` - `StubAdapter` implements the same contract with zero subprocesses for testing

**Ledger/State Implications:**
- None directly. The stateless model means the adapter has no state to persist. All continuity lives in the ledger (ADR-002).

**General Guidance:**
- Never add `--continue` or `--session-id` to `_build_command` without revisiting this ADR
- All platform-specific context (prior outputs, evidence) must be composed into `AgentInvocation.prompt` by the orchestrator, never by the adapter
- Timeout handling is per-invocation (`inv.timeout_s`); there is no session-level timeout to manage

## Evidence from Codebase

**Source References:**
- `src/adapters/base.py:12-18` - Docstring: "Every call is stateless -- continuity lives in autodev state files, not in the LLM session"
- `src/adapters/claude_code.py:53` - Comment: "We deliberately do NOT pass `--continue`; every call is fresh"
- `src/adapters/claude_code.py:65-160` - `execute()` spawns a fresh `asyncio.create_subprocess_exec` per call, parses JSON stdout, computes git diff
- `src/adapters/base.py:33-54` - `parallel()` runs N independent `execute()` calls under a semaphore with `asyncio.gather`
- `src/adapters/types.py:27-39` - `AgentInvocation` has `extra="forbid"`, enforcing strict boundaries

**Test Coverage:**
- `tests/stub_adapter.py` - `StubAdapter` proves the contract works without subprocesses (FIFO, callable, and default response modes)
- `tests/test_adapter_base.py` - Tests `parallel()` concurrency enforcement, order preservation, exception propagation, and edge cases (empty list, serial mode)
- `tests/test_adapter_claude.py` - Tests `ClaudeCodeAdapter._build_command()` flag construction and `execute()` error paths

**Property-Based Tests (Hypothesis):**
- N/A for this decision (adapter tests use scenario-based assertions)

## Related Design Documents

- [adapters.md](../../docs/design_documentation/adapters.md) - Full adapter subsystem design including the subprocess contract
- [architecture.md](../../docs/architecture.md) - System-level view showing adapters as the boundary between orchestrator and LLM platforms

## Monitoring and Review

- [ ] Review date: 2026-10-17
- [ ] Success criteria: All integration tests pass with StubAdapter; no orphaned session bugs reported; new adapters (Cursor, inline) implement the contract without session management
- [ ] Metrics to track: Adapter invocation latency (subprocess startup overhead); prompt size distribution across agent roles; test suite execution time

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-04-17 | Mohamed Ameen | Initial ADR created |
