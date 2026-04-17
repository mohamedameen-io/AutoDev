# ADR-005: Protocol-Based Plugin System with Entry-Point Discovery

**Status:** Accepted
**Date:** 2026-04-17
**Deciders:** Mohamed Ameen
**Tags:** plugins, extensibility, structural-typing, entry-points, protocols
**Related ADRs:** ADR-006 (Platform Adapter Abstraction)

## Context

AutoDev is a multi-agent orchestrator that ships with built-in QA gates, tournament judges, and agent definitions. However, teams need to extend AutoDev without forking the codebase -- adding custom QA gates (e.g., security scanners, performance benchmarks), alternative tournament judges (e.g., domain-specific evaluation criteria), and new agent definitions (e.g., a "security auditor" role).

The plugin system must satisfy three constraints simultaneously:

1. **Zero coupling**: Third-party plugin packages must not import or depend on `autodev` types at import time. Plugin authors should be able to develop, test, and ship their packages independently of the AutoDev release cycle.
2. **Safe discovery**: A malformed plugin (missing dependency, crash in `__init__`, wrong shape) must never crash the host process. The orchestrator must log the failure and continue.
3. **Standard packaging**: Plugin installation should use pip/uv and standard `pyproject.toml` entry-point declarations. No custom config files, no YAML registries, no import hooks.

The plugin system needs to support three extension points corresponding to the three subsystems that teams most commonly want to customize:
- **QA gates** -- custom quality checks run against implementation diffs
- **Judge providers** -- custom tournament ranking algorithms
- **Agent extensions** -- new agent definitions contributed to the workspace

## Options Considered

### Option A: Abstract Base Class Inheritance

**Description:**
Plugins inherit from ABC classes defined in `autodev.plugins` (e.g., `class MyGate(QAGateBase)`). Discovery uses the same `entry_points` mechanism, but each loaded class is validated via `isinstance(cls, QAGateBase)` which requires the plugin to have imported the ABC.

**Pros:**
- Full method signature enforcement at class definition time (not just attribute presence)
- IDE support for method stubs via inheritance
- Familiar pattern from Django, Flask, pytest, and most Python frameworks
- ABC enforcement catches missing methods before runtime

**Cons:**
- **Hard coupling**: Plugin package must `import autodev.plugins.QAGateBase`, creating a build-time dependency on AutoDev. Version conflicts arise when plugin pins `autodev>=0.8` but user runs `autodev==0.7`
- Version skew: ABC signature changes are breaking changes for all installed plugins
- Heavier install footprint: every plugin transitively pulls in `autodev` and its dependencies (pydantic, structlog, etc.)
- Circular dependency risk: if AutoDev ever imports plugin-contributed types, the import graph cycles

### Option B: Class Registration Decorator Pattern

**Description:**
AutoDev provides a `@register_plugin("qa_gate")` decorator. Plugin authors decorate their classes; the decorator appends the class to a module-level registry. Discovery iterates the registry at startup.

**Pros:**
- Explicit opt-in: only decorated classes are discovered
- Decorator can validate shape at decoration time (fail fast)
- Familiar pattern from click, FastAPI, celery

**Cons:**
- **Import-order dependency**: the module containing the decorated class must be imported before the registry is read. This requires either an explicit import chain or a separate "register all plugins" bootstrap step
- Still requires coupling: the decorator itself lives in `autodev`, so the plugin must `from autodev.plugins import register_plugin`
- Global mutable state: the module-level registry is a singleton that can be accidentally cleared or double-registered
- No standard packaging mechanism: requires custom loader code to find and import plugin modules (reinvents what `entry_points` already provides)
- Testing is harder: must reset the global registry between test cases

### Option C: Protocol + runtime_checkable with Entry-Point Discovery (Current Choice)

