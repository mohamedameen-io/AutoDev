# ADR-009: Pydantic v2 Strict Validation with `extra="forbid"`

**Status:** Accepted
**Date:** 2026-04-17
**Deciders:** Mohamed Ameen
**Tags:** validation, pydantic, schemas, type-safety, boundaries
**Related ADRs:** ADR-008 (FSM payloads are validated by these schemas), ADR-010 (tournament config uses the same strict pattern)

## Context

AutoDev has over 25 Pydantic models spanning four subsystems: configuration (`config/schema.py`), adapter types (`adapters/types.py`, `adapters/inline_types.py`), state and evidence schemas (`state/schemas.py`, `state/ledger.py`), and orchestrator envelopes (`orchestrator/delegation_envelope.py`). These models serve as the boundary contracts at every data exchange point:

1. **Config loading**: User-authored `config.json` is deserialized into `AutodevConfig`
2. **LLM output parsing**: Agent results are parsed into evidence models (`CoderEvidence`, `ReviewEvidence`, etc.)
3. **Ledger persistence**: Every state mutation is serialized as a `LedgerEntry` to the append-only JSONL log
4. **Adapter I/O**: `AgentInvocation` flows into adapters; `AgentResult` flows out
5. **Inline delegation**: `InlineSuspendState` and `InlineResponseFile` are serialized to/from JSON files for cross-process handoff

The key question is: how strictly should these boundaries validate data? A typo in a config key (e.g., `qa_rtry_limit` instead of `qa_retry_limit`) should be caught immediately, not silently ignored. An unexpected field in LLM output should fail fast rather than propagate corrupt data through the pipeline. Schema drift between versions (adding a field in one model but forgetting to update the consumer) should surface at deserialization time.

## Options Considered

### Option A: Dataclasses with Manual Validation

**Description:**
Use Python's built-in `@dataclass` decorator for all data structures. Add manual validation in `__post_init__()` methods or factory functions. JSON serialization/deserialization via `json.loads()` + manual dict-to-dataclass conversion.

**Pros:**
- Zero external dependencies
- Familiar to all Python developers
- Full control over validation logic

**Cons:**
- No built-in JSON Schema generation
- Manual serialization code for every model (error-prone, verbose)
- No `extra="forbid"` equivalent without writing custom `__init__` wrappers that inspect kwargs
- No discriminated unions for the evidence hierarchy
- Performance: manual validation is slower than Pydantic v2's Rust core
- No `.model_dump(mode="json")` for safe JSON serialization of Path, datetime, etc.
- Typos in dict keys during deserialization silently become `None` or missing attributes

### Option B: Pydantic v1 with `orm_mode`

**Description:**
Use Pydantic v1 (the pre-2.0 API) with `class Config: extra = "forbid"` on all models. Use `orm_mode = True` where needed.

**Pros:**
- Proven library, widely used
- Extra field rejection via `extra = "forbid"` in `class Config`
- Discriminated unions via `__root__` or `discriminator` field
- Good error messages

**Cons:**
- Pydantic v1 is in maintenance-only mode; v2 is the actively developed version
- Significantly slower than v2: v2's Rust-based validator is 5-50x faster depending on model complexity
- `class Config` is deprecated in favor of `model_config = ConfigDict(...)`
- `.dict()` and `.json()` are deprecated in favor of `.model_dump()` and `.model_dump_json()`
- `orm_mode` conflates ORM concerns with schema validation
- v1 `discriminator` support is less robust (no native `Field(discriminator=...)`)

### Option C: Pydantic v2 with `ConfigDict(extra="forbid")` on Every Model (Current Choice)

**Description:**
Every Pydantic model in the codebase uses `model_config = ConfigDict(extra="forbid")`. This is applied uniformly across all 25+ models in `config/schema.py`, `adapters/types.py`, `adapters/inline_types.py`, `state/schemas.py`, `state/ledger.py`, and `orchestrator/delegation_envelope.py`. Pydantic v2's Rust-backed validator handles deserialization, type coercion, and validation. The evidence hierarchy uses `Field(discriminator="kind")` for zero-ambiguity parsing.

