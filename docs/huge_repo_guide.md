# Huge-Repo Operator Guide (v0.23.0+)

This guide covers AutoDev's behavior on **huge** repositories — codebases
where `runtime.repo_probe.RepoCapacity.is_huge` is `True`, defined as:

* **file_count > 20,000** OR
* **total_bytes > 5 GB**

Reference: 2026-05-09 Unity stall (358K files, 3 GB) — the crash that
motivated the v0.22.x and v0.23.0 huge-repo work.

---

## What changes automatically on huge repos

When `is_huge` resolves true, the following defaults activate **without
operator intervention** (each is independently overridable; see knobs
below):

| Subsystem | Default behavior on huge | Knob to override | First shipped |
|---|---|---|---|
| `qa.cpp_symbols` regex | Per-line scan (was multi-line) | n/a — always linear | v0.22.1 A1 |
| `hallucination_guard` watchdog | 10 s/file timeout | `qa_gates.regex_timeout_per_file_s` | v0.22.1 A1 |
| `secretscan` gate | Auto-skipped (warn) | `qa_gates.secretscan_force_run_on_huge_repo=True` | v0.22.1 A2 |
| `git worktree add` timeout | 600 s (was 60 s) | `worktree_huge_create_timeout_s` | v0.22.1 A3 |
| Per-task worktree | Sparse checkout | `worktree_huge_repo_mode="off"` | v0.23.0 C1 |
| Worktree pool size | 2 (was `parallelism`) | `worktree_huge_pool_size` | v0.23.0 C1 |
| Plan tournament | Single-branch | `tournaments.plan.huge_repo_overrides_disabled=True` | v0.23.0 C4 |
| `explorer` agent `max_turns` | 2× (default 3 → 6) | `agents.explorer.max_turns` | v0.23.0 C5 |

---

## Resilience features (huge or not)

These v0.22.x improvements apply to every run; they happen to matter
**most** on huge repos:

* **Resume reaper** (v0.22.2 B1, `PlanManager.reap_orphans()`) — at
  resume, any task wedged in `coded`/`in_progress`/`reviewed`/etc. is
  reverted to `pending` so the dispatcher can re-pick it up.
* **`PhaseStuckError`** (v0.22.2 B2) — replaces the silent return that
  used to mask interrupted FSM states; surfaces wedged task IDs.
* **Atomic evidence ↔ ledger** (v0.22.3 B3, `attempt_started` marker +
  `reconcile_evidence_vs_ledger`) — process death between a successful
  `write_evidence` and `update_task_status("coded")` is reconciled at
  resume by auto-promoting the evidence to `coded`.
* **Path normalization** (v0.22.4 B4, `path_validator.normalize_path`)
  — architect-emitted paths with backticks / quotes / parentheticals
  trigger structured retry instead of wedging at execute time.
* **Lifecycle handlers** (v0.23.0 C3) — SIGTERM/SIGHUP log the signal
  and raise `SystemExit` so `finally` blocks (notably `plan_lock`
  release) run before exit. Lockfile records `<pid> <iso>` instead of
  0 bytes; stale-PID locks self-clear.
* **Telemetry**: new audit-only ledger ops `attempt_started`,
  `reconcile_evidence`, `reap_orphans`, `regex_timeout` — query via
  `autodev metrics` (planned for v0.24.0 D3).

---

## Recommended config for huge repos

Most defaults Just Work. If you want to be explicit:

```jsonc
{
  // v0.22.1 — already auto, but explicit is fine:
  "qa_gates": {
    "regex_timeout_per_file_s": 10.0,
    "secretscan_auto_skip_huge_repo": true,
    "secretscan_force_run_on_huge_repo": false
  },

  // v0.23.0 C2 — operator opt-in for tightening secret-scan signal:
  // (uncomment to use; preserves the auto-skip default)
  //   "secretscan_ignore_paths": ["Tests/**", "Fixtures/**", "*.unity.meta"],
  //   "secretscan_entropy_threshold": 4.8,
  //   "secretscan_min_entropy_length": 32

  // v0.23.0 C1 — worktree huge-repo mode:
  "worktree_huge_repo_mode": "auto",          // also: "on" | "off"
  "worktree_huge_create_timeout_s": 600,
  "worktree_huge_pool_size": 2,
  "worktree_sparse_checkout_enabled": false,  // legacy; auto in huge mode

  // v0.23.0 C4 — plan-tournament fast-path:
  "tournaments": {
    "plan": {
      "huge_repo_overrides_disabled": false   // false = fast-path on
    }
  }
}
```

---

## Recovery from a stuck workspace

If a run was interrupted (kill, OOM, SIGTERM, etc.), follow this
recipe — most of it now happens automatically.

1. **No process is running** (`ps aux | grep autodev` is empty).
2. **Lock state**: `cat .autodev/.lock` → should show `<pid> <iso>`.
   * If empty (legacy 0-byte lock from pre-v0.23.0), it's harmless.
   * If the recorded PID is alive, that's an actual conflict — kill
     it before resuming.
3. **Resume**: `autodev resume`. The orchestrator will:
   * Reconcile evidence-vs-ledger (B3) — auto-promote orphan
     successful work to `coded` so it isn't lost.
   * Reap orphan in-flight tasks (B1) — revert wedged tasks to
     `pending` so the dispatcher re-picks them up.
   * Auto-clear stale-PID lockfile (C3) if the previous holder
     process is gone.
4. If the FSM is genuinely wedged (no pending, not all terminal), you
   get a clear `PhaseStuckError` with the offending task IDs (B2).

---

## Performance expectations

Reference: Unity (358K files, 3 GB), Opus xhigh, single-machine.

| Operation | Pre-v0.22.1 | v0.23.0 huge mode |
|---|---|---|
| `cpp_symbols` scan / file | ∞ (regex hang) | < 1 s |
| Plan tournament | 80 min (3 branches) | < 20 min (1 branch) |
| Per-task `git worktree add` | 60 s (timed out) | 5-15 s (sparse) |
| Resume cycle | manual surgery | automatic (B1+B3) |
| `secretscan` on full repo | 25-30 min, 27K-50K FPs | auto-skipped |

---

## Filing huge-repo bugs

When reporting issues on huge repos, please include:

* `RepoCapacity` snapshot (file_count, total_bytes, is_huge).
* Output of `autodev status` if a run is wedged.
* The relevant section of `.autodev/plan-ledger.jsonl` (last ~50
  entries, redacted).
* Any `regex_timeout` / `reconcile_evidence` / `reap_orphans` audit
  ops that fire during reproduction.

ADR-0042 covers the v0.23.0+ "Code the Transforms" deferral — the
synthesis-based AST rewrite pipeline that depends on the longitudinal
corpus collected via v0.24.0 D4 (`autodev metrics anti-bloat
--export-corpus`).
