# 0043 — Huge-Repo Mode (`worktree_huge_repo_mode` + cascading defaults)

* **Status:** Accepted (v0.23.0)
* **Deciders:** AutoDev maintainers
* **Date:** 2026-05-10
* **Related:** [`thoughts/shared/plans/2026-05-10-huge-repo-stability-roadmap.md`](../../thoughts/shared/plans/2026-05-10-huge-repo-stability-roadmap.md), `docs/huge_repo_guide.md`, ADR-0007 (worktree isolation), ADR-0008 (deterministic FSM)

## Context

The 2026-05-09 Unity run (358K files, 3 GB) surfaced 12 distinct failure modes during a single AutoDev invocation: a catastrophic regex backtrack that pinned the orchestrator for 40+ min, a 60 s `git worktree add` timeout that killed full checkouts, a 27K-50K secretscan false-positive avalanche on test fixtures, an 80-min plan tournament, an explorer hitting `error_max_turns`, and several FSM resilience gaps that prevented graceful recovery.

`runtime.repo_probe.RepoCapacity.is_huge` already existed (file_count > 20K **OR** total_bytes > 5 GB) but was consumed only by `resolve_max_turns` for per-task budget scaling. The signal was correct; the consumers were missing.

## Decision

We promote `is_huge` from a single-purpose advisory to a **first-class config dimension** with three modes:

* **`worktree_huge_repo_mode = "auto"`** (default) — every huge-repo behavior keys off the `is_huge` probe.
* **`worktree_huge_repo_mode = "on"`** — force huge-repo defaults regardless of probe (operators who know their repo will grow).
* **`worktree_huge_repo_mode = "off"`** — disable huge-repo behaviors even on huge repos (legacy escape hatch for operators with bigger compute budgets / parallelism).

The mode controls a **cascading default**:

| Subsystem | Huge-mode default | Knob name |
|---|---|---|
| Worktree create timeout | 600 s (was 60 s) | `worktree_huge_create_timeout_s` |
| Worktree pool size | 2 (was `parallelism`) | `worktree_huge_pool_size` |
| Per-task worktree | Sparse-checkout | (auto when huge mode resolves on) |
| Secretscan | Auto-skip with warn | `qa_gates.secretscan_force_run_on_huge_repo` |
| Plan tournament branches | Single-branch | `tournaments.plan.huge_repo_overrides_disabled` |
| Explorer `max_turns` | 2× the configured base | (built-in role-aware bump) |
| Hallucination_guard regex | Per-file 10 s watchdog | `qa_gates.regex_timeout_per_file_s` |

Each subsystem **independently overridable**: setting `worktree_huge_repo_mode = "on"` does NOT force every cascading default — operators can keep the regex watchdog while opting out of secretscan auto-skip, etc.

## Consequences

### Why now (v0.23.0)

* **Empirical validation.** The Unity run is a real-world operating point in the > 20K-file regime. The 12-finding failure list pins the relevant defaults to concrete observed numbers (60 s → 600 s timeout; 5 → 1 plan branches; 4.5 → 4.8 entropy threshold).
* **Backward-compatible by construction.** Every cascading default is gated on `is_huge` (off for the majority of users). Existing CI / smoke runs on small fixtures preserve byte-identical behavior.
* **Composes cleanly with ADR-0007.** Worktree isolation infrastructure was already in place — C1 just promotes existing sparse-checkout machinery to default behavior under huge mode.

### Why three modes (`auto`/`on`/`off`)

* Two-mode (`auto`/`off`) loses the "I'm about to grow" signal — operators with huge-repo intent before the probe trips need `on`.
* Boolean-only (`enabled: bool`) ambiguates auto vs. force-on at config-read time.
* Three-mode mirrors the pattern in `cfg.prm.strategy = "rules" | "rules+ml"` and `cfg.plateau_detector.strategy = "rules" | "regression"` — operators already know this idiom.

### What we deliberately did NOT do

* **No global "huge mode" toggle.** Each subsystem has its own escape hatch. This avoids the trap of "oh, you wanted secretscan but not sparse-checkout? Tough luck."
* **No new top-level config namespace.** Fields land on `AutodevConfig` directly with `worktree_*` / `qa_gates.*` / `tournaments.*` prefixes that match the existing layering. A `HugeRepoConfig` sub-class would just hide the cascading defaults behind a layer of indirection.
* **No retroactive renaming of `worktree_sparse_checkout_enabled`.** The legacy flag stays as a deprecated alias for v0.23.0; removal is scheduled for v0.24.0.
* **No CLI ergonomics for `huge_repo_mode`.** Operators set it in `.autodev/config.json` like any other field. A `--huge-mode` CLI flag would suggest huge mode is per-invocation when in fact it's per-repo.

## Trigger criteria for revisiting

* `is_huge` thresholds (20K files / 5 GB) prove wrong on a real workload — bump or expose them per-repo.
* The cascading defaults compose poorly (operators report needing every override on every run). Suggests a `HugeRepoConfig` sub-class or per-subsystem `huge_overrides` blocks.
* New subsystems that should be huge-aware land outside the cascading default set.
