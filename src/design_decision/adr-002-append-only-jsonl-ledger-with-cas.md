# ADR-002: Append-Only JSONL Ledger with CAS Hash Chaining

**Status:** Accepted
**Date:** 2026-04-17
**Deciders:** Mohamed Ameen
**Tags:** state, ledger, crash-safety, append-only, hash-chain, JSONL, CAS
**Related ADRs:** ADR-001 (stateless adapters rely on this ledger for continuity)

## Context

AutoDev's orchestrator drives a multi-phase FSM that produces plans, dispatches agent invocations, records evidence, and transitions task statuses. This state must survive process crashes (SIGKILL mid-write), support concurrent writers (parallel agent completions), enable full audit replay, and remain human-debuggable without specialized tooling.

The state subsystem (`src/state/`) is responsible for persisting all plan mutations. The `PlanManager` facade exposes methods like `init_plan()`, `save()`, `update_task_status()`, and `mark_escalated()` -- each of which must atomically record the mutation so that a crash between steps does not corrupt the plan or leave it in an inconsistent state.

The design must also support deterministic replay: given the ledger file, an operator must be able to reconstruct the exact plan state at any point in the execution history, which is essential for debugging, auditing, and crash recovery.

## Options Considered

### Option A: SQLite with WAL Mode

**Description:**
Store all plan state in a SQLite database using Write-Ahead Logging (WAL) mode. Each mutation is a SQL transaction. The `plan` table stores the current plan; a `ledger` table stores an append-only audit trail. SQLite's WAL mode provides crash-safe writes and concurrent reader access.

**Pros:**
- Mature, battle-tested crash-safety guarantees via SQLite's WAL
- Built-in support for concurrent readers and single-writer serialization
- Rich query capabilities (e.g., "show all status transitions for task X")
- Compact binary format with automatic page-level compression
- Schema enforcement at the database level

**Cons:**
- Adds a binary dependency (sqlite3 C library); while Python bundles it, version pinning across platforms is non-trivial
- Binary file format is not human-readable; debugging requires `sqlite3` CLI or a viewer
- WAL files can grow large under sustained writes; checkpoint timing adds operational complexity
- Harder to `diff` or `grep` in a git repository; merge conflicts are irresolvable at the file level
- Replay requires re-executing SQL queries rather than simply streaming lines
- Overkill for the current write pattern (sequential appends, rare reads)

### Option B: Single JSON File with Overwrite

**Description:**
Maintain a single `plan.json` file that is overwritten atomically (via `tmp + os.replace`) on every mutation. No separate audit trail; the current state is always the file's content. A backup copy (`plan.json.bak`) is saved before each write.

**Pros:**
- Simplest possible implementation (read-modify-write cycle)
- Always contains the current plan state; no replay needed
- Human-readable JSON with pretty-printing
- Trivial to restore from `.bak` file on corruption

**Cons:**
- No audit trail: once overwritten, previous states are lost forever
- Crash between read and write leaves the plan in an unknown state (last-writer-wins race)
- No way to detect tampering or out-of-order writes
- Cannot reconstruct the sequence of mutations for debugging
- Concurrent writers cause silent data loss (last overwrite wins)
- No support for deterministic replay or crash-recovery beyond "restore .bak"

### Option C: Append-Only JSONL with CAS Hash Chain

**Description:**
All plan mutations are appended as individual JSON lines to a `.autodev/ledger.jsonl` file. Each entry has a monotonically increasing `seq` number, a `self_hash` computed over its content (SHA-256 prefix), and a `prev_hash` pointing to the previous entry's `self_hash`. The genesis entry has `prev_hash == ""`. Writes use atomic `tempfile + os.replace` (copy existing content to temp, append new line, fsync, replace). A separate `plan.json` snapshot is written periodically for fast loading, but the ledger is the source of truth.

