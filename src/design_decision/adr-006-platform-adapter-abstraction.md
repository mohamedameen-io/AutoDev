# ADR-006: Platform Adapter Abstraction via Strategy Pattern ABC

**Status:** Accepted
**Date:** 2026-04-17
**Deciders:** Mohamed Ameen
**Tags:** adapters, platform-portability, strategy-pattern, abc, testability
**Related ADRs:** ADR-005 (Protocol-Based Plugin System), ADR-007 (Git Worktree Isolation)

## Context

AutoDev orchestrates multi-agent coding workflows by spawning LLM-backed coding assistants (Claude Code, Cursor) as subprocesses. The orchestrator must issue agent invocations, collect results, run parallel judge calls during tournaments, and initialize platform-native workspace files -- all without knowing which platform CLI is installed on the user's machine.

Three platforms are supported:

1. **Claude Code** (`claude -p "<prompt>" --output-format json`) -- subprocess-based, supports `--allowed-tools` and `--model` flags.
2. **Cursor** (`cursor agent "<prompt>" --print --output-format json`) -- subprocess-based, lacks `--allowed-tools` equivalent; relies on `.cursor/rules/*.mdc` prompt-level constraints.
3. **Inline mode** -- file-based adapter for when AutoDev runs inside an active Claude Code or Cursor session. Instead of spawning a subprocess, it writes a delegation file to `.autodev/delegations/` and suspends via `DelegationPendingSignal`. The host agent reads the delegation, executes the task, writes a response file, and runs `autodev resume`.

Each platform has different CLI flags, output formats, agent file structures (`.claude/agents/*.md` vs `.cursor/rules/*.mdc`), error patterns (rate limits, auth failures), and concurrency characteristics (inline mode is inherently sequential). The orchestrator, tournament engine, and QA pipeline must remain platform-agnostic.

## Options Considered

### Option A: Direct Subprocess Calls Scattered Through Orchestrator

**Description:**
The orchestrator, tournament engine, and QA pipeline each contain platform-specific `if platform == "claude_code": ... elif platform == "cursor": ...` branches with inline subprocess invocations. No abstraction layer.

**Pros:**
- Zero abstraction overhead: no adapter classes, no inheritance, no indirection
- Each call site can fine-tune platform-specific flags without going through an abstraction
- Fastest path to a working prototype

