# ADR-007: Git Worktree Isolation for Implementation Tournaments

**Status:** Accepted
**Date:** 2026-04-17
**Deciders:** Mohamed Ameen
**Tags:** tournaments, git-worktree, isolation, impl-tournament, concurrency
**Related ADRs:** ADR-006 (Platform Adapter Abstraction)

## Context

The implementation tournament (autoreason self-refinement) produces three variant implementations per pass: the incumbent (A), a revised version (B), and a synthesis of both (AB). Each variant must exist as a real on-disk file state so that:

1. **Independent testing**: Each variant runs its test suite in its own working directory. Test artifacts, generated files, and build outputs from one variant must not contaminate another.
2. **Clean diffing**: The tournament judges evaluate unified diffs (`git diff HEAD`) for each variant. These diffs must reflect only the variant's changes, not leftover state from another variant.
3. **Concurrent execution**: The developer agent can work on variant B while tests run on variant A. The judge evaluates all three in parallel.
4. **Atomic application**: The winning variant's diff is applied to the main repository via `git apply`. Losing variants leave no trace.
5. **Safe cleanup**: After the tournament, all variant state is removed. A mid-tournament crash (SIGKILL) must not leave the main repository in a dirty state.

The variants live under `.autodev/tournaments/impl-{task_id}/` with subdirectories `a/`, `b/`, and `ab/`. Each subdirectory is a complete working tree that the developer agent, test engineer, and judges operate on independently.

## Options Considered

### Option A: Git Stash/Branch Switching (Sequential, Single Working Directory)

**Description:**
Use the main repository's single working directory. Before creating variant B, stash variant A's changes (`git stash`). After B is created, stash B and pop A. Repeat for AB. Judging happens sequentially: restore each variant, compute diff, score, restore next.

**Pros:**
- Zero disk overhead beyond the existing repository
- No new git concepts: stash and branch are familiar to every developer
- Simplest possible implementation (~30 lines)

**Cons:**
- **No concurrency**: Only one variant can exist at a time. The developer cannot work on B while A's tests run. Judges cannot evaluate variants in parallel
- **Fragile state management**: Stash/pop operations can fail silently when untracked files conflict. A crash between stash and pop leaves the working directory in an undefined state
- **Test contamination**: Build artifacts (`.pyc`, `node_modules/.cache`, `__pycache__`) persist across stash/pop cycles, potentially causing false positives/negatives
- **Blocks the main repo**: The developer's working directory is occupied by the tournament. If the user inspects the repo mid-tournament, they see tournament state, not their code
- **No parallel judging**: Judges must evaluate sequentially, adding 3x latency to the judging step

### Option B: Full Repository Clones (`cp -r`)

**Description:**
Create full copies of the repository for each variant: `cp -r <repo> .autodev/tournaments/impl-{id}/a/`, etc. Each clone is a complete, independent git repository.

**Pros:**
- **Full isolation**: Each variant has its own `.git/` directory, objects, refs, and working tree. Zero possibility of cross-contamination
- **Conceptually simple**: a copy is a copy; no git-specific knowledge required
- **Parallelism**: All three variants can be worked on and tested concurrently

**Cons:**
- **Disk explosion**: For a repository with 500 MB of git objects, three clones consume 1.5 GB of additional disk. Multiply by concurrent tournaments and disk usage becomes prohibitive
- **Clone latency**: `cp -r` of a large `.git/` directory can take 10+ seconds. `git clone --local` is faster (hardlinks objects) but still copies refs and config
- **Diff complexity**: Computing a diff between the clone and the original requires cross-repository `git diff`, which is error-prone and requires explicit `--git-dir` / `--work-tree` juggling
- **Cleanup overhead**: Removing 3 full repository copies is slower than removing 3 lightweight worktrees
- **No object sharing**: If the tournament makes commits (e.g., for `git apply` preflight), objects are duplicated across clones rather than shared

### Option C: Git Worktree Add --detach (Current Choice)

**Description:**
Use `git worktree add --detach <path> HEAD` to create lightweight working directories that share the main repository's git object store. Each worktree is a separate on-disk file state pointing at the same `HEAD` commit. The `--detach` flag avoids creating named branches (which would conflict across parallel tournaments).

Worktrees live at `.autodev/tournaments/impl-{task_id}/a/`, `b/`, and `ab/`. The `WorktreeManager` class handles creation, diff computation (including untracked files), patch application to the main repo, and cleanup.