**Pros:**
- Catches typos in config keys immediately at load time (e.g., `unexpected_field` in config.json raises `ConfigError`)
- Catches unexpected fields in LLM output at evidence construction time
- Catches schema drift: if a producer adds a field that the consumer's model doesn't know about, deserialization fails
- 5-50x faster than v1 due to Rust core (important for evidence serialization in hot loops)
- Native `.model_dump(mode="json")` handles Path, datetime, Literal, etc. safely
- `Field(discriminator="kind")` provides zero-cost discriminated unions for the 7-variant evidence type
- `model_copy(update={...})` for immutable-style updates (used in envelope retry and invocation metadata injection)
- `arbitrary_types_allowed=True` only where needed (`AgentInvocation`, `AgentResult` with `Path` fields)

**Cons:**
- Pydantic v2 is a heavy dependency (~5MB installed)
- `extra="forbid"` can be overly strict during development: adding a field to a JSON file before updating the model causes crashes
- Must remember to add `model_config = ConfigDict(extra="forbid")` to every new model; there is no codebase-wide enforcement beyond code review
- Pydantic v2 migration from v1 is non-trivial (already completed for AutoDev)

## Decision Drivers

- **Pydantic-Boundary Strictness:** `extra="forbid"` at every public boundary; fail fast on bad data
- **Crash Safety:** Strict validation prevents corrupt state from entering the ledger
- **LLM Cost Efficiency:** Catching bad LLM output early prevents wasted downstream agent calls
- **Stateless Reproducibility:** Validated schemas ensure identical data shapes across serialize/deserialize cycles
- **Testability:** Schemas with strict validation are trivially testable (construct with bad data, assert ValidationError)

## Architecture Drivers Comparison

| Architecture Driver        | Option A: Dataclasses | Option B: Pydantic v1 | Option C: Pydantic v2 (chosen) | Notes |
|----------------------------|----------------------|----------------------|-------------------------------|-------|
| **Pydantic Strictness**    | ⭐ (1/5)             | ⭐⭐⭐⭐ (4/5)          | ⭐⭐⭐⭐⭐ (5/5)                  | Dataclasses have no built-in extra-field rejection; v2 has `ConfigDict(extra="forbid")` |
| **Crash Safety**           | ⭐⭐ (2/5)            | ⭐⭐⭐⭐ (4/5)          | ⭐⭐⭐⭐⭐ (5/5)                  | Strict validation prevents corrupt data from entering the hash-chained ledger |
| **Performance**            | ⭐⭐⭐ (3/5)          | ⭐⭐ (2/5)            | ⭐⭐⭐⭐⭐ (5/5)                  | v2 Rust core is 5-50x faster than v1; matters for evidence serialization in tournament loops |
| **JSON Mode Compatibility**| ⭐ (1/5)             | ⭐⭐⭐ (3/5)           | ⭐⭐⭐⭐⭐ (5/5)                  | v2's `model_dump(mode="json")` handles Path/datetime natively |
| **Testability**            | ⭐⭐ (2/5)            | ⭐⭐⭐⭐ (4/5)          | ⭐⭐⭐⭐⭐ (5/5)                  | `model_validate()` + ValidationError assertions are concise and readable |
| **Developer Experience**   | ⭐⭐ (2/5)            | ⭐⭐⭐ (3/5)           | ⭐⭐⭐⭐⭐ (5/5)                  | Clear error messages naming the unexpected field and valid alternatives |
| **Dependency Weight**      | ⭐⭐⭐⭐⭐ (5/5)       | ⭐⭐⭐ (3/5)           | ⭐⭐⭐ (3/5)                    | Dataclasses are stdlib; Pydantic adds ~5MB |
| **Discriminated Unions**   | ⭐ (1/5)             | ⭐⭐⭐ (3/5)           | ⭐⭐⭐⭐⭐ (5/5)                  | v2's `Field(discriminator="kind")` is native and type-checked |