**Pros:**
- Crash-safe: `os.replace` is atomic on all target platforms; either the new file is visible or nothing changed
- Full audit trail: every mutation is preserved with timestamp, session ID, and operation type
- Tamper-detectable: broken `prev_hash` or `self_hash` chains are caught during `read_entries()` validation
- Human-readable: each line is valid JSON that can be inspected with `cat`, `jq`, or any text editor
- Deterministic replay: `replay_ledger()` reconstructs the exact plan state by applying ops in sequence
- Zero external dependencies: uses only stdlib `json`, `hashlib`, `os`, `tempfile`
- Git-friendly: append-only JSONL has clean diffs and no binary merge conflicts
- Snapshot optimization: `snapshot_plan()` embeds the full plan so replay can skip to the last snapshot

**Cons:**
- File grows monotonically; very long-running projects accumulate large ledger files (mitigated by snapshot-based replay short-circuiting)
- Atomic append via copy-then-replace is O(n) in file size; degrades for very large ledgers (mitigated by the snapshot fast-path in `PlanManager._load_sync`)
- No built-in query capability beyond sequential scan (acceptable given the small number of entries per run)
- Hash chain validation is O(n) on full reads; must scan all entries to verify integrity

## Decision Drivers

- **Crash Safety:** Process-kill resilience, atomic writes, no corrupted state after SIGKILL
- **Auditability:** Every mutation must be traceable to a session, timestamp, and operation type
- **Stateless Reproducibility:** Replay from genesis must reconstruct the identical plan state
- **Debuggability:** An operator with `cat` and `jq` must be able to inspect the full mutation history
- **Simplicity:** Zero external dependencies; no database setup, migration, or schema management
- **Pydantic-Boundary Strictness:** `extra="forbid"` at every public boundary; fail fast on bad data
- **Determinism:** Same ledger file produces the same plan state on replay

## Architecture Drivers Comparison

| Architecture Driver        | Option A: SQLite WAL | Option B: Single JSON | Option C: JSONL + CAS | Notes |
|----------------------------|----------------------|-----------------------|-----------------------|-------|
| **Crash Safety**           | ⭐⭐⭐⭐⭐              | ⭐⭐                   | ⭐⭐⭐⭐⭐               | A: SQLite WAL is industry-standard; B: overwrite can lose data; C: tempfile+replace is atomic |
| **Auditability**           | ⭐⭐⭐⭐               | ⭐                    | ⭐⭐⭐⭐⭐               | A: separate ledger table; B: no history; C: every line is a permanent audit record |
| **Debuggability**          | ⭐⭐                  | ⭐⭐⭐⭐⭐               | ⭐⭐⭐⭐⭐               | A: binary format needs tools; B: readable JSON; C: readable JSONL with `cat`/`jq` |
| **Deterministic Replay**   | ⭐⭐⭐                 | ⭐                    | ⭐⭐⭐⭐⭐               | A: requires SQL replay logic; B: no history to replay; C: `replay_ledger()` walks the chain |
| **Simplicity**             | ⭐⭐                  | ⭐⭐⭐⭐⭐               | ⭐⭐⭐⭐                | A: DB dependency + schema; B: simplest; C: slightly more complex than B but far simpler than A |
| **Tamper Detection**       | ⭐⭐                  | ⭐                    | ⭐⭐⭐⭐⭐               | A: no built-in chain; B: none; C: SHA-256 hash chain detects any modification |
| **Pydantic Strictness**    | ⭐⭐⭐                 | ⭐⭐⭐                  | ⭐⭐⭐⭐⭐               | C: every entry is validated via `LedgerEntry.model_validate` with `extra="forbid"` |
| **Concurrent Writers**     | ⭐⭐⭐⭐               | ⭐                    | ⭐⭐⭐⭐                | A: SQLite handles locking; B: last-writer-wins; C: `plan_lock` serializes writers, hash chain detects races |
| **Git Friendliness**       | ⭐                   | ⭐⭐⭐⭐                | ⭐⭐⭐⭐⭐               | A: binary; B: full-file overwrites; C: append-only has minimal diffs |

## Decision Outcome

**Chosen Option:** Option C: Append-Only JSONL with CAS Hash Chain

