# QA Gates Pipeline Design

**Status:** Implemented
**Author:** Mohamed Ameen
**Date:** 2026-04-17
**Last Updated:** 2026-05-09
**Version:** v0.21.1
**Reviewers:** --
**Package:** `src/qa/`
**Entry Point:** Invoked by `orchestrator/execute_phase.py` after the `coded` status; no standalone CLI subcommand. The `autodev secretscan baseline` CLI subcommand (v0.19.0) refreshes the per-repo secretscan baseline.

## 1. Overview

### 1.1 Purpose

The QA Gates Pipeline provides a sequential battery of local, zero-LLM-cost quality checks that run automatically after a developer agent produces code and before a reviewer sees it. Each gate validates one dimension of code quality (syntax correctness, lint compliance, build integrity, test passage, secret absence) and returns a pass/fail verdict. A failed gate triggers a developer retry with structured feedback, preventing obviously broken code from consuming expensive reviewer and tournament cycles.

### 1.2 Scope

**In scope:**

- Language and toolchain detection from project manifest files
- Built-in gates: `syntax_check`, `lint`, `build_check`, `test_runner`, `secretscan`, `hallucination_guard`, `mutation_test`
- Sequential fail-fast pipeline execution
- Per-gate configuration toggles via `QAGatesConfig`
- Graceful degradation when tools are not installed
- `GateResult` return type for uniform verdict reporting
- Extension via `QAGatePlugin` protocol for third-party gates
- Per-repo secretscan baseline (v0.19.0): catches NEW secrets vs. an accepted baseline, with per-extension entropy tuning
- Mutation-test gate (v0.19.0): opt-in `mutmut` runner that gates on kill-rate
- Extended-scope critic gate (v0.20.0): `critic_sounding_board` review of any task that declares paths outside its phase/plan `EDIT_SCOPE` (see [extended_scope.md](extended_scope.md))

**Out of scope:**

- SAST scanning (toggle `sast_scan` is a planning-time advisory consumed by agent prompts only; no dispatch yet)
- LLM-based code review (that is the `reviewer` agent role, not a QA gate)
- Retry/escalation logic (handled by `orchestrator/execute_phase.py`)

### 1.3 Context

The QA Gates Pipeline sits between the developer agent and the reviewer agent in the execute phase:

```
developer (coded) -> [QA GATES] -> auto_gated -> reviewer -> tested -> tournament -> complete
```

Gates run locally using the project's own toolchain. They are the cheapest validation layer in the pipeline (zero LLM calls) and serve as the first filter, catching syntax errors, lint violations, build failures, test regressions, and leaked secrets before any LLM-based review occurs.

## 2. Requirements

### 2.1 Functional Requirements

- **FR-1:** Detect the project's primary language from manifest files (`pyproject.toml`, `package.json`, `Cargo.toml`, etc.).
- **FR-2:** Run each enabled gate in sequence: syntax_check -> lint -> build_check -> test_runner -> secretscan -> hallucination_guard -> mutation_test.
- **FR-3:** Stop at the first failed gate and return the failure details (fail-fast).
- **FR-4:** When a gate's required tool is not installed, pass gracefully with an informational message rather than failing.
- **FR-5:** Support per-gate enable/disable toggles via `QAGatesConfig`.
- **FR-6:** Support third-party gate extensions via the `QAGatePlugin` protocol.
- **FR-7:** Secret scanning must detect well-known secret patterns (AWS keys, GitHub PATs, Slack tokens, Stripe keys, private key headers) and high-entropy strings.
- **FR-8:** Secretscan must support a per-repo baseline (`secretscan_baseline_enabled`) that masks pre-existing findings, plus per-extension entropy thresholds (`secretscan_per_extension_thresholds`) tunable from config.
- **FR-9:** Mutation testing (`mutation_test_enabled`) must run after the standard test gate passes and gate on a configurable kill-rate threshold (`mutation_test_threshold`, default 0.7).
- **FR-10:** Tasks that declare a non-empty `Task.extended_scope` must be routed through `critic_sounding_board` for an EXTENDED SCOPE REVIEW before the synchronous edit-scope validator admits the paths.

### 2.2 Non-Functional Requirements

- **Zero LLM cost:** All gates are local subprocess executions or in-process analysis. No LLM calls.
- **Asyncio concurrency:** All gates use `asyncio.create_subprocess_exec` and `asyncio.wait_for` with configurable timeouts. No blocking I/O on the event loop.
- **Fail-fast:** The pipeline short-circuits on the first failure. Remaining gates are not executed, minimizing time spent on a known-bad state.
- **Graceful degradation:** A missing tool (e.g., `ruff` not installed) causes the gate to pass with an informational message, not crash the pipeline. This ensures the orchestrator can run in minimal environments.
- **Maintainability:** Each gate is a standalone module with a single async entry function. Adding a new gate requires one new module and one entry in the pipeline list.