**Description:**
Three `@runtime_checkable` Protocol classes (`QAGatePlugin`, `JudgeProviderPlugin`, `AgentExtensionPlugin`) define the structural contract. Plugins are discovered via `importlib.metadata.entry_points(group="autodev.plugins")`. Each entry point is loaded, instantiated (if it is a class), and bucketed into the `PluginRegistry` based on which Protocol it satisfies via `isinstance()`. Malformed plugins are logged at WARNING level and skipped.

Third-party packages declare plugins in their `pyproject.toml`:

```toml
[project.entry-points."autodev.plugins"]
my_qa_gate = "mypkg.plugins:MyQAGate"
```

The plugin class simply implements the right shape -- it never imports anything from `autodev`.

**Pros:**
- **Zero hard dependency**: plugin packages need zero imports from `autodev`. They can develop against a copy-pasted Protocol docstring or their own test doubles
- Standard Python packaging: `entry_points` is the blessed mechanism since PEP 517/518; pip, uv, flit, hatch, and poetry all support it
- Structural typing: duck typing at runtime means any object with `name: str` and `async def run(ctx) -> GateResult` qualifies
- Safe discovery: each entry point load/instantiate is wrapped in try/except; failures are logged and skipped
- No global mutable state: `PluginRegistry` is a fresh dataclass returned by `discover_plugins()`
- Testable: tests patch `entry_points()` return value; no module-level side effects
- Pre-instantiated objects accepted: entry points that resolve to already-instantiated objects (not classes) are handled gracefully

**Cons:**
- `runtime_checkable` only validates attribute presence, not method signatures. A plugin with `def run(self, wrong_args)` passes the isinstance check but fails at call time
- No IDE auto-complete for plugin authors unless they voluntarily import the Protocol (which defeats the zero-coupling goal)
- Entry-point iteration order is package-name-alphabetical, not explicitly controlled. Last-write-wins for duplicate `name` attributes is deterministic but not intuitive
- Plugin authors get no type-checking feedback at development time unless they set up their own Protocol mirror

## Decision Drivers

- **Stateless Reproducibility:** Plugin discovery runs once at startup; the returned `PluginRegistry` is immutable for the session lifetime. No hidden mutable state.
- **Pydantic-Boundary Strictness:** `QAContext` and `GateResult` are narrow dataclasses (not Pydantic models with `extra="forbid"`) -- intentionally loose so plugins remain forward-compatible as fields are added.
- **Asyncio-Friendliness:** `QAGatePlugin.run()` and `JudgeProviderPlugin.rank()` are async, so long-running subprocess gates don't block the orchestrator event loop.
- **Testability:** `discover_plugins()` accepts a `group` parameter override, making it trivial to test with isolated entry-point groups.
- **Platform Portability:** Plugins are platform-agnostic; the `AgentExtensionPlugin.render_platform(platform)` method lets a plugin emit platform-specific artifacts.
- **Zero Coupling:** The primary driver. Plugin packages must not depend on `autodev` at build time.

## Architecture Drivers Comparison

| Architecture Driver        | Option A: ABC Inheritance | Option B: Decorator Registry | Option C: Protocol + Entry-Points |Notes |
|----------------------------|---------------------------|------------------------------|-----------------------------------|------|
| **Zero Coupling**          | ⭐ (1/5)                 | ⭐⭐ (2/5)                   | ⭐⭐⭐⭐⭐ (5/5)              | A/B require `import autodev`; C requires nothing |
| **Crash Safety**           | ⭐⭐⭐ (3/5)             | ⭐⭐ (2/5)                   | ⭐⭐⭐⭐⭐ (5/5)              | C wraps every load/instantiate in try/except |
| **Testability**            | ⭐⭐⭐ (3/5)             | ⭐⭐ (2/5)                   | ⭐⭐⭐⭐⭐ (5/5)              | C: patch `entry_points()`, no global state. B: must reset singleton |
| **Asyncio Compatibility**  | ⭐⭐⭐⭐ (4/5)           | ⭐⭐⭐⭐ (4/5)               | ⭐⭐⭐⭐ (4/5)                | All three can define async methods; equal |
| **Determinism**            | ⭐⭐⭐⭐ (4/5)           | ⭐⭐ (2/5)                   | ⭐⭐⭐⭐ (4/5)                | B has import-order dependency; A and C are deterministic |
| **Complexity**             | ⭐⭐⭐⭐ (4/5)           | ⭐⭐⭐ (3/5)                 | ⭐⭐⭐⭐ (4/5)                | C is ~80 lines; B needs bootstrap loader |
| **Signature Safety**       | ⭐⭐⭐⭐⭐ (5/5)         | ⭐⭐⭐⭐ (4/5)               | ⭐⭐ (2/5)                    | A catches wrong signatures at import; C defers to call time |
| **Standard Packaging**     | ⭐⭐⭐ (3/5)             | ⭐ (1/5)                     | ⭐⭐⭐⭐⭐ (5/5)              | C uses PEP 517 entry-points; B needs custom loader |