**Rationale:**
Option C provides the best balance across the drivers that matter most: crash safety, auditability, debuggability, and deterministic replay. While SQLite (Option A) matches on crash safety, it fails on debuggability and adds an unnecessary dependency. Single-JSON (Option B) is simpler but sacrifices auditability and crash safety -- the two most critical requirements for an orchestrator that may be interrupted at any point.

The hash-chain mechanism (CAS) adds tamper detection at near-zero cost: each append computes a 16-char SHA-256 prefix and stores it alongside `prev_hash`. This transforms the JSONL file from a simple log into a verifiable chain where any modification (deletion, insertion, or edit of a middle entry) is detected during `read_entries()`.

**Key Factors:**
- The `_atomic_append` function (copy-existing + append + fsync + os.replace) ensures that a SIGKILL at any point leaves either the old file intact or the new file complete. There is no window where a partial line can appear.
- The `LedgerEntry` model uses `extra="forbid"` via Pydantic, rejecting unknown fields at the boundary.
- The snapshot mechanism (`snapshot_plan`) provides O(1) load for the common case while preserving the full audit trail for debugging and recovery.
- File-lock serialization (`plan_lock`) prevents concurrent writers from interleaving, and the hash chain provides a secondary integrity check.

## Consequences

### Positive Consequences
- Full crash recovery: `replay_ledger()` reconstructs the plan from any valid ledger file, regardless of whether `plan.json` was written.
- Complete audit trail: every `update_task_status`, `mark_blocked`, `mark_complete`, `append_evidence`, and `snapshot` is preserved with session ID and timestamp.
- Human debugging: `cat .autodev/ledger.jsonl | jq .` shows the entire mutation history in readable form.
- Tamper detection: `read_entries()` validates the full hash chain on every read, raising `LedgerCorruptError` with actionable recovery instructions on any integrity violation.
- Snapshot optimization: `PlanManager._load_sync()` walks backwards to find the last `snapshot` entry and only replays subsequent ops, making loads fast even for long ledgers.

### Negative Consequences / Trade-offs
- The atomic-append strategy is O(n) in file size because it copies the entire existing file to a temp file before appending. For very large ledgers (thousands of entries), this degrades. In practice, AutoDev runs produce 10-100 entries, well within acceptable bounds.
- No random-access query: answering "what was task 1.3's status at seq 47?" requires sequential scan. This is acceptable because such queries are rare (debugging only).
- The `plan.json` snapshot can drift from the ledger if a crash occurs between the snapshot write and the ledger append. This is tolerable because the ledger is the source of truth; the next successful snapshot re-synchronizes them.

### Neutral / Unknown Consequences
- If AutoDev is extended to support very long-running projects (hundreds of phases), ledger file size may become a concern. A future compaction mechanism (write a new genesis snapshot, archive old entries) could address this without changing the current API.
- The 16-character hash prefix provides ~64 bits of collision resistance, which is sufficient for integrity checking but not for cryptographic tamper-proofing.

## Implementation Notes

**Files Affected:**
- `src/state/ledger.py` - Core ledger implementation: `LedgerEntry` model, `append_entry()`, `read_entries()`, `replay_ledger()`, `snapshot_plan()`, `compute_hash()`, `_atomic_append()`
- `src/state/plan_manager.py` - `PlanManager` facade that acquires `plan_lock` and calls ledger functions; handles snapshot fast-path in `_load_sync()`
- `src/state/lockfile.py` - `plan_lock()` async context manager for serializing writers
- `src/state/paths.py` - `ledger_path()` and `autodev_root()` path resolution
- `src/state/schemas.py` - `Plan`, `Phase`, `Task`, `TaskStatus` Pydantic models consumed by the ledger
- `src/errors.py` - `LedgerCorruptError` and `PlanConcurrentModificationError` exception types

**Ledger/State Implications:**
- `LedgerOp` is a `Literal` union of: `init_plan`, `update_plan`, `update_task_status`, `append_evidence`, `mark_blocked`, `mark_complete`, `snapshot`, `plan_tournament_complete`, `impl_tournament_complete`
- New operations must be added to `LedgerOp`, handled in `_apply_op()`, and handled in `_apply_for_load()` (even if as a no-op for audit-only ops)
- CAS integrity: `self_hash` is computed over all fields except itself; `prev_hash` is the `self_hash` of the previous entry