### 2.3 Constraints

- Must run on Python 3.11+ with no compiled extensions.
- Must work within a single-machine context.
- Tool availability depends on the user's environment (ruff, eslint, cargo, etc. are not bundled).
- Timeout defaults to 60 seconds per gate to prevent runaway subprocesses.

## 3. Architecture

### 3.1 High-Level Design

```mermaid
flowchart TB
    subgraph "Execute Phase"
        DEV[Developer Agent] -->|coded| QA["QA Gates Pipeline"]
        QA -->|all pass| AG[auto_gated]
        QA -->|any fail| RETRY["Retry Developer<br/>(with failure feedback)"]
        RETRY -->|retry_count < limit| DEV
        RETRY -->|retry_count >= limit| ESC["Escalate to<br/>critic_sounding_board"]
    end

    subgraph "QA Gates Pipeline (sequential, fail-fast)"
        D[detect_language] --> S[syntax_check]
        S -->|pass| L[lint]
        L -->|pass| B[build_check]
        B -->|pass| T[test_runner]
        T -->|pass| SC[secretscan]
        SC -->|pass| HG[hallucination_guard]
        HG -->|pass| MT[mutation_test]
        MT -->|pass| PL[plugin gates]
        PL -->|pass| PASS[GateResult: passed]

        S -->|fail| FAIL[GateResult: failed]
        L -->|fail| FAIL
        B -->|fail| FAIL
        T -->|fail| FAIL
        SC -->|fail| FAIL
        HG -->|fail| FAIL
        MT -->|fail| FAIL
        PL -->|fail| FAIL
    end
```

The `secretscan` and `mutation_test` gates accept a diff-scoped path list (v0.13.0+ / v0.19.0+ respectively) so the scan touches only the developer's just-introduced changes — pre-existing repo state is not re-evaluated on every retry.

### 3.2 Component Structure

| File | Purpose |
|------|---------|
| `qa/__init__.py` | Re-exports all public gate functions and `GateResult` |
| `qa/detect.py` | `detect_language()` and `detect_toolchain()` -- manifest-based detection |
| `qa/syntax_check.py` | `run_syntax_check()` -- Python `py_compile`, Node.js `node --check` |
| `qa/lint.py` | `run_lint()` -- ruff (Python), eslint (Node.js), cargo clippy (Rust), golangci-lint (Go) |
| `qa/build_check.py` | `run_build_check()` -- py_compile (Python), tsc/npm build (Node.js), cargo check (Rust), go build (Go) |
| `qa/test_runner.py` | `run_tests()` -- pytest (Python), npm test (Node.js), cargo test (Rust), go test (Go) |
| `qa/secretscan.py` | `run_secretscan()` -- regex patterns + Shannon entropy heuristics; per-extension thresholds; baseline filter |
| `qa/secretscan_baseline.py` | `compute_baseline()`, `load_baseline()`, `filter_against_baseline()` -- per-repo baseline persisted to `.autodev/secretscan-baseline.json` |
| `qa/mutation_test.py` | `run_mutation_test()` -- mutmut subprocess wrapper, kill-rate gate |
| `qa/equivalence_filter.py` | Stage-1 static AST/whitespace equivalence filter for surviving mutants |
| `qa/llm_equivalence_judge.py` | Stage-2 LLM-based semantic-equivalence judge for surviving mutants |
| `qa/hallucination_guard.py` | `run_hallucination_guard()` -- AST walk over developer-introduced files |
| `qa/cpp_symbols.py` | C++ symbol-table support for hallucination-guard cross-file resolution |
| `cli/commands/secretscan_baseline.py` | `autodev secretscan baseline` Click command -- refreshes the baseline |
| `orchestrator/extended_scope_critic.py` | `critic_review_extended_scope()` -- routes a task with non-empty `extended_scope` through `critic_sounding_board` |
| `plugins/registry.py` | `QAGatePlugin` protocol, `GateResult` dataclass, `QAContext`, plugin discovery |

### 3.3 Data Models

```python
@dataclass
class GateResult:
    """Verdict emitted by a QA gate."""
    passed: bool
    details: str = ""

@dataclass
class QAContext:
    """Inputs handed to QAGatePlugin.run()."""
    cwd: Path
    task_id: str
    diff: str | None = None

class QAGatesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    syntax_check: bool = True
    lint: bool = True
    build_check: bool = True
    test_runner: bool = True
    secretscan: bool = True
    # v0.19.0: per-repo secretscan baseline. When True, the gate diffs
    # findings against ``.autodev/secretscan-baseline.json`` and only
    # net-new findings trip the gate. Refresh with
    # ``autodev secretscan baseline``.
    secretscan_baseline_enabled: bool = False
    # v0.19.0: per-extension entropy override. ``None`` means use the
    # module-default curve (looser thresholds for ``.cpp`` / ``.yaml`` etc.).
    secretscan_per_extension_thresholds: dict[str, float] | None = None
    # Planning-time advisories (consumed by agent prompts only — NOT
    # dispatched as gates). Kept for forward-compat with ADR-008.
    sast_scan: bool = False
    mutation_test: bool = False
    # v0.19.0: dispatch toggle for the mutation-test gate (distinct from
    # the planning-time ``mutation_test`` advisory). When True, the gate
    # invokes ``qa.mutation_test.run_mutation_test`` after the standard
    # tests pass.
    mutation_test_enabled: bool = False
    mutation_test_threshold: float = 0.7
```