**Cons:**
- **Shotgun surgery**: every new platform requires changes to every call site across the orchestrator, tournament engine, and QA pipeline
- Untestable without real CLI binaries: no way to inject a stub or mock at the adapter boundary
- Duplicated error handling: rate-limit retry, timeout, output parsing logic repeated everywhere
- Platform-specific quirks leak into business logic (e.g., Cursor's 429 fallback-to-auto logic mixed with tournament scoring)
- Inline mode would require a third branch in every call site, with fundamentally different semantics (file I/O vs subprocess)

### Option B: Strategy Pattern with ABC (Current Choice)

**Description:**
A single `PlatformAdapter` ABC defines four methods: `init_workspace()`, `execute()`, `parallel()`, and `healthcheck()`. Three concrete subclasses implement the ABC: `ClaudeCodeAdapter`, `CursorAdapter`, and `InlineAdapter`. The orchestrator receives a `PlatformAdapter` instance (resolved via `detect_platform()` / `get_adapter()`) and never calls platform-specific code directly.

The `parallel()` method has a default implementation on the ABC using `asyncio.Semaphore` + `asyncio.gather`, which `ClaudeCodeAdapter` and `CursorAdapter` inherit. `InlineAdapter` overrides it to raise `NotImplementedError` (inline mode is inherently sequential).

Auto-detection (`detect.py`) follows a precedence chain: explicit CLI flag > `AUTODEV_PLATFORM` env var > probe `claude --version` > probe `cursor --version` > error.

**Pros:**
- **Single orchestrator codebase**: the orchestrator, tournament engine, and QA pipeline call `adapter.execute()` with no platform branching
- **Testable**: a `_DummyAdapter` (or any test double) can be injected, enabling deterministic offline tests with zero real CLI calls
- The `parallel()` default implementation with semaphore-based concurrency limiting is reused by Claude Code and Cursor adapters for free
- Auto-detection with healthcheck probing means users don't need to manually configure their platform
- `InlineAdapter` cleanly diverges (file I/O instead of subprocess) without polluting the contract: `execute()` raises `DelegationPendingSignal`, `parallel()` raises `NotImplementedError`
- Clear extension path for new platforms: implement 4 methods, add to `_make_adapter()`, done

**Cons:**
- ABC requires tight coupling: concrete adapters must import `PlatformAdapter`, `AgentInvocation`, `AgentResult`, `AgentSpec` from `autodev`. Unlike the plugin system (ADR-005), adapters are internal-only, so this coupling is acceptable
- Lowest-common-denominator API: features available on only one platform (e.g., Claude Code's `--allowed-tools`) must be handled inside the adapter, not surfaced in the contract
- `InlineAdapter` bends the contract: `execute()` never returns normally (always raises), `parallel()` is unsupported. The ABC's type signature promises `AgentResult` but inline mode always raises. This requires the orchestrator to catch `DelegationPendingSignal` explicitly
- Auto-detection probes CLIs at startup, adding latency. Mitigated by short-circuit when explicit preference is set

### Option C: Separate Orchestrator Per Platform

**Description:**
Each platform gets its own orchestrator: `ClaudeCodeOrchestrator`, `CursorOrchestrator`, `InlineOrchestrator`. Each contains the full FSM, tournament engine, and QA pipeline, tailored to its platform's capabilities.

**Pros:**
- Each orchestrator can exploit platform-specific features without abstraction constraints
- No lowest-common-denominator API: Claude Code orchestrator can use `--allowed-tools`, Cursor orchestrator can implement cursor-specific retry logic
- No need for adapter abstraction at all

**Cons:**
- **Massive code duplication**: the FSM, tournament engine, QA pipeline, state management, and error handling are duplicated across orchestrators. At ~11K LOC, this would roughly triple the codebase
- Bug fixes must be applied N times (once per platform orchestrator)
- Feature additions (new tournament algorithm, new QA gate) require N implementations
- Testing surface area multiplies: every feature must be tested against every platform orchestrator
- Inline mode is fundamentally different (async file-based ping-pong vs synchronous subprocess), making the duplication even worse

## Decision Drivers

- **Platform Portability:** The defining driver. The orchestrator, tournament engine, and QA pipeline must work identically across Claude Code, Cursor, and inline mode.
- **Testability:** Tests must run without real CLI binaries. `_DummyAdapter` in tests provides deterministic, offline execution with measurable concurrency.
- **Asyncio-Friendliness:** All adapter methods are async. `parallel()` uses `asyncio.Semaphore` for bounded concurrency without blocking the event loop.
- **Stateless Reproducibility:** Each `execute()` call is a fresh subprocess (Claude Code, Cursor) or a fresh delegation file (inline). No hidden session state in the adapter.
- **Pydantic-Boundary Strictness:** `AgentInvocation`, `AgentResult`, `AgentSpec`, and `ToolCall` are all Pydantic models with `extra="forbid"`. Malformed data fails fast at the adapter boundary.

## Architecture Drivers Comparison

| Architecture Driver          | Option A: Direct Calls | Option B: Strategy ABC | Option C: Separate Orchestrators | Notes |
|------------------------------|------------------------|------------------------|----------------------------------|-------|
| **Platform Portability**     | ⭐ (1/5)              | ⭐⭐⭐⭐⭐ (5/5)      | ⭐⭐⭐⭐ (4/5)                  | A scatters platform logic; C duplicates it per platform; B isolates it |
| **Testability**              | ⭐ (1/5)              | ⭐⭐⭐⭐⭐ (5/5)      | ⭐⭐⭐ (3/5)                    | A requires real CLIs; B injects stubs; C needs stubs per orchestrator |
| **Crash Safety**             | ⭐⭐ (2/5)            | ⭐⭐⭐⭐ (4/5)        | ⭐⭐⭐⭐ (4/5)                  | B centralizes timeout/error handling in adapter; A scatters it |
| **LLM Cost Efficiency**      | ⭐⭐⭐ (3/5)          | ⭐⭐⭐⭐ (4/5)        | ⭐⭐⭐⭐⭐ (5/5)                | C can exploit platform-specific batching; B's LCD API may miss optimizations |
| **Stateless Reproducibility**| ⭐⭐⭐ (3/5)          | ⭐⭐⭐⭐⭐ (5/5)      | ⭐⭐⭐⭐ (4/5)                  | B enforces statelessness in the contract; A/C may accumulate hidden state |
| **Asyncio Compatibility**    | ⭐⭐⭐ (3/5)          | ⭐⭐⭐⭐⭐ (5/5)      | ⭐⭐⭐⭐ (4/5)                  | B's parallel() uses asyncio.gather + Semaphore; A may mix sync/async |
| **Complexity**               | ⭐⭐⭐⭐⭐ (5/5)      | ⭐⭐⭐⭐ (4/5)        | ⭐ (1/5)                        | A has zero abstraction; C triples the codebase |
| **Extensibility**            | ⭐ (1/5)              | ⭐⭐⭐⭐⭐ (5/5)      | ⭐⭐ (2/5)                      | B: implement 4 methods for a new platform; C: clone an entire orchestrator |

**Rating Scale:**
- ⭐⭐⭐⭐⭐ (5/5) - Excellent
- ⭐⭐⭐⭐ (4/5) - Good
- ⭐⭐⭐ (3/5) - Average
- ⭐⭐ (2/5) - Below Average
- ⭐ (1/5) - Poor

## Decision Outcome

**Chosen Option:** Option B: Strategy Pattern with ABC

**Rationale:**
The Strategy pattern with ABC provides the optimal balance between platform portability, testability, and extensibility. The orchestrator (~11K LOC) contains the FSM, tournament engine, QA pipeline, and state management -- all of which are platform-agnostic business logic. Duplicating this logic per platform (Option C) would be untenable. Scattering platform branches through it (Option A) would create a maintenance nightmare as platforms are added.

The key insight is that the adapter surface is deliberately small: 4 methods that capture the lowest common denominator across all platforms. Platform-specific features (Claude Code's `--allowed-tools`, Cursor's 429 fallback-to-auto, inline mode's delegation files) are handled inside the concrete adapter, invisible to the orchestrator.

The trade-off with `InlineAdapter` bending the contract (execute never returns, parallel is unsupported) is accepted because inline mode is architecturally distinct: it operates in a suspend/resume model rather than synchronous subprocess execution. The orchestrator explicitly catches `DelegationPendingSignal`, which is documented and tested.

**Key Factors:**
- A single orchestrator codebase eliminates the maintenance burden of N-way duplication
- `_DummyAdapter` in tests enables offline, deterministic testing of the entire orchestrator pipeline
- The `parallel()` default implementation with `asyncio.Semaphore` is correct-by-construction and reused by all subprocess-based adapters
- Auto-detection with healthcheck probing provides zero-config user experience
- The 4-method contract is small enough that adding a new platform (e.g., Windsurf, Aider) is a bounded task

## Consequences

### Positive Consequences
- The orchestrator, tournament engine, and QA pipeline are completely platform-agnostic. No `if platform ==` branches in business logic
- Testing the full pipeline requires zero real CLI installations -- `_DummyAdapter` simulates arbitrary execution with configurable delay and concurrency
- New platforms can be added by implementing 4 methods and registering in `_make_adapter()`. The existing test suite validates orchestrator behavior automatically
- `parallel()` concurrency limiting (via `asyncio.Semaphore`) is implemented once in the ABC and inherited by all subprocess-based adapters
- Auto-detection with explicit override (CLI flag > env var > probe) satisfies both advanced users and beginners

### Negative Consequences / Trade-offs
- Lowest-common-denominator API means platform-specific features must be hidden inside the adapter. If a future platform has a capability that fundamentally changes the orchestrator's flow (e.g., native multi-agent orchestration), the ABC would need extension
- `InlineAdapter` is a conceptual stretch of the Strategy pattern: `execute()` raises instead of returning, `parallel()` is unsupported. The orchestrator must know to catch `DelegationPendingSignal`, which slightly breaks the substitution principle
- Auto-detection probes CLI binaries at startup. On systems where neither CLI is installed, this adds ~2 seconds of latency before the error message. Mitigated by caching detection results for the session

### Neutral / Unknown Consequences
- Whether the adapter contract needs a `stream_execute()` method for future streaming output support. Currently all adapters buffer the full subprocess output. Streaming would require a generator-based API extension
- Whether `InlineAdapter.parallel()` should eventually be supported via sequential delegation or remain unsupported

## Implementation Notes

**Files Affected:**
- `src/adapters/base.py` -- `PlatformAdapter` ABC with 4 abstract methods (`init_workspace`, `execute`, `healthcheck`) plus one concrete method (`parallel` with semaphore)
- `src/adapters/claude_code.py` -- `ClaudeCodeAdapter` implementation: spawns `claude -p` subprocess, parses JSON output, detects file changes via git diff
- `src/adapters/cursor.py` -- `CursorAdapter` implementation: spawns `cursor agent --print`, handles 429 rate-limit fallback to `auto` model
- `src/adapters/inline.py` -- `InlineAdapter` implementation: file-based delegation/response, `DelegationPendingSignal` on execute, `NotImplementedError` on parallel
- `src/adapters/detect.py` -- `detect_platform()` auto-detection and `get_adapter()` factory function
- `src/adapters/types.py` -- Pydantic models: `AgentInvocation`, `AgentResult`, `AgentSpec`, `ToolCall`, `StreamEvent` (all with `extra="forbid"`)
- `tests/test_adapter_base.py` -- Tests for ABC: parallel concurrency enforcement, order preservation, exception propagation, edge cases
- `tests/test_adapter_detect.py` -- Tests for auto-detection precedence: preferred platform, env var, fallback chain, invalid values
- `tests/test_adapter_inline.py` -- Tests for InlineAdapter: delegation file writing, response collection, error handling, healthcheck

**Ledger/State Implications:**
- None. Adapter selection is transient (per-session). The chosen platform name may be logged in ledger entries for diagnostics, but the adapter itself writes nothing to the ledger.

**General Guidance:**
- When adding a new adapter, implement all 4 abstract methods, add to `_make_adapter()` in `detect.py`, add the platform name to `PlatformName` Literal type, and add healthcheck probe logic to `detect_platform()`
- Keep `AgentInvocation` and `AgentResult` as the sole contract types. Do not leak platform-specific types (e.g., `ClaudeJsonOutput`) beyond the adapter boundary
- If a future adapter needs to override `parallel()`, it should still respect `max_concurrent` semantics so the orchestrator's cost guardrails remain valid

## Evidence from Codebase

**Source References:**
- `src/adapters/base.py:12-58` -- `PlatformAdapter` ABC definition with all 4 methods, including the `parallel()` default implementation using `asyncio.Semaphore` and `asyncio.gather`
- `src/adapters/detect.py:23-72` -- `detect_platform()` precedence chain: preferred > env var > probe claude > probe cursor > error
- `src/adapters/detect.py:75-102` -- `get_adapter()` factory and `_make_adapter()` with lazy import of `InlineAdapter` to avoid circular dependencies
- `src/adapters/inline.py:33-40` -- `InlineAdapter` class docstring explaining the suspend/resume model via `DelegationPendingSignal`
- `src/adapters/inline.py:93-111` -- `execute()` implementation: writes delegation file, raises `DelegationPendingSignal`, never returns normally
- `src/adapters/inline.py:113-120` -- `parallel()` override raising `NotImplementedError` with explanation
- `src/adapters/types.py:27-55` -- `AgentInvocation` and `AgentResult` Pydantic models with `extra="forbid"`

**Test Coverage:**
- `tests/test_adapter_base.py::test_parallel_enforces_max_concurrent` -- Validates that `_DummyAdapter.parallel()` never exceeds `max_concurrent` in-flight tasks
- `tests/test_adapter_base.py::test_parallel_preserves_order` -- Validates that results are returned in the same order as invocations
- `tests/test_adapter_base.py::test_parallel_propagates_exceptions` -- Validates that exceptions from `execute()` propagate through `parallel()`
- `tests/test_adapter_base.py::test_abstract_cannot_instantiate` -- Validates that `PlatformAdapter()` raises `TypeError`
- `tests/test_adapter_detect.py::test_auto_prefers_claude` -- Validates Claude Code is preferred when both CLIs are available
- `tests/test_adapter_detect.py::test_auto_falls_back_to_cursor` -- Validates Cursor fallback when Claude Code is unavailable
- `tests/test_adapter_inline.py::test_execute_writes_delegation_and_raises_signal` -- Validates the inline suspend/resume model

**Property-Based Tests (Hypothesis):**
- N/A

## Related Design Documents

- [adapters.md](../../docs/design_documentation/adapters.md) -- Comprehensive specification of the adapter contract, Claude Code and Cursor invocation patterns, output parsing, auto-detection logic, and instructions for adding new adapters
- [agents.md](../../docs/design_documentation/agents.md) -- Agent definitions that flow through the adapter via `AgentInvocation`; tool map per role
- [tournaments.md](../../docs/design_documentation/tournaments.md) -- Tournament engine uses `adapter.parallel()` for concurrent judge invocations and `adapter.execute()` for critic/architect/synthesizer calls
- [cost.md](../../docs/design_documentation/cost.md) -- Cost guardrails that depend on `parallel(max_concurrent=N)` to cap concurrent subprocess spawns

## Monitoring and Review

- [ ] Review date: 2026-10-17
- [ ] Success criteria: All three adapters (Claude Code, Cursor, Inline) pass the full test suite. At least one community-contributed adapter (e.g., Windsurf, Aider) validates the extensibility claim
- [ ] Metrics to track: Adapter healthcheck failure rate, auto-detection latency, platform distribution across user sessions

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-04-17 | Mohamed Ameen | Initial ADR created |