## Decision Outcome

**Chosen Option:** Option C: Pydantic v2 with `ConfigDict(extra="forbid")` on Every Model

**Rationale:**
The combination of `extra="forbid"` and Pydantic v2's performance makes this the clear winner. AutoDev processes LLM output at every boundary -- config load, agent result parsing, evidence construction, ledger serialization, inline delegation handoff. Each of these is a potential entry point for corrupt or unexpected data. By rejecting unknown fields at every boundary, the system fails fast and loud rather than propagating subtle bugs.

The performance advantage of v2 is material: tournament passes serialize/deserialize evidence models in tight loops (N judges per pass, up to 30 passes), and v2's Rust core handles this without becoming a bottleneck.

The discriminated union for the 7-variant `Evidence` type (`Field(discriminator="kind")`) eliminates the need for manual type-checking dispatch code and ensures round-trip fidelity through the ledger.

**Key Factors:**
- Every model in the codebase already has `ConfigDict(extra="forbid")` -- this is not aspirational but fully implemented across all 25+ models (verified by grep: 22 occurrences of `extra="forbid"` in `src/`)
- The `test_unknown_top_level_field_rejected` test in `test_config_schema.py` and `test_rejects_unknown_fields` tests in `test_inline_types.py` explicitly verify that unknown fields cause errors
- `model_copy(update={...})` is used extensively in the orchestrator for immutable-style envelope and invocation updates (plan retry context, inline metadata injection)

## Consequences

### Positive Consequences
- A typo in `config.json` (e.g., `"qa_rtry_limit": 5`) raises a clear `ConfigError` naming the unexpected field, rather than silently using the default value of 3
- If an LLM agent returns JSON with an unexpected key in a structured response, evidence construction fails immediately rather than storing corrupt data in the ledger
- Schema versioning is explicit: `schema_version: Literal["1.0.0"]` on `AutodevConfig` and `schema_version: Literal["1.0"]` on `InlineSuspendState` mean version changes require code changes, not silent degradation
- The 7-variant evidence discriminated union (`Evidence = Annotated[Union[...], Field(discriminator="kind")]`) provides type-safe dispatch without isinstance chains

### Negative Consequences / Trade-offs
- Adding a new field to any model requires updating the model definition before the field can appear in any JSON input; this can slow rapid prototyping
- `extra="forbid"` is a convention enforced by code review, not by a linter rule; a new model without it would silently accept extra fields until caught in review
- The Pydantic v2 dependency adds ~5MB to the installation; not an issue for a developer tool but worth noting