Cleanup uses a three-phase strategy: `git worktree remove` > `git worktree remove --force` > `shutil.rmtree` + `git worktree prune`. This ensures cleanup succeeds even when worktrees have uncommitted changes or corrupted metadata.

**Pros:**
- **Disk-efficient**: Worktrees share the git object store with the main repository. Each worktree adds only the working tree files (typically 1-10% of the total repository size, depending on object store vs working tree ratio)
- **Full isolation**: Each worktree has its own `HEAD`, index, and working tree. File changes in worktree B are invisible to worktree A
- **Concurrent execution**: Developer agent, test engineer, and judges can all operate on different worktrees simultaneously
- **Native diffing**: `git diff --no-color HEAD` from within a worktree produces the correct unified diff against the tournament's base commit. No cross-repository juggling
- **Atomic application**: `git apply --check` + `git apply` from the main repo applies the winning diff cleanly. The preflight check ensures conflicts are detected before the apply
- **Detached mode**: `--detach` avoids branch name collisions. Worktrees are ephemeral; named branches would accumulate and require cleanup
- **Built-in cleanup**: `git worktree remove` handles the git admin database; `git worktree prune` cleans stale entries
- **Robust error recovery**: Three-phase cleanup (remove > force remove > rmtree + prune) handles every failure mode: uncommitted changes, locked files, corrupted metadata

**Cons:**
- **Git version requirement**: `git worktree` was added in Git 2.5 (2015). Ancient systems may not support it, though this is vanishingly unlikely in 2026
- **Admin database locking**: Git's worktree admin database (`.git/worktrees/`) uses file locks. On rare occasions, a crash can leave a stale lock that blocks `git worktree remove`. Mitigated by the force-remove + prune fallback
- **Untracked file handling**: `git diff HEAD` only shows tracked-and-modified files. New files created by the developer agent require a separate `git ls-files --others --exclude-standard` + `git diff --no-index /dev/null <file>` pass. This adds implementation complexity
- **Shared object store race**: If two worktrees simultaneously run `git gc` or `git repack`, they contend on the shared object store. Mitigated by AutoDev not running gc during tournaments
- **Complexity vs stash**: More code (~200 lines for `WorktreeManager`) compared to ~30 lines for stash-based approach

## Decision Drivers

- **Crash Safety:** A mid-tournament SIGKILL must not corrupt the main repository. Worktrees are isolated; the main repo's working directory is untouched during the tournament. The three-phase cleanup handles interrupted operations.
- **Stateless Reproducibility:** Each worktree starts from a detached `HEAD` pointing at the same base commit. Variants begin with identical state and diverge only through the developer agent's edits.
- **Asyncio-Friendliness:** All git operations in `WorktreeManager` use `asyncio.create_subprocess_exec` with configurable timeouts (default 60s). No blocking calls.
- **Testability:** `WorktreeManager` accepts `main_repo` and `tournament_dir` as Path parameters, making it testable with `tmp_path` fixtures without touching the real repository.
- **LLM Cost Efficiency:** Parallel worktrees enable concurrent judge execution. With 3 judges and `parallel()`, judging latency is 1x (not 3x), reducing wall-clock time and subscription cost on per-seat platforms.

## Architecture Drivers Comparison

| Architecture Driver          | Option A: Stash/Branch | Option B: Full Clones | Option C: Git Worktrees | Notes |
|------------------------------|------------------------|-----------------------|-------------------------|-------|
| **Crash Safety**             | ⭐ (1/5)              | ⭐⭐⭐⭐ (4/5)       | ⭐⭐⭐⭐⭐ (5/5)       | A: stash/pop crash corrupts working dir. B: clones are independent. C: worktrees are independent + main repo untouched |
| **Disk Efficiency**          | ⭐⭐⭐⭐⭐ (5/5)      | ⭐ (1/5)             | ⭐⭐⭐⭐ (4/5)          | A: zero overhead. B: 3x repo size. C: 3x working tree only (shared objects) |
| **Concurrent Execution**     | ⭐ (1/5)              | ⭐⭐⭐⭐⭐ (5/5)     | ⭐⭐⭐⭐⭐ (5/5)       | A: sequential only. B/C: fully parallel |
| **Testability**              | ⭐⭐ (2/5)            | ⭐⭐⭐ (3/5)         | ⭐⭐⭐⭐⭐ (5/5)       | C: WorktreeManager is a testable class. A: global stash state is hard to test. B: requires real repo copies |
| **Diffability**              | ⭐⭐⭐ (3/5)          | ⭐⭐ (2/5)           | ⭐⭐⭐⭐⭐ (5/5)       | C: native `git diff HEAD` per worktree. A: must unstash to diff. B: cross-repo diff is awkward |
| **Cleanup Reliability**      | ⭐⭐ (2/5)            | ⭐⭐⭐ (3/5)         | ⭐⭐⭐⭐ (4/5)          | C: three-phase fallback. A: orphaned stash entries. B: large rmtree operations |
| **Stateless Reproducibility**| ⭐⭐ (2/5)            | ⭐⭐⭐⭐ (4/5)       | ⭐⭐⭐⭐⭐ (5/5)       | C: each worktree starts from detached HEAD. A: stash history leaks. B: clone inherits full state |
| **Complexity**               | ⭐⭐⭐⭐⭐ (5/5)      | ⭐⭐⭐⭐ (4/5)       | ⭐⭐⭐ (3/5)            | A: ~30 LOC. B: ~50 LOC. C: ~200 LOC for WorktreeManager |
| **Asyncio Compatibility**    | ⭐⭐ (2/5)            | ⭐⭐⭐ (3/5)         | ⭐⭐⭐⭐⭐ (5/5)       | C: all git calls via asyncio.create_subprocess_exec. A: stash is inherently sequential |