### 3.4 Protocol / Interface Contracts

```python
@runtime_checkable
class QAGatePlugin(Protocol):
    """A custom QA gate. Runs against a checked-out diff.

    Third-party gates are discovered via entry-points:
        [project.entry-points."autodev.plugins"]
        my_qa_gate = "mypkg.plugins:MyQAGate"
    """
    name: str

    async def run(self, ctx: QAContext) -> GateResult:
        """Evaluate the gate and return a GateResult.

        Must be async so long-running subprocess gates don't block
        the orchestrator event loop.
        """
        ...
```

Third-party plugins are discovered via `importlib.metadata.entry_points(group="autodev.plugins")` at runtime. Plugins that fail to load or don't satisfy the protocol are logged at WARNING level and skipped.

### 3.5 Interfaces

**Gate functions (all async, all return `GateResult`):**

| Function | Module | Description |
|----------|--------|-------------|
| `detect_language(cwd) -> str \| None` | `qa/detect.py` | Detect primary language from manifests |
| `detect_toolchain(cwd) -> str \| None` | `qa/detect.py` | Map language to canonical lint/build tool |
| `run_syntax_check(cwd, language, timeout_s) -> GateResult` | `qa/syntax_check.py` | Compile/parse all source files |
| `run_lint(cwd, language, timeout_s) -> GateResult` | `qa/lint.py` | Run language-appropriate linter |
| `run_build_check(cwd, language, timeout_s) -> GateResult` | `qa/build_check.py` | Run build/typecheck tool |
| `run_tests(cwd, language, timeout_s) -> GateResult` | `qa/test_runner.py` | Run project test suite |
| `run_secretscan(cwd, paths, per_extension_thresholds, baseline_enabled) -> GateResult` | `qa/secretscan.py` | Scan for hard-coded secrets; diff-scoped + baseline-filtered |
| `compute_baseline(cwd) -> set[str]` | `qa/secretscan_baseline.py` | Snapshot the current finding set into `.autodev/secretscan-baseline.json` |
| `filter_against_baseline(findings, cwd) -> list[str]` | `qa/secretscan_baseline.py` | Drop findings already recorded in the baseline |
| `run_mutation_test(cwd, paths, kill_rate_threshold, timeout_s) -> GateResult` | `qa/mutation_test.py` | Run mutmut on Python sources; gate on kill-rate |
| `run_hallucination_guard(cwd, paths) -> GateResult` | `qa/hallucination_guard.py` | AST walk for unresolved symbol references |

**Pipeline entry (in `execute_phase.py`):**

```python
async def _run_qa_gates(orch, task, *, developer_result=None) -> str | None:
    """Run enabled QA gates. Returns the first failure detail string, or None if all pass."""
    cfg = orch.cfg.qa_gates
    cwd = orch.cwd
    language = detect_language(cwd)

    # v0.13.0: derive a diff-scoped path list from the developer's result so
    # secretscan / mutation_test / hallucination_guard touch only the just-
    # introduced changes (not the pre-existing repo state).
    secretscan_paths = _files_changed_for_secretscan(developer_result)
    hallucination_guard_enabled = bool(getattr(orch.cfg, "hallucination_guard", True))

    gates = [
        (cfg.syntax_check, lambda: run_syntax_check(cwd, language)),
        (cfg.lint,         lambda: run_lint(cwd, language)),
        (cfg.build_check,  lambda: run_build_check(cwd, language)),
        (cfg.test_runner,  lambda: run_tests(cwd)),
        (cfg.secretscan,   lambda: run_secretscan(
            cwd,
            paths=secretscan_paths,
            per_extension_thresholds=cfg.secretscan_per_extension_thresholds,
            baseline_enabled=cfg.secretscan_baseline_enabled,
        )),
        (hallucination_guard_enabled, lambda: run_hallucination_guard(
            cwd, paths=secretscan_paths,
        )),
        (cfg.mutation_test_enabled, lambda: run_mutation_test(
            cwd,
            paths=secretscan_paths,
            kill_rate_threshold=cfg.mutation_test_threshold,
        )),
    ]

    for enabled, gate_fn in gates:
        if not enabled:
            continue
        result = await gate_fn()
        if not result.passed:
            return result.details or "QA gate failed"
    # Plugin QA gates run after all built-in gates pass (see Section 6.4).
    return None
```

## 4. Design Decisions