### Neutral / Unknown Consequences
- If AutoDev ever needs to support forward-compatible schemas (e.g., a newer producer sends fields an older consumer doesn't know about), `extra="forbid"` would need to be relaxed on specific boundaries; this has not been needed yet
- Pydantic v2's `model_config` approach using `ConfigDict` is more verbose than v1's `class Config` but is the stable API going forward

## Implementation Notes

**Files Affected:**
- `src/config/schema.py` - 8 models: `AgentConfig`, `TournamentPhaseConfig`, `TournamentsConfig`, `QAGatesConfig`, `GuardrailsConfig`, `HiveConfig`, `KnowledgeConfig`, `AutodevConfig` -- all with `ConfigDict(extra="forbid")`
- `src/adapters/types.py` - 6 models: `ToolCall`, `AgentInvocation`, `AgentResult`, `AgentSpec`, `StreamEvent` -- all with `ConfigDict(extra="forbid")`; `AgentInvocation` and `AgentResult` additionally use `arbitrary_types_allowed=True` for `Path` fields
- `src/adapters/inline_types.py` - 2 models: `InlineSuspendState`, `InlineResponseFile` -- both with `ConfigDict(extra="forbid")`
- `src/state/schemas.py` - 12 models: `AcceptanceCriterion`, `Task`, `Phase`, `Plan`, `_BaseEvidence` (inherited by 7 evidence variants) -- all with `ConfigDict(extra="forbid")`
- `src/state/ledger.py` - 1 model: `LedgerEntry` -- with `ConfigDict(extra="forbid")`
- `src/orchestrator/delegation_envelope.py` - 1 model: `DelegationEnvelope` -- with `ConfigDict(extra="forbid")`

**Ledger/State Implications:**
- `LedgerEntry` itself uses `extra="forbid"`, so any attempt to append a ledger entry with unexpected fields will fail at serialization time
- The `Plan` model embedded in `init_plan` ledger entries is also strict; schema evolution requires explicit migration
- Evidence models use the `kind` discriminator; adding a new evidence type requires adding it to the `Evidence` union and handling the new `kind` value in consumers

**General Guidance:**
- Every new Pydantic model must include `model_config = ConfigDict(extra="forbid")` as its first line
- Use `model_dump(mode="json")` for serialization, never `dict()` (which doesn't handle Path/datetime)
- Use `model_copy(update={...})` for immutable updates, never direct attribute mutation
- When adding a new evidence variant, add it to both the individual class definition and the `Evidence` discriminated union

## Evidence from Codebase

**Source References:**
- `src/config/schema.py:30,37,46,55,67,87,106,128` - All 8 config models use `ConfigDict(extra="forbid")`
- `src/adapters/types.py:19,30,45,61,73` - All 5 adapter type models use `ConfigDict(extra="forbid")`
- `src/state/schemas.py:43,53,74,85,104` - Plan/Task/Phase/AcceptanceCriterion and _BaseEvidence (inherited by all 7 evidence variants) use `ConfigDict(extra="forbid")`
- `src/state/ledger.py:67` - `LedgerEntry` uses `ConfigDict(extra="forbid")`
- `src/adapters/inline_types.py:43,68` - `InlineSuspendState` and `InlineResponseFile` use `ConfigDict(extra="forbid")`
- `src/orchestrator/delegation_envelope.py:34` - `DelegationEnvelope` uses `ConfigDict(extra="forbid")`
- `src/state/schemas.py:188-199` - `Evidence` discriminated union with `Field(discriminator="kind")` over 7 variants

**Test Coverage:**
- `tests/test_config_schema.py::test_unknown_top_level_field_rejected` - Adds `unexpected_field` to config JSON, asserts `ConfigError` raised on load
- `tests/test_config_schema.py::test_default_config_validates` - Round-trips the default config through `model_dump` + `model_validate`
- `tests/test_adapter_types.py::test_agent_invocation_roundtrip` - Verifies `AgentInvocation` survives `model_dump(mode="json")` + `model_validate`
- `tests/test_adapter_types.py` (line 135) - Tests that passing `unexpected_field="oops"` to a model raises ValidationError
- `tests/test_inline_types.py::test_rejects_unknown_fields` - Two test methods verifying both `InlineSuspendState` and `InlineResponseFile` reject unknown fields

**Property-Based Tests (Hypothesis):**
- N/A directly for schema validation, though Hypothesis is used extensively in the tournament subsystem (see ADR-010) which relies on these validated schemas

## Related Design Documents

- [adapters.md](../../docs/design_documentation/adapters.md) - `AgentInvocation` and `AgentResult` are the primary adapter boundary types; strict validation ensures adapter implementations cannot return unexpected fields
- [agents.md](../../docs/design_documentation/agents.md) - Agent prompts produce output parsed into evidence models; strict schemas catch malformed output early
- [cost.md](../../docs/design_documentation/cost.md) - Config schema strictness prevents silent misconfiguration of cost-affecting settings (tournament rounds, judge counts)

## Monitoring and Review

- [ ] Review date: 2026-10-17
- [ ] Success criteria: Zero incidents caused by schema drift or undetected typos in configuration; all new models added with `extra="forbid"` verified in code review
- [ ] Metrics to track: Count of ValidationError incidents in production (should be zero in normal operation; non-zero indicates a bug in a producer), number of models in codebase vs. number with `extra="forbid"` (should be equal)

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-04-17 | Mohamed Ameen | Initial ADR created |