**Rating Scale:**
- ⭐⭐⭐⭐⭐ (5/5) - Excellent
- ⭐⭐⭐⭐ (4/5) - Good
- ⭐⭐⭐ (3/5) - Average
- ⭐⭐ (2/5) - Below Average
- ⭐ (1/5) - Poor

## Decision Outcome

**Chosen Option:** Option C: Git Worktree Add --detach

**Rationale:**
The implementation tournament is the performance-critical path of AutoDev: it runs after every developer task and adds 1-3 passes of critic/architect/developer/judge cycles. Minimizing wall-clock time is essential both for user experience and for LLM cost (subscription platforms charge per-seat, not per-call, so wall-clock time matters).

Git worktrees provide the only option that achieves all three of: disk efficiency (shared objects), full isolation (independent working trees), and concurrent execution (parallel judging). The stash-based approach (Option A) fails on concurrency and crash safety -- the two most critical drivers. Full clones (Option B) fail on disk efficiency and scale poorly as repository size grows.

The added implementation complexity (~200 lines for `WorktreeManager`) is justified by the robustness guarantees: three-phase cleanup, async git operations with timeouts, preflight `git apply --check` before patch application, and explicit untracked-file handling via `ls-files --others`.

**Key Factors:**
- Concurrent execution of developer, test engineer, and judges across worktrees reduces tournament wall-clock time by up to 3x
- Shared git object store keeps disk overhead proportional to working tree size, not repository history size
- `--detach` mode avoids branch-name management and works cleanly with ephemeral tournament lifecycles
- Three-phase cleanup (remove > force remove > rmtree + prune) handles every failure mode, including SIGKILL mid-tournament
- `git apply --check` preflight prevents the main repo from being left half-patched by a conflicting diff

## Consequences

### Positive Consequences
- Tournament passes execute with true parallelism: developer agent writes to `/b` while tests run on `/a`, judges evaluate all three concurrently
- The main repository's working directory is never modified during the tournament. Users can inspect their code mid-tournament without interference
- Losing variants are removed cleanly with no trace in the main repo. The winner is applied via `git apply`, which is atomic (either the full diff applies or nothing does)
- `WorktreeManager.get_diff_vs_base()` produces correct diffs including untracked files, which is essential for tournament judges to evaluate the full scope of changes
- `cleanup_all()` is idempotent: calling it twice is safe, and it handles partially-cleaned state from interrupted operations

### Negative Consequences / Trade-offs
- `WorktreeManager` is ~200 lines of git subprocess orchestration, compared to ~30 lines for a stash-based approach. This adds maintenance surface area
- Untracked file handling requires a two-pass diff strategy: `git diff HEAD` for tracked files + `git diff --no-index /dev/null <file>` for each untracked file. This is correct but adds per-file subprocess overhead for new files
- The `git worktree` admin database can occasionally deadlock under extreme concurrent operations (multiple tournaments running simultaneously with rapid create/remove cycles). Mitigated by the force-remove + prune fallback
- Requires Git 2.5+ (released July 2015). Not expected to be a real constraint, but is a hard dependency

### Neutral / Unknown Consequences
- Whether tournament worktrees should commit their changes (for better `git diff` and `git log` support) or remain as uncommitted working tree modifications. Currently they remain uncommitted, which simplifies cleanup but requires the `--no-index /dev/null` workaround for untracked files
- Whether `WorktreeManager` should support a "warm worktree" pool that pre-creates worktrees before the tournament starts, to amortize creation latency. Currently, worktrees are created on-demand

