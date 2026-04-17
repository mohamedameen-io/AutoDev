# ADR-NNN: [Short Descriptive Title]

**Status:** [Proposed | Accepted | Deprecated | Superseded]
**Date:** YYYY-MM-DD
**Deciders:** [List of decision makers]
**Tags:** [tag1, tag2, tag3]
**Related ADRs:** [ADR-NNN, ADR-NNN] (if applicable)

## Context

[Describe the issue or situation that motivates this decision. What is the problem we're trying to solve? What are the constraints or requirements that drive this decision? Reference the specific AutoDev subsystem(s) affected: adapters, tournament engine, orchestrator, state, knowledge, guardrails, QA gates, plugins.]

## Options Considered

### Option 1: [Name of Option]

**Description:**
[Brief description of this option]

**Pros:**
- [Advantage 1]
- [Advantage 2]

**Cons:**
- [Disadvantage 1]
- [Disadvantage 2]

### Option 2: [Name of Option]

**Description:**
[Brief description of this option]

**Pros:**
- [Advantage 1]
- [Advantage 2]

**Cons:**
- [Disadvantage 1]
- [Disadvantage 2]

### Option 3: [Name of Option] (if applicable)

**Description:**
[Brief description of this option]

**Pros:**
- [Advantage 1]
- [Advantage 2]

**Cons:**
- [Disadvantage 1]
- [Disadvantage 2]

## Decision Drivers

[Identify which of the following drivers matter most for this decision. Delete any that are irrelevant and add custom ones as needed.]

- **Crash Safety:** Process-kill resilience, atomic writes, no corrupted state after SIGKILL
- **LLM Cost Efficiency:** Minimize subscription/API calls; batch where possible
- **Stateless Reproducibility:** Fresh subprocess per adapter call; no hidden mutable state
- **Pydantic-Boundary Strictness:** `extra="forbid"` at every public boundary; fail fast on bad data
- **Asyncio-Friendliness:** No blocking the event loop; all I/O via `async`/`await`
- **Platform Portability:** Must work across Claude Code, Cursor, and Inline adapters
- **Testability:** `StubAdapter` support for deterministic, offline testing
- **Determinism:** Same inputs produce the same execution path through the FSM

## Architecture Drivers Comparison

[Compare each option against the decision drivers identified above. Use the star rating scale below.]

| Architecture Driver        | Option 1: [Name] | Option 2: [Name] | Option 3: [Name] | Notes |
|----------------------------|-------------------|-------------------|-------------------|-------|
| **Crash Safety**           | [Rating]          | [Rating]          | [Rating]          | [e.g., survives SIGKILL mid-write?] |
| **LLM Cost**               | [Rating]          | [Rating]          | [Rating]          | [e.g., number of extra API calls] |
| **Determinism**            | [Rating]          | [Rating]          | [Rating]          | [e.g., same FSM path on replay?] |
| **Testability**            | [Rating]          | [Rating]          | [Rating]          | [e.g., works with StubAdapter?] |
| **Asyncio Compatibility**  | [Rating]          | [Rating]          | [Rating]          | [e.g., any blocking calls?] |
| **Platform Portability**   | [Rating]          | [Rating]          | [Rating]          | [e.g., adapter-agnostic?] |
| **Complexity**             | [Rating]          | [Rating]          | [Rating]          | [e.g., new abstractions introduced?] |
| **[Custom Driver]**        | [Rating]          | [Rating]          | [Rating]          | [Brief explanation if needed] |

**Rating Scale:**
- ⭐⭐⭐⭐⭐ (5/5) - Excellent
- ⭐⭐⭐⭐ (4/5) - Good
- ⭐⭐⭐ (3/5) - Average
- ⭐⭐ (2/5) - Below Average
- ⭐ (1/5) - Poor

## Decision Outcome

**Chosen Option:** [Option X: Name]

**Rationale:**
[Explain why this option was chosen. Reference the architecture drivers comparison and highlight which drivers were most important in making this decision. Address any trade-offs that were accepted.]

**Key Factors:**
- [Factor 1 that influenced the decision]
- [Factor 2 that influenced the decision]
- [Factor 3 that influenced the decision]

## Consequences

### Positive Consequences
- [What becomes easier or better because of this decision?]
- [What benefits do we gain?]

### Negative Consequences / Trade-offs
- [What becomes more difficult?]
- [What limitations or constraints do we accept?]
- [What risks are introduced?]

### Neutral / Unknown Consequences
- [What might change but impact is unclear?]
- [What needs to be monitored?]

## Implementation Notes

**Files Affected:**
- `src/[subsystem]/[file].py` - [what changes and why]
- `src/[subsystem]/[file].py` - [what changes and why]
- `tests/[subsystem]/[test_file].py` - [new or modified tests]

**Ledger/State Implications:**
- [New operation types added to the append-only JSONL ledger, if any]
- [Schema changes to `LedgerEntry` or related Pydantic models, if any]
- [CAS (compare-and-swap) considerations, if any]
- [None, if this decision does not affect state]

**General Guidance:**
- [Key considerations during implementation]
- [Potential pitfalls to avoid]

## Evidence from Codebase

**Source References:**
- `src/[subsystem]/[file].py:[line range]` - [what this code shows]
- `src/[subsystem]/[file].py:[line range]` - [what this code shows]

**Test Coverage:**
- `tests/[subsystem]/[test_file].py` - [what this test validates about the decision]
- `tests/[subsystem]/[test_file].py` - [what this test validates about the decision]

**Property-Based Tests (Hypothesis):**
- `tests/[subsystem]/[test_file].py::[test_name]` - [property being verified, if applicable]
- [N/A if no property-based tests are relevant]

## Related Design Documents

- [adapters.md](adapters.md) - [relevance to this decision, if any]
- [agents.md](agents.md) - [relevance to this decision, if any]
- [tournaments.md](tournaments.md) - [relevance to this decision, if any]
- [cost.md](cost.md) - [relevance to this decision, if any]
- [semver.md](semver.md) - [relevance to this decision, if any]
- [architecture.md](../architecture.md) - [relevance to this decision, if any]
- [Delete rows that are not relevant to this ADR]

## Monitoring and Review

[How will we monitor the success of this decision? When should this ADR be reviewed?]

- [ ] Review date: [YYYY-MM-DD]
- [ ] Success criteria: [How will we know this decision was correct?]
- [ ] Metrics to track: [What should we measure?]

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| YYYY-MM-DD | [Name] | Initial ADR created |
| YYYY-MM-DD | [Name] | [Description of changes] |

---

## Usage Instructions

1. Copy this template to create a new ADR: `adr-NNN-short-title.md` (e.g., `adr-001-append-only-ledger.md`)
2. Place the new file in `src/design_decision/`
3. Number ADRs sequentially with three digits (ADR-001, ADR-002, etc.)
4. Replace all placeholder text (text in square brackets `[...]`) with actual content
5. Remove sections, table rows, or list items that are not applicable
6. Add or remove architecture drivers based on what is relevant for your decision
7. Update the status as the decision progresses:
   - **Proposed:** Decision is being discussed
   - **Accepted:** Decision has been made and approved
   - **Deprecated:** Decision is no longer valid but kept for historical reference
   - **Superseded:** Decision has been replaced by another ADR (link to the new ADR)
8. Keep the ADR updated as implementation progresses and new information becomes available