**Rating Scale:**
- ⭐⭐⭐⭐⭐ (5/5) - Excellent
- ⭐⭐⭐⭐ (4/5) - Good
- ⭐⭐⭐ (3/5) - Average
- ⭐⭐ (2/5) - Below Average
- ⭐ (1/5) - Poor

## Decision Outcome

**Chosen Option:** Option C: Protocol + runtime_checkable with Entry-Point Discovery

**Rationale:**
The primary decision driver is zero coupling. AutoDev is a CLI tool that orchestrates third-party coding agents; its plugin ecosystem must not create a dependency web where plugin version upgrades force AutoDev version upgrades (or vice versa). Option C is the only option that achieves true structural typing -- a plugin class that happens to have the right method shapes will work, regardless of whether it has ever seen an `autodev` import.

The trade-off accepted is weaker signature validation: `runtime_checkable` checks attribute presence but not method arity. This is mitigated by the narrow Protocol surfaces (each Protocol has 1-2 methods with simple signatures) and by comprehensive test coverage that exercises the actual call paths. In practice, a plugin with a wrong method signature will fail loudly the first time it is called, not silently.

**Key Factors:**
- Zero coupling eliminates version-skew breakage across the plugin ecosystem
- `entry_points` is the Python-standard discovery mechanism, requiring zero custom infrastructure
- `PluginRegistry` as a plain dataclass (not a singleton) makes testing deterministic and avoids global state
- Safe discovery (log-and-skip on failure) means a broken third-party plugin never takes down the orchestrator

## Consequences

### Positive Consequences
- Plugin authors can develop, test, and release independently with no `autodev` dependency in their `pyproject.toml`
- New plugin kinds can be added by defining a new Protocol and adding a bucket to `PluginRegistry` -- no framework changes needed
- The orchestrator is protected from plugin failures: a broken plugin is logged and skipped, never crashes the host
- Tests are hermetic: `discover_plugins()` is a pure function of `entry_points()` return value

### Negative Consequences / Trade-offs
- Plugin authors get no compile-time feedback on method signature correctness unless they voluntarily import the Protocol for type-checking
- `runtime_checkable` isinstance checks are shallow (attribute presence only), so a plugin with `def run(self)` (missing `ctx` parameter) passes discovery but fails at call time
- Last-write-wins semantics for duplicate plugin names: if two packages declare a QA gate with `name = "security"`, the one loaded last wins. This is deterministic (alphabetical by package name) but not explicitly controllable

### Neutral / Unknown Consequences
- Performance of `entry_points()` at startup: currently negligible, but could slow down if hundreds of plugins are installed. Monitor startup time if the ecosystem grows.
- Whether `QAContext` and `GateResult` should migrate to Pydantic models with `extra="forbid"` as the plugin ecosystem matures. Currently kept as plain dataclasses for forward-compatibility.

## Implementation Notes