**General Guidance:**
- Never write to the ledger file directly; always use `append_entry()` which computes the hash chain
- Never read the ledger without validating the chain; always use `read_entries()` which verifies seq, prev_hash, and self_hash
- Always hold `plan_lock` when calling `append_entry()` or `snapshot_plan()`
- New audit-only ops (like `plan_tournament_complete`) should return `plan` unchanged in `_apply_op`

## Evidence from Codebase

**Source References:**
- `src/state/ledger.py:1-24` - Module docstring documenting the hash-chain invariant and supported ops
- `src/state/ledger.py:60-76` - `LedgerEntry` model with `extra="forbid"`, seq, timestamps, prev_hash, self_hash
- `src/state/ledger.py:82-85` - `compute_hash()`: 16-char SHA-256 prefix of canonical sorted JSON
- `src/state/ledger.py:94-126` - `append_entry()`: reads last entry head, computes next seq/prev_hash, validates via Pydantic, atomically appends
- `src/state/ledger.py:164-190` - `_atomic_append()`: copy-existing-to-tmp, append, fsync, os.replace strategy with best-effort cleanup on failure
- `src/state/ledger.py:193-249` - `read_entries()`: validates every entry's JSON, schema, seq monotonicity, prev_hash chain, and self_hash integrity
- `src/state/ledger.py:252-274` - `replay_ledger()`: applies ops in sequence to reconstruct the Plan
- `src/state/plan_manager.py:61-95` - `_load_sync()`: snapshot fast-path walks backward to find last snapshot, applies subsequent entries
- `src/state/plan_manager.py:97-120` - `init_plan()`: appends `init_plan` entry then immediately snapshots
- `src/state/plan_manager.py:162-219` - `update_task_status()`: validates transition, appends entry, applies in-memory, snapshots

**Test Coverage:**
- `tests/test_state_ledger.py::test_genesis_entry_has_empty_prev_hash` - Verifies the chain starts with prev_hash=""
- `tests/test_state_ledger.py::test_hash_chain_links_entries` - Verifies e2.prev_hash == e1.self_hash
- `tests/test_state_ledger.py::test_tampered_middle_entry_detected` - Modifies a payload without updating self_hash; confirms `LedgerCorruptError`
- `tests/test_state_ledger.py::test_concurrent_appends_serialized_under_lock` - Three concurrent writers produce a valid chain [1,2,3,4]
- `tests/test_state_ledger.py::test_replay_reconstructs_plan_from_ops` - Init + two status updates -> replayed plan has correct final status
- `tests/test_state_ledger.py::test_truncated_partial_line_detected` - Simulates SIGKILL mid-write; confirms detection and recovery guidance
- `tests/test_state_ledger.py::test_snapshot_writes_plan_json_and_entry` - Verifies both plan.json and ledger entry are written
- `tests/test_state_ledger.py::test_compute_hash_is_deterministic` - Same content in different key order produces same hash

**Property-Based Tests (Hypothesis):**
- N/A for ledger tests (scenario-based with specific corruption patterns)

## Related Design Documents

- [adapters.md](../../docs/design_documentation/adapters.md) - Adapters are stateless (ADR-001); the ledger is the sole source of continuity
- [tournaments.md](../../docs/design_documentation/tournaments.md) - Tournament results are recorded via `plan_tournament_complete` and `impl_tournament_complete` ledger ops
- [architecture.md](../../docs/architecture.md) - System architecture showing the state subsystem's role as the persistence layer

## Monitoring and Review

- [ ] Review date: 2026-10-17
- [ ] Success criteria: Zero data-loss incidents from process crashes; ledger replay produces correct plan state in all integration tests; no `LedgerCorruptError` in production outside of genuine tampering
- [ ] Metrics to track: Ledger file size distribution; `_atomic_append` latency at various file sizes; frequency of snapshot-based fast-path vs full replay in `_load_sync`

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-04-17 | Mohamed Ameen | Initial ADR created |