## Implementation Notes

**Files Affected:**
- `src/orchestrator/worktree.py` -- `WorktreeManager` class with `create()`, `remove()`, `cleanup_all()`, `get_diff_vs_base()`, `apply_patch_to_main()`, plus `_run_git()` async subprocess helper and `WorktreeError` exception class
- `src/orchestrator/worktree.py` is used by the impl tournament runner, which creates a `WorktreeManager(main_repo=repo_path, tournament_dir=.autodev/tournaments/impl-{task_id}/)` and calls `create("a")`, `create("b")`, `create("ab")` before each pass

**Ledger/State Implications:**
- Tournament artifacts (diffs, scores, history.json) are written under `.autodev/tournaments/impl-{task_id}/` alongside the worktree directories. These persist after cleanup for post-mortem analysis
- The worktree directories themselves (`a/`, `b/`, `ab/`) are ephemeral and removed by `cleanup_all()` after the tournament completes
- No changes to the JSONL ledger schema. Tournament results are recorded as standard ledger entries with `operation="impl_tournament_pass"`

**General Guidance:**
- Always call `cleanup_all()` in a `finally` block to ensure worktrees are removed even on exception
- Use `base_ref="HEAD"` (default) for impl tournaments. Using a specific commit hash would be needed for reproducible replays
- The `timeout_s=60.0` default on `_run_git()` is generous for local operations. Consider lowering it for CI environments where git operations should be near-instant
- Never run `git gc` or `git repack` while worktrees are active -- it can corrupt the shared object store

## Evidence from Codebase

**Source References:**
- `src/orchestrator/worktree.py:37-67` -- `WorktreeManager.__init__` and `worktree_path()` establishing the directory layout convention
- `src/orchestrator/worktree.py:71-95` -- `create()` using `git worktree add --detach <path> <base_ref>` with existence check and error handling
- `src/orchestrator/worktree.py:97-143` -- `remove()` with three-phase cleanup: normal remove > force remove > rmtree + prune. `_force_remove()` handles the filesystem fallback
- `src/orchestrator/worktree.py:145-171` -- `cleanup_all()`: iterates all label subdirs, removes each, then rmtrees the entire tournament directory and prunes stale worktree metadata
- `src/orchestrator/worktree.py:175-226` -- `get_diff_vs_base()`: two-pass diff strategy with `git diff --no-color HEAD` for tracked files and `git diff --no-index /dev/null <file>` for each untracked file via `_list_untracked()`
- `src/orchestrator/worktree.py:228-266` -- `apply_patch_to_main()`: preflight `git apply --check` followed by `git apply` with stdin pipe. Raises `WorktreeError` on conflict
- `src/orchestrator/worktree.py:272-312` -- `_run_git()` async subprocess helper: `asyncio.create_subprocess_exec` with optional stdin, configurable timeout, and `WorktreeError` on timeout/launch failure

**Test Coverage:**
- The `WorktreeManager` class is tested via integration with the impl tournament runner. Specific unit tests for worktree creation/removal/diffing are planned for Phase 4

**Property-Based Tests (Hypothesis):**
- N/A -- git worktree operations are inherently side-effectful and not amenable to property-based testing. Integration tests with `tmp_path` git repos provide better coverage.

## Related Design Documents

- [tournaments.md](../../docs/design_documentation/tournaments.md) -- Describes the impl tournament flow that creates worktrees for A/B/AB variants, runs judges in parallel, and applies the winning diff. Includes configuration parameters (`max_rounds`, `convergence_k`) and cost analysis
- [adapters.md](../../docs/design_documentation/adapters.md) -- Platform adapters execute agent invocations within worktree `cwd` directories. The `AgentInvocation.cwd` field points to the worktree path, not the main repo
- [cost.md](../../docs/design_documentation/cost.md) -- Cost guardrails that bound tournament rounds and parallel subprocess count, directly affecting how many worktrees are active simultaneously

## Monitoring and Review

- [ ] Review date: 2026-10-17
- [ ] Success criteria: Impl tournaments complete without orphaned worktrees. `cleanup_all()` succeeds in >99% of cases without falling back to `shutil.rmtree`
- [ ] Metrics to track: Worktree creation latency, `_force_remove` fallback frequency, disk usage of `.autodev/tournaments/` across user sessions, `git worktree prune` invocation frequency

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-04-17 | Mohamed Ameen | Initial ADR created |