### 4.1 Key Decisions

| Decision | Rationale | Alternatives Considered |
|----------|-----------|------------------------|
| Sequential fail-fast pipeline | A syntax error makes lint and build results meaningless. Failing fast saves time and provides a clear, focused error message for the developer retry. | Parallel execution (more expensive, confusing multi-error feedback), run all gates regardless (wastes time) |
| Graceful degradation on missing tools | AutoDev must work in minimal environments where not all tools are installed. A missing linter should not block the pipeline. | Hard-fail on missing tools (too strict), skip gates entirely (loses coverage) |
| Language detection via manifest files | Manifest files (pyproject.toml, package.json, etc.) are the most reliable indicator of project language and toolchain. | File extension heuristics (error-prone for polyglot repos), explicit config (extra burden) |
| In-process secret scanning | Avoids dependency on external tools like `trufflehog` or `gitleaks`. Uses well-known regex patterns and Shannon entropy for high-entropy string detection. | External tool dependency (not always available), git-hooks-only (doesn't catch secrets in non-git context) |
| `GateResult` as a simple dataclass | Gates need to return only pass/fail and details. No need for Pydantic validation at this boundary -- the data never crosses a serialization boundary. | Pydantic model (overhead not justified), exception-based signaling (less composable) |
| Plugin protocol for extensibility | Third-party gates (SAST tools, custom checks) can be added via entry-points without modifying AutoDev source. | Config-driven command list (less type-safe), monkey-patching (fragile) |

### 4.2 Trade-offs

- **Detection accuracy vs. simplicity:** Manifest-based detection uses first-match ordering. A polyglot repo with both `pyproject.toml` and `package.json` will be detected as Python. This is correct for AutoDev's single-language-at-a-time model but may miss secondary languages.
- **Coverage vs. tool dependency:** Built-in gates cover Python, Node.js, Rust, and Go. Other languages (Java, .NET, Ruby, Swift) are detected but gates pass with "no checker configured" -- coverage is language-dependent.
- **Entropy threshold:** The `_ENTROPY_THRESHOLD = 4.5` for secret scanning balances false positives (random IDs, hashes) against false negatives (short or low-entropy secrets). The threshold is tunable but not yet exposed in config.

## 5. Implementation Details

### 5.1 Core Algorithms/Logic

**Language detection (`detect_language`):**

First-match ordering against manifest files:

| Priority | File | Language |
|----------|------|----------|
| 1 | `pyproject.toml` or `setup.py` | `python` |
| 2 | `package.json` | `nodejs` |
| 3 | `Cargo.toml` | `rust` |
| 4 | `go.mod` | `go` |
| 5 | `pom.xml` or `build.gradle` | `java` |
| 6 | `*.csproj` | `dotnet` |
| 7 | `Gemfile` | `ruby` |
| 8 | `*.swift` | `swift` |

**Toolchain mapping (`detect_toolchain`):**

| Language | Tool |
|----------|------|
| python | ruff |
| nodejs | eslint |
| rust | cargo |
| go | golangci-lint |
| java | maven (or gradle if `build.gradle` present) |
| dotnet | dotnet |
| ruby | rubocop |
| swift | swiftlint |

**Secret scanning algorithm:**

1. **File filtering:** Skip known noise directories (`.git`, `node_modules`, `.venv`, `__pycache__`, etc.) and binary file extensions (`.pyc`, `.so`, `.png`, `.zip`, etc.).
2. **Regex pattern scan:** Match against 8 known secret patterns:
   - AWS access keys (`AKIA[0-9A-Z]{16}`)
   - GitHub PATs/OAuth/Actions tokens (`ghp_`, `gho_`, `ghs_` prefixes)
   - Private key headers (`-----BEGIN ... PRIVATE KEY-----`)
   - Slack tokens (`xox[baprs]-...`)
   - Stripe secret keys (`sk_live_...`)
   - Generic API key assignments (`api_key = "..."`)
3. **Shannon entropy scan:** Find quoted strings of 20+ alphanumeric characters and flag those with entropy >= 4.5 bits/char.

```python
def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    freq: dict[str, int] = {}
    for ch in text:
        freq[ch] = freq.get(ch, 0) + 1
    length = len(text)
    return -sum((c / length) * math.log2(c / length) for c in freq.values())
```

### 5.2 Concurrency Model

Each gate runs a subprocess asynchronously:

```python
async def _run_subprocess(
    args: list[str], cwd: Path, *, timeout_s: float, tool_name: str
) -> GateResult:
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *args, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=timeout_s,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except FileNotFoundError:
        return GateResult(passed=True, details=f"{tool_name} not found, skipping")
    except asyncio.TimeoutError:
        return GateResult(passed=False, details=f"{tool_name} timed out")

    if proc.returncode == 0:
        return GateResult(passed=True, details=f"{tool_name} passed")
    return GateResult(passed=False, details=f"{tool_name} failed:\n{combined}")
```

Key design points:
- `asyncio.create_subprocess_exec` avoids shell injection.
- `asyncio.wait_for` with configurable timeout prevents runaway processes.
- `FileNotFoundError` is caught for graceful degradation when the tool is not installed.
- `stdout` and `stderr` are captured via `PIPE` for inclusion in failure details.

Gates run sequentially (fail-fast), so no concurrent gate execution occurs within a single pipeline run.

### 5.3 Subprocess Invocation Pattern

All gate subprocesses follow a common pattern:

| Gate | Command(s) |
|------|-----------|
| **syntax_check (Python)** | `python -m py_compile <files>` |
| **syntax_check (Node.js)** | `node --check <file>` (per-file) |
| **lint (Python)** | `ruff check .` |
| **lint (Node.js)** | `npx eslint .` |
| **lint (Rust)** | `cargo clippy` |
| **lint (Go)** | `golangci-lint run` |
| **build_check (Python)** | `python -m py_compile <files>` |
| **build_check (Node.js)** | `npm run build` (if script exists) or `npx tsc --noEmit` |
| **build_check (Rust)** | `cargo check` |
| **build_check (Go)** | `go build ./...` |
| **test_runner (Python)** | `pytest` |
| **test_runner (Node.js)** | `npm test` |
| **test_runner (Rust)** | `cargo test` |
| **test_runner (Go)** | `go test ./...` |
| **secretscan** | (in-process, no subprocess) |
| **mutation_test (Python)** | `mutmut run --no-progress [--paths-to-mutate <files>]` then `mutmut results --json` |
| **hallucination_guard** | (in-process AST walk, no subprocess) |

Python syntax check and build check filter files to exclude `.venv` and `__pycache__` directories. Node.js syntax check excludes `node_modules`.

### 5.4 Error Handling

Gates handle three error categories:

1. **Tool not found (`FileNotFoundError`):** Gate passes with informational message. This is graceful degradation, not an error.
2. **Timeout (`asyncio.TimeoutError`):** Gate fails. The timeout message is returned as the failure detail.
3. **Non-zero exit code:** Gate fails. Combined stdout+stderr is returned as the failure detail (truncated to first 20 findings for secretscan).

The pipeline caller (`_run_qa_gates` in `execute_phase.py`) returns the first failure detail string, which the orchestrator passes to `_try_retry_or_escalate`:
- If `retry_count < qa_retry_limit` (default 3): the developer is retried with the gate failure injected as `last_issues` context.
- If `retry_count >= qa_retry_limit`: the task is escalated to `critic_sounding_board` and marked as `blocked`.

### 5.5 Mutation testing gate (v0.19.0)

`qa/mutation_test.py` exposes `run_mutation_test()` — an opt-in QA gate that
shells out to [`mutmut`](https://mutmut.readthedocs.io/) and gates on the
**kill rate**: the fraction of mutants the existing test suite caught. A
mutant survives when no test fails after the mutation, signalling that the
mutated code path is under-covered.

**Configuration**

| Field | Default | Purpose |
|-------|---------|---------|
| `mutation_test_enabled` | `False` | Master dispatch toggle. Distinct from the planning-time `mutation_test` advisory (which is consumed by agent prompts only and never dispatches a gate). |
| `mutation_test_threshold` | `0.7` | Minimum acceptable kill-rate (0.0–1.0). Looser than a typical coverage gate because mutation testing is more demanding. |

**When it runs.** Last in the built-in sequence — *after* `syntax_check`,
`lint`, `build_check`, `test_runner`, `secretscan`, and
`hallucination_guard`. The gate is meaningful only when the standard test
suite is green; running it earlier would conflate test failures with
mutation survivors.

**Behavior.** The runner:

1. Skips with a pass-and-warn if `mutmut` is not on `PATH` (graceful
   degradation: a missing binary must not block the pipeline).
2. Filters the diff-scope path list to Python files only. Empty Python
   filter → pass-and-warn ("no Python files in diff scope").
3. Invokes `mutmut run --no-progress [--paths-to-mutate <rel-paths>]`
   with a 5-minute hard cap. Hangs return a pass-and-warn (false negatives
   preferred over flakes).
4. Reads `mutmut results --json` to extract counts (`killed`, `survived`,
   `timeout`, `suspicious`, `skipped`).
5. Computes `kill_rate = killed / (killed + survived + timeout + suspicious)`.
   `skipped` mutants are excluded from both numerator and denominator.
6. Optionally adjusts the kill-rate upward via two staged equivalence
   filters when survivors exist:
   - **Stage 1** (`qa/equivalence_filter.py`) — static AST/whitespace
     equivalence: a mutant whose AST differs only in trivia is treated as
     killed.
   - **Stage 2** (`qa/llm_equivalence_judge.py`) — LLM-based semantic
     equivalence: gated on `mutation_cache` presence + Anthropic key
     availability; a survivor judged semantically equivalent to the
     original is treated as killed.
7. Returns `passed = kill_rate >= mutation_test_threshold`.

**Cost shape.** Mutation testing is the most expensive gate by far — every
mutant requires re-running the test suite. The 5-minute hard cap and
diff-scope filter contain the blast radius; opt-in default keeps the gate
off in default configurations where the cost is not warranted.

### 5.6 Secretscan baseline (v0.19.0)

`qa/secretscan_baseline.py` provides a per-repo baseline so legacy
high-entropy strings or test fixtures don't trip the gate on every run.
The baseline file lives at `.autodev/secretscan-baseline.json` and is
keyed by `f"{rel_path}|{label}"` — the repo-relative file path plus the
finding category (`"AWS access key"`, `"high-entropy string"`, etc.).

**Semantics — catches NEW secrets, not existing ones.**

When `secretscan_baseline_enabled = True`, `run_secretscan` calls
`filter_against_baseline(findings, cwd)` which drops any finding whose key
appears in the baseline file. Net-new findings — secrets introduced after
the baseline was last refreshed — still trip the gate.

**Fail-open on missing baseline.** A missing baseline file is treated as
an empty key set (no findings filtered). This guarantees that disabling
the baseline by deleting the file cannot silently mask findings.

**CLI: `autodev secretscan baseline`**

The Click command in `cli/commands/secretscan_baseline.py` refreshes the
baseline:

```bash
autodev secretscan baseline [--cwd <path>]
```

Workflow:

1. Operator inspects the current findings (`autodev` runs the gate as
   normal, prints failures).
2. Operator decides which findings are accepted (test fixtures, throwaway
   credentials in `examples/`, etc.).
3. Operator runs `autodev secretscan baseline` to snapshot the *current*
   set of findings into `.autodev/secretscan-baseline.json`.
4. Subsequent gate runs only flag NEW findings vs. the snapshot.
5. The baseline is refreshed whenever new findings are intentionally
   accepted.

**Per-extension entropy thresholds.** `secretscan_per_extension_thresholds`
overrides the default entropy curve baked into
`qa.secretscan._DEFAULT_PER_EXTENSION_ENTROPY`:

| Extension | Default threshold | Rationale |
|-----------|-------------------|-----------|
| `.cpp` / `.cc` / `.cxx` / `.c` / `.h` / `.hpp` / `.hxx` | 5.0 | C/C++ codebases use long camelCase / snake_case identifiers; the global 4.5 threshold over-fires. |
| `.yaml` / `.yml` | 5.5 | Build-manifest hashes are routinely 5.0+ entropy; tighter thresholds for these files would be unusable. |
| (all others) | 4.5 | Default Shannon-entropy threshold. |

`None` (default) means "use the module curve". Operators with
finding-noisy file types can override per extension without disabling the
gate entirely.

### 5.7 Extended-scope critic gate (v0.20.0)

`orchestrator/extended_scope_critic.py` implements a critic-review gate
that fires for any task with a non-empty `Task.extended_scope`. The
architect signals that a task may legitimately touch paths outside its
phase/plan `EDIT_SCOPE` by emitting an `Extended-scope:` block in the
plan markdown; the orchestrator routes the review through
`critic_sounding_board` before `validate_edit_scope_with_critic_review`
admits the paths.

The critic returns one of two RESOLUTION tokens:

- `RESOLUTION: approved-extended-scope` — work proceeds; the resolved
  scope is unioned with `task.extended_scope` (per `dag.py` C1).
- `RESOLUTION: rejected-extended-scope` — `EditScopeViolation` is raised
  with the rejection reason inline; the task is blocked.

Decisions are cached in `plan_manager.metadata['extended_scope_decisions']`
keyed by `(task_id, sorted-extended-scope)` so re-running the validator
does not re-invoke the critic for the same scope signature.

For full coverage of the architect prompt format, the
`validate_edit_scope_with_critic_review` wrapper, the cache strategy, and
the dynamic-scope-expansion repair flow, see
[extended_scope.md](extended_scope.md).

### 5.8 Dependencies

- **asyncio:** Subprocess execution and timeout management.
- **re / math:** Secret scanning (regex patterns, Shannon entropy).
- **mutmut (optional):** Mutation testing gate. `pip install ai-autodev[mutation]` to install.
- **Internal:** `plugins/registry` for `GateResult` and `QAGatePlugin`, `qa/detect` for language detection, `orchestrator/delegation_envelope` for the extended-scope critic envelope.

The mutation-test gate is the only gate with an optional Python
dependency. The other gates invoke external CLI tools (ruff, eslint,
pytest, etc.) as subprocesses, but these are not Python package
dependencies.

### 5.9 Configuration

From `.autodev/config.json`:

```json
{
  "qa_gates": {
    "syntax_check": true,
    "lint": true,
    "build_check": true,
    "test_runner": true,
    "secretscan": true,
    "secretscan_baseline_enabled": false,
    "secretscan_per_extension_thresholds": null,
    "sast_scan": false,
    "mutation_test": false,
    "mutation_test_enabled": false,
    "mutation_test_threshold": 0.7
  },
  "qa_retry_limit": 3
}
```

Each gate can be individually enabled or disabled. `sast_scan` and
`mutation_test` (without the `_enabled` suffix) are planning-time
advisories consumed by agent prompts (e.g. `architect.md` for security
tier routing) and do NOT dispatch as gates. Use `mutation_test_enabled`
to actually run the mutation gate.

## 6. Integration Points

### 6.1 Dependencies on Other Components

| Component | Dependency |
|-----------|------------|
| `plugins/registry.py` | `GateResult`, `QAGatePlugin`, `QAContext` types |
| `config/schema.py` | `QAGatesConfig` for per-gate toggles |

### 6.2 Adapter Contract Dependency

The QA gates do not depend on any adapter. They are pure local checks.

### 6.3 Ledger Event Emissions

The QA gates do not write to the ledger directly. The orchestrator's execute phase handles state transitions:
- On gate success: `update_task_status(task_id, "auto_gated")`
- On gate failure: retry via `_try_retry_or_escalate` (which may emit `update_task_status` entries)

### 6.4 Components That Depend on This

| Consumer | Usage |
|----------|-------|
| `orchestrator/execute_phase.py` | `_run_qa_gates()` calls all enabled gates sequentially |
| Plugin system | Third-party `QAGatePlugin` implementations discovered via entry-points |

### 6.5 External Systems

- **Local CLI tools:** ruff, eslint, cargo, go, pytest, npm, node, tsc, golangci-lint. Availability depends on the user's environment.
- **Filesystem:** Gates read source files for syntax checking and secret scanning. Subprocesses operate on the repository working directory.

## 7. Testing Strategy

### 7.1 Unit Tests

- `detect_language`: each manifest file type detected correctly; no manifest returns None; priority ordering verified.
- `detect_toolchain`: language-to-tool mapping for all supported languages; Java gradle override.
- `run_secretscan`: detection of each secret pattern type; entropy threshold; file filtering (skip dirs/extensions); no false positives on safe files.
- `_shannon_entropy`: known entropy values for uniform and biased distributions.
- `GateResult` construction and field defaults.

### 7.2 Integration Tests

- Full pipeline with a Python project fixture: all gates pass on clean code.
- Pipeline with syntax error: `syntax_check` fails, subsequent gates are not run.
- Pipeline with missing tools (mock `FileNotFoundError`): gates pass gracefully.
- Pipeline with timeout (mock slow subprocess): gate returns failure.
- Plugin discovery: register a mock `QAGatePlugin` via test entry-point and verify it is discovered.

### 7.3 Property-Based Tests

- Hypothesis strategy for `_shannon_entropy`: entropy of a string of N identical characters is 0; entropy increases with character diversity.
- Hypothesis strategy for `detect_language`: creating any supported manifest file causes the correct language to be detected.

### 7.4 Test Data Requirements

- Fixture projects for each supported language (Python, Node.js, Rust, Go) with valid and invalid source files.
- Files containing known secret patterns for secretscan testing.
- Files with high-entropy strings that should and should not trigger.

## 8. Security Considerations

- **Secret detection:** The secretscan gate is the primary defense against accidentally committing secrets. It runs before the reviewer sees the code, catching secrets at the earliest possible point.
- **Subprocess sandboxing:** Gates run subprocesses with captured stdout/stderr. They do not execute arbitrary user code -- only well-known CLI tools with fixed arguments. The `cwd` is always the repository root.
- **No network access:** All gates are local. No data is sent to external services.
- **Entropy false positives:** High-entropy detection may flag legitimate random strings (UUIDs, hashes). The 4.5-bit threshold and 20-char minimum length are tuned to reduce false positives, but operators may need to adjust or disable the entropy scan for hash-heavy codebases.

## 9. Performance Considerations

- **Total pipeline time:** Typically 5-30 seconds depending on project size and which tools are available. Each gate has a 60-second default timeout.
- **Fail-fast benefit:** If syntax_check fails (usually <1s), the pipeline exits without running the more expensive lint, build, and test gates.
- **Secret scan I/O:** The in-process scanner reads all text files under `cwd` (excluding filtered directories). For large codebases, this is bounded by filesystem I/O. The `_SKIP_DIRS` and `_SKIP_EXTENSIONS` filters exclude noise directories (`node_modules`, `.git`, `.venv`).
- **No LLM latency:** Zero network calls. Gates are limited only by local tool execution time.

## 10. Installation & CLI Entry

### 10.1 Package Registration

The QA gates are an internal library package under `src/qa/`. No standalone CLI entry points.

### 10.2 CLI Commands

No direct CLI commands. Gates are triggered automatically during `autodev run` as part of the execute phase. Gate behavior is controlled via `.autodev/config.json`:

```bash
# Disable lint gate
autodev config set qa_gates.lint false

# Full run with all gates enabled (default)
autodev run
```

## 11. Observability

### 11.1 Structured Logging

| Event | Key Fields | Description |
|-------|------------|-------------|
| `execute_phase.qa_gate_failed` | `task_id`, `details` | First failing gate's output |
| (gate-level logging is minimal; gates report via `GateResult`) | | |

### 11.2 Audit Artifacts

Gate results are not persisted as separate artifacts. They are captured in the orchestrator's retry flow:
- Gate failure details appear in the `last_issues` context passed to the developer on retry.
- If a task is escalated, the gate failure reason appears in the `CriticEvidence` and the task's `blocked_reason`.

### 11.3 Status Command

`autodev status` does not display QA gate history directly. Gate outcomes are visible through task status transitions (e.g., a task stuck at `coded` with `retry_count > 0` indicates gate failures).

## 12. Cost Implications

| Operation | LLM Calls | Notes |
|-----------|-----------|-------|
| syntax_check | 0 | Local subprocess |
| lint | 0 | Local subprocess |
| build_check | 0 | Local subprocess |
| test_runner | 0 | Local subprocess |
| secretscan | 0 | In-process regex + entropy |
| hallucination_guard | 0 | In-process AST walk |
| mutation_test | 0 | mutmut subprocess + JSON parse |
| extended-scope critic | 1 | Single `critic_sounding_board` invocation per task with non-empty `extended_scope`; cached per `(task_id, scope_signature)` to make re-runs free |
| **Total per pipeline run** | **0–N** | N = number of tasks with new extended-scope blocks; cached after first review |

The non-LLM gates save costs by catching issues locally before they reach
the reviewer agent or tournament engine. A syntax error caught by
`syntax_check` costs 0 LLM calls; the same error caught by the reviewer
would cost at least 1 reviewer call + the retry developer call.

The extended-scope critic is the only gate that consumes an LLM call,
and it is gated on the rare case where a task crosses its declared
edit-scope. The cache (keyed by `scope_signature`) ensures the cost is
paid at most once per `(task_id, extended_scope)` pair across the run.

## 13. Future Enhancements

- **SAST scanning gate dispatch:** Integrate `bandit` (Python), `semgrep`, or similar static analysis tools. The `sast_scan` flag is currently a planning-time advisory consumed by agent prompts only — the dispatcher hook is reserved per ADR-008.
- **Tree-sitter integration:** Replace `py_compile` / `node --check` with tree-sitter grammars for faster, language-agnostic syntax validation without requiring the language runtime.
- **Per-file gating beyond diff scope:** v0.13.0+ already passes diff-scope paths to `secretscan` / `mutation_test` / `hallucination_guard`; extending the same pattern to `lint` and `build_check` would give faster feedback on large codebases.
- **Configurable global entropy threshold:** Expose the global `_ENTROPY_THRESHOLD` in `QAGatesConfig` (per-extension is already exposed via `secretscan_per_extension_thresholds`).
- **Gate result persistence:** Write `GateResult` objects as evidence bundles for full auditability.
- **Parallel gate execution:** For independent gates (e.g., secretscan does not depend on lint), run in parallel to reduce total pipeline time.
- **Mutation-test equivalence Stage 1 wiring:** The `_stage1_static_filter` hook in `qa/mutation_test.py` is currently a no-op — the per-mutant text extraction depends on internal `mutmut` APIs that are not yet stable across versions. Wiring the static AST filter once a stable surface lands.

## 14. Open Questions

- [ ] Should gate results be persisted as evidence bundles for full auditability?
- [ ] Should the pipeline support running gates only on changed files (diff-scoped)?
- [ ] Should the entropy threshold for secret scanning be configurable?
- [ ] Should gates have a retry mechanism of their own (e.g., flaky test re-run)?

## 15. Related ADRs

No specific ADRs have been created for the QA gates pipeline yet. Candidates:

- ADR: Sequential fail-fast vs. parallel gate execution
- ADR: Graceful degradation policy for missing tools

## 16. References

- [ruff documentation](https://docs.astral.sh/ruff/)
- [eslint documentation](https://eslint.org/)
- [Shannon entropy](https://en.wikipedia.org/wiki/Entropy_(information_theory))
- [py_compile module](https://docs.python.org/3/library/py_compile.html)
- `plugins/registry.py` -- `QAGatePlugin` protocol definition

## 17. Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-04-17 | Mohamed Ameen | Initial draft |
| 2026-05-09 | Mohamed Ameen | v0.21.1 refresh: documented v0.19.0 mutation-test gate, secretscan baseline + per-extension entropy thresholds, hallucination-guard, and v0.20.0 extended-scope critic gate. Updated `QAGatesConfig`, pipeline entry, gate list, and architecture diagram. |