**Files Affected:**
- `src/plugins/__init__.py` -- Public API re-exports: `QAGatePlugin`, `JudgeProviderPlugin`, `AgentExtensionPlugin`, `PluginRegistry`, `discover_plugins`, `QAContext`, `GateResult`
- `src/plugins/registry.py` -- Protocol definitions, `PluginRegistry` dataclass, `discover_plugins()` function, `_plugin_name()` helper
- `tests/test_plugins_discovery.py` -- Unit tests for all discovery paths: empty group, each protocol kind, protocol mismatch, load error, instantiate error, pre-instantiated objects, multiple plugins

**Ledger/State Implications:**
- None. Plugin discovery is stateless and does not write to the JSONL ledger. The `PluginRegistry` is an in-memory object scoped to the session.

**General Guidance:**
- When adding a new Protocol (e.g., `OutputFormatterPlugin`), add a corresponding bucket to `PluginRegistry` and an `isinstance` branch in `discover_plugins()`
- Keep Protocol surfaces narrow: 1-2 methods with simple signatures. Wide Protocols are harder for third parties to implement correctly with structural typing
- Never add `autodev` as a dependency in plugin package documentation or examples. The zero-coupling contract is the key value proposition

## Evidence from Codebase

**Source References:**
- `src/plugins/registry.py:55-105` -- Three Protocol definitions (`QAGatePlugin`, `JudgeProviderPlugin`, `AgentExtensionPlugin`) with `@runtime_checkable` decorator and docstrings explaining the zero-dependency contract
- `src/plugins/registry.py:162-225` -- `discover_plugins()` implementation: iterates `entry_points(group=group)`, wraps load/instantiate in try/except, buckets via isinstance, logs mismatches at WARNING
- `src/plugins/registry.py:139-159` -- `PluginRegistry` dataclass with name-keyed dicts per bucket and `total`/`is_empty` helpers
- `src/plugins/registry.py:191-192` -- Pre-instantiated object handling: `instance = target() if isinstance(target, type) else target`
- `src/plugins/__init__.py:14-17` -- Module docstring showing the entry-point declaration format for third-party packages

**Test Coverage:**
- `tests/test_plugins_discovery.py::test_discover_empty_group` -- Validates that an empty entry-point group returns an empty registry
- `tests/test_plugins_discovery.py::test_discover_qa_gate` -- Validates QA gate Protocol matching and bucketing
- `tests/test_plugins_discovery.py::test_discover_protocol_mismatch_skipped` -- Validates that objects not matching any Protocol are logged and skipped
- `tests/test_plugins_discovery.py::test_discover_load_error_skipped` -- Validates that ImportError during load is caught and skipped
- `tests/test_plugins_discovery.py::test_discover_instantiate_error_skipped` -- Validates that exceptions in `__init__` are caught and skipped
- `tests/test_plugins_discovery.py::test_discover_pre_instantiated_plugin` -- Validates that entry points resolving to instances (not classes) are handled

**Property-Based Tests (Hypothesis):**
- N/A -- discovery logic is deterministic given mocked entry points; property-based testing would not add signal here.

## Related Design Documents

- [agents.md](../../docs/design_documentation/agents.md) -- Custom agents can be added via `AgentExtensionPlugin`, which contributes agent definitions via the plugin system
- [tournaments.md](../../docs/design_documentation/tournaments.md) -- Custom judges can replace or supplement built-in Borda-aggregated judges via `JudgeProviderPlugin`
- [adapters.md](../../docs/design_documentation/adapters.md) -- Platform adapters are a separate extension point (ABC-based, not plugin-based) because they require tighter coupling with the orchestrator

## Monitoring and Review

- [ ] Review date: 2026-10-17
- [ ] Success criteria: At least one third-party plugin package published and working without `autodev` in its dependencies
- [ ] Metrics to track: Number of plugins discovered at startup; frequency of `plugins.load_failed` and `plugins.protocol_mismatch` warnings in production logs

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-04-17 | Mohamed Ameen | Initial ADR created |
