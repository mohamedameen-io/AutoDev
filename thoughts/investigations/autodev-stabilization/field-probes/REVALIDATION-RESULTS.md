# REVALIDATION-RESULTS.md — Phase 4 field re-validation of `stabilization-v1`

## ⟲ RE-RUN VERDICT — 2026-06-17 (post-stabilization-fixes, HEAD 95ae3b2)

**Build under test:** `stabilization-v1` @ `95ae3b2` (10 stabilization commits; harness fix `e87fb5f` added after for tooling) ·
**Adapter:** `claude_code` · **Method:** serial, real `claude` subprocesses, lint ENABLED (to validate A3). P3 (Rust `task_006`) DEFERRED — no `cargo` on host.

### TL;DR

**WS-4 "no plan initialized" residual: RESOLVED & VALIDATED.**
P2 delivers cleanly through the exact previously-failing path; 0 occurrences of `block_path_plan_uninitialized` or `worker_exception` across all 5 probe ledgers.
This SUPERSEDES the prior verdict's "WS-4 NOT resolved."

**CORRECTION (2026-06-18):** The original summary stated "0 occurrences of … `conflict_3way_failed` … across all 5 probe ledgers." That was **wrong** — it counted ledger *op names* rather than `failure_class` values inside `blocker_escalated` ops. `conflict_3way_failed` **recurs** on the hard tasks as a `failure_class`: task_007=3, task_008=9, task_009=6. It is the engine of **F-2** (budget-churn loop). What A2 (`491382e`) genuinely eliminates is *spurious* conflicts on trivial tasks — P2 delivered cleanly with 0 conflicts. Genuine conflicts on hard tasks' corrective regeneration were never eliminated and drove F-2. See corrected forbidden-op table below.

**Field bar NOT cleared — DO NOT tag v1.0.** Only 1/5 delivered (P2). New blocking findings: **F-1** (scope-recovery — `narrow_scope` wrong remedy when a task needs an out-of-scope file) and **F-2** (hard-task budget-churn → 40–56 min execute timeout; P4/P5/P6). Fix F-1 + F-2, then re-run.

### Re-run scorecard

| Probe | Lang / type | Outcome | Cause | WS-4 ops |
|---|---|---|---|---|
| **P1** `task_002` | JS / bug | **FAIL** (empty diff, ~13 m) | F-1: plan internally inconsistent — phase `edit_scope=["index.js"]` but task 1.2 requires `test_index.js` (out of scope) → `edit_scope_violation` pre-flight (~10 s) → resolver chose `narrow_scope` (ineffective — can't admit the needed file) → `task_blocked_scope_violation`, no delivery | 0 |
| **P2** `task_004` | Go / bug | **PASS** (1-line diff, exit 0, ~11 m) | Previously-failing binding-constraint path now delivers cleanly; 0 forbidden ops | 0 |
| **P3** `task_006` | Rust / bug | **DEFERRED** | No `cargo` on host | n/a |
| **P4** `task_007` | Python / feature | **FAIL** (timeout 2400 s, ~51 m, no diff) | F-2: 5 budget-recovery cycles; heavy churn → hit hard execute timeout | 0 |
| **P5** `task_008` | Python / refactor | **FAIL** (timeout 2400 s, ~56 m, no diff) | F-2: 59 budget cycles, 7 `claude error_max_turns`, `qa_gate_failed` (pytest collection errors), `conflict_rewrite_cap_exceeded` (A2 critic-rewrite cap hit LOUD) | 0 |
| **P6** `task_009` | Python / 50k-scale | **FAIL** (timeout 2400 s, ~43 m, no diff) | F-2: 24 budget cycles, 6 tournament rounds; S3 scale HEALTHY — `init_plan` present, plan tournament completed on 50k-file repo; delivery blocked by F-2 churn, NOT by scale | 0 |

**Result: 1/5 delivered (P2 PASS). P3 deferred. P1/P4/P5/P6 FAIL.**

### Forbidden-op scan (all 5 ledgers) — CORRECTED 2026-06-18

| Signal | Count | Notes |
|---|---|---|
| `block_path_plan_uninitialized` | **0** | WS-4 residual genuinely absent — A1 fixed it |
| `conflict_3way_failed` (as `failure_class` in `blocker_escalated` ops) | **recurs on hard tasks** | task_007=3, task_008=9, task_009=6; engine of F-2. CORRECTION: original scan counted op names, not failure_class values — "0" was wrong. A2 eliminates *spurious* conflicts on trivial tasks (P2: 0 ✓); genuine conflicts recur on hard-task corrective regeneration. |
| `worker_exception` | **0** | Also genuinely absent across all 5 probes |
| `init_plan` present | all 5 | |
| `task_blocked_scope_violation` | 1 (P1 only) | |

### Findings (status as of 2026-06-18)

- **F-1** (**FIXED `a5c9bb1`**): `edit_scope_violation` + ineffective `narrow_scope` recovery. Plan repair (`repair_phase_edit_scope`) now admits a phase's tasks' declared concrete files into `edit_scope`; run after the drop/empty-guard pass; P0 empty-guard preserved; `tournament promote` CLI bypass also closed. Reproduce-first + reviewed. Resolves the P1 scope-block.
- **F-2** (**FIXED `9ecf8ee`**): Hard-task budget-churn → 40–56 min execute timeouts (P4/P5/P6). Phase-scoped non-convergence ceiling (`max_corrective_cycles_per_phase`=3) bounds same-failure-class corrective regeneration — emits loud `corrective_nonconvergent_ceiling` op + terminal block instead of churning to the 40-min execute timeout. Resets on a different `failure_class` or forward progress (legitimate recovery preserved). Reproduce-first + reviewed, replay-safe. **NOTE:** makes hard tasks fail-loud-fast; does NOT make them deliver (see F-5).
- **F-3** (FIXED `e87fb5f` / infra): Benchmark harness crashed (`TypeError: Object of type bytes is not JSON serializable`) on `subprocess.TimeoutExpired` — raw bytes from stdout/stderr not decoded before JSON serialization. Fixed.
- **F-4** (OPEN / high / deferred): Execute-flow apply-time scope enforcement is **dormant** — all 3 `apply_patch_to_main` callers pass `edit_scope=None`, so the developer's actual diff is never scope-checked at apply time. Only declaration-level pre-flight guards scope. Activating real diff-level enforcement is a separate, higher-risk change (could surface latent violations in existing plans). Deferred.
- **F-5** (OPEN / high / candidate next): Genuine-conflict non-convergence — corrective tasks on hard tasks repeatedly collide with the same accumulated worktree state. F-2 bounds the loop and makes failure loud/fast, but the deeper cause (worktree-state accumulation / merge-base divergence) is unsolved → hard tasks still produce 0 diff (fail-loud-fast but do NOT deliver). Next candidate fix for hard-task DELIVERY.

### What the stabilization commits validated

A3 lint: validated in production (lint ENABLED, 0 lint-block events).
A5: confirmed prior `cost_usd 0.0` was a symptom of the ledger-wipe (watermark already correct after A1).
A4/B1/B2/B3: landed; full unit gate green (4299 passed / 6 skipped, ruff + mypy clean).
A1 (`7b40ce4`): exclude `.autodev/` from `git clean -fd` + idempotent reload-retry — the ledger-wipe root cause.
A2 (`491382e`): auto `--3way` before critic, removing the *spurious*-conflict trigger (trivial tasks); genuine conflicts on hard tasks' corrective regeneration persist → F-2/F-5.

The 11 commits (589b2bf..HEAD): `ec293c5` A3 lint, `7c5d9f3` B1 necessity-ladder, `7b40ce4` A1 ledger-wipe fix, `491382e` A2 auto-3way, `94e3dec`+`f81a185` A4 finalize/exit-code, `1e70861` A5 cost metric, `3da18ef` B2 reviewer advisory, `2d74af3` B3 effort-modulation, `95ae3b2` final-review polish, `e87fb5f` harness bytes fix.

---

## POST-RE-RUN FIXES — 2026-06-18

**Build:** `stabilization-v1` @ `9ecf8ee` · **Unit gate:** 4315 passed / 6 skipped (ruff + mypy clean) · **Field re-run:** PENDING

### Correction: `conflict_3way_failed` was NOT zero across all probes

The RE-RUN VERDICT above originally stated "0 occurrences of … `conflict_3way_failed` … across all 5 probe ledgers." This was **incorrect** — the scan counted ledger *op names* but `conflict_3way_failed` surfaces as a `failure_class` value inside `blocker_escalated` ops. Actual counts: **task_007=3, task_008=9, task_009=6** — it is the engine of the F-2 budget-churn loop. What IS genuinely absent across all 5 probes is the WS-4 `block_path_plan_uninitialized` / `worker_exception` residual (that part of the verdict stands — A1 fixed it). A2 eliminates *spurious* conflicts on trivial tasks; genuine conflicts on hard-task corrective regeneration were never eliminated.

### F-1 — FIXED (`a5c9bb1`)

Plan repair (`repair_phase_edit_scope`) admits a phase's tasks' declared concrete files into `edit_scope`. Run after the drop/empty-guard pass; P0 empty-guard preserved; `tournament promote` CLI bypass also closed. Reproduce-first + reviewed. Resolves the P1 scope-block.

### F-2 — FIXED (`9ecf8ee`)

Phase-scoped non-convergence ceiling (`max_corrective_cycles_per_phase`=3) bounds same-`failure_class` corrective regeneration — emits loud `corrective_nonconvergent_ceiling` op + terminal block instead of churning to the 40-min execute timeout. Resets on a different `failure_class` or forward progress (legitimate recovery preserved). Reproduce-first + reviewed, replay-safe. **NOTE: makes hard tasks fail-loud-fast; does NOT make them deliver (see F-5).**

### F-4 — OPEN / DEFERRED

Apply-time edit_scope enforcement is dormant: all 3 `apply_patch_to_main` callers pass `edit_scope=None`. Only declaration-level pre-flight guards scope. Activating real diff-level enforcement is a separate, higher-risk change (could surface latent violations in existing plans). Deferred.

### F-5 — OPEN / CANDIDATE NEXT (hard-task delivery)

Genuine-conflict non-convergence: corrective tasks on hard tasks repeatedly collide with the same accumulated worktree state. F-2 bounds the churn loop and makes failure loud/fast, but the underlying cause (worktree-state accumulation / merge-base divergence between corrective attempts) is unsolved. Hard tasks still produce 0 diff. This is the next candidate fix for hard-task DELIVERY.

### Overall status

| Item | Status |
|---|---|
| F-1 (scope recovery) | **FIXED** `a5c9bb1` |
| F-2 (non-convergence ceiling) | **FIXED** `9ecf8ee` |
| F-3 (harness bytes-JSON) | **FIXED** `e87fb5f` |
| F-4 (apply-time enforcement dormant) | **OPEN** / deferred |
| F-5 (genuine-conflict non-convergence) | **OPEN** / candidate next |
| Unit gate | **GREEN** (4315 passed / 6 skipped) |
| Field re-run | **PENDING** (F-1/F-2 unit-validated only) |
| v1.0 tag | **NOT READY** — F-4, F-5 open; field re-run pending |

---

> **The sections below are the PRIOR run that motivated these fixes — superseded on the WS-4 question, preserved as historical record.**

---

**Date:** 2026-06-17 · **Build under test:** `stabilization-v1` @ `8d99742` (+ field fixes below) ·
**Adapter:** `claude_code` (CLI 2.1.178) · **Method:** serial, isolated venv, real `claude` subprocesses.

## TL;DR verdict

**`stabilization-v1` does NOT clear the field bar — DO NOT tag v1.0 yet.**

The WS-4 binding constraint is **NOT resolved**. It reproduces verbatim and **language-general**
(Go, Python, Python-at-50k): a tournament 3-way merge conflict triggers
`conflict_3way_failed → re_architect → fell_through`, and the synthesized corrective-retry task then
raises **`worker_exception: "no plan initialized; call init_plan first"`** → falls through → **no
delivery**. Three of four scored probes failed this way; the one PASS (P1) simply never triggered a
tournament conflict.

Good news: the four `[FIELD]` gates themselves are in good shape — **N4 was found broken and FIXED**
(see below), **S1** is now trustworthy, **G5** runners executed (node/go/py), and **S3** 50k-scale is
healthy. But the tagging bar also requires *“WS-4 P2–P6 resolved”*, and they are not.

---

## What had to be fixed just to run the matrix (3 issues found pre-/in-flight)

| # | Issue | Severity | Fix | Commit |
|---|---|---|---|---|
| N4 | `claude_code` emitted `--allowed-tools ""` for text-only roles (`critic_t`/`synthesizer`). The A4 micro-probe proved this is a **no-op**: a Bash-triggering prompt still executed Bash (`permission_denials=[]`). `--allowed-tools` is permission-only, not availability. | **must-fix (FIELD)** | render `allowed_tools=[]` as **`--tools ""`** (availability flag; field-verified 0 `tool_use`). RED-on-HEAD test. | `e614e93` |
| Harness | `task_runner.py` invoked `autodev plan --spec <path>` — but the shipped CLI is `plan [OPTIONS] INTENT` (no `--spec`). click exited 2 in <1 s, before any planning. | blocker (benchmark) | pass spec **text** as positional intent + `--assume-defaults`; also capture failing-command stdout/stderr into results. | `589b2bf` |
| Lint gate | `qa/lint._run_eslint` runs `npx eslint .` → fetches ESLint v10 → hard-errors (“no eslint.config.js”) on config-less fixtures; `severity=block` ⇒ unwinnable retry loop ⇒ block. | robustness | opt-in `AUTODEV_BENCH_DISABLE_LINT` (default OFF) disables the env-fragile lint gate for the benchmark (lint is orthogonal to delivery + the FIELD gates). | `589b2bf` |

Also reconstructed the 4 lost WS-4 fixtures as durable `benchmarks/tasks/v1/` entries (`task_006_rs_vowels`,
`task_007_py_csv_feature`, `task_008_py_email_refactor`, `task_009_py_50k_scale`), commit `849244d`.

---

## Probe scorecard

| Probe | Lang / type | WS-4 outcome | Phase-4 outcome | Acceptance? | Evidence |
|---|---|---|---|---|---|
| **P1** `task_002` | JS / bug | ✅ verified | **PASS** (4-line null-check delivered, test passes; ~24 m) | ✅ **met** | No conflict triggered → no cascade. Watch: `execute exited 2` + final status `in_progress` = *delivered-but-not-finalized*; fix left **uncommitted** in worktree (recovered by the initial→worktree diff). |
| **P2** `task_004` | Go / bug | ❌ blocked | **FAIL** (empty diff; ~40 m) | ❌ **not met** | `conflict_3way_failed → re_architect → fell_through`; corrective task `1.c2` → `"no plan initialized"` → `retry_with_changes → fell_through`; `block_path_plan_uninitialized` ×2; `clear_in_flight`. Final status not “blocked” (silent non-delivery). |
| **P3** `task_006` | Rust / bug | ⚠️ unverified | **DEFERRED** (no `cargo`/`rustup` installable on host) | n/a | Fixture is durable for a cargo-equipped host. G5-rust unverified-in-field (toolchain absent → degrade-loud is the documented behavior). |
| **P4** `task_007` | Python / **feature** | ❌ blocked, mis-framed | **FAIL** (empty diff; ~23 m) | ❌ **not met** | **Same** constraint: `conflict_3way_failed` ×5, `re_architect → fell_through`, `"no plan initialized"` ×4. → residual is **language-general**. |
| **P5** `task_008` | Python / **refactor** | ⚠️ vacuous pass | **SKIPPED** | n/a | Cascade already shown ×2; the structural-change guard (the P5 acceptance mechanism) is unit-verified in the scorer (empty-diff → FAIL “no structural change”). Skipped to conserve quota. |
| **P6** `task_009` | Python / **50k-file** | ❌ blocked + “no plan init” | **FAIL** delivery; **S3 scale PASS** (~27 m) | delivery ❌ / scale ✅ | Same constraint (×5/×2/×4). **But S3 healthy:** 50,254 files; `init` 4.8 s; index 22.95 MB; plan 7 m with `huge_repo` overrides engaged (×14); execute 20 m; **no crash/OOM/timeout/unbounded read**. Delivery blocked by the residual, NOT by scale. |

Scored: **P1 PASS, P2/P4/P6 FAIL** (binding constraint), P3 deferred, P5 skipped.

---

## The binding constraint — root cause (live-traced; the runbook’s explicit ask)

The REVALIDATION runbook flagged `worker_exception: "no plan initialized; call init_plan first"` as a
carried-forward residual that *“needs a live trace to fix.”* **Phase 4 produced that trace** (kept
workdirs under `/tmp/p4work/*/agent_repo/.autodev/plan-ledger.jsonl`).

Mechanism:
1. The implementation tournament’s 3-way merge (worktree → main) **conflicts even on trivial fixes**
   (e.g. adding `defer file.Close()`) → `conflict_3way_failed`.
2. The resolver chooses `re_architect`. Phase-1A Step 5 *does* synthesize a structured corrective
   task (`1.c2`) — but it `fell_through` (no recovery).
3. The corrective-retry path then calls
   `orch.plan_manager.update_task_status(task.id, "blocked", …)` →
   `PlanManager._load_sync()` reads the plan **from disk at `self._cwd`** → returns `None` →
   raises **`"no plan initialized; call init_plan first"`** (`src/state/plan_manager.py:527` et al.).
   The corrective path’s `PlanManager._cwd` points at a **worktree / execute-pool dir that has no
   persisted `.autodev/` plan snapshot** (the plan lives in the main repo).
4. `blocker_guard.py:123` catches *only* that signature and emits the attributable
   `block_path_plan_uninitialized` breadcrumb (the Step-5 guard works as designed — diagnosable, not
   self-masking), then re-raises → second `fell_through` → `clear_in_flight` → no diff, `execute` rc 0.

**Net:** stabilization-v1 *guarded and attributed* this residual but did **not fix** it. It is the
delivery-layer v1.0 blocker.

---

## `[FIELD]` gate closure

| Gate | Status | Basis |
|---|---|---|
| **N4** — `--allowed-tools ""` enforces no-tools | **✅ CLOSED (after fix)** | A4 micro-probe: pre-fix tool_use=1 (no-op) → post-fix tool_use=0 via `--tools ""`. `results/phase4/A4-*.json`. |
| **S1** — critic context bounded | **✅ trustworthy** | With N4 fixed, `critic_t`/`synthesizer` have **zero tools** (cannot `Read`), so they cannot pull 190–260K-token file context — bounded by construction. Tool-less critic ran in P1/P2/P4/P6. |
| **G5** — per-language runners execute | **✅ node/go/py · ⚠️ cargo deferred** | node (P1), go (P2), pytest (P4/P6) ran with the test gate enabled; the scorer re-ran each language’s tests. `cargo` (P3) deferred — no toolchain on host. |
| **S3** — 50k-file repo, execute-side scale | **✅ healthy** | P6: 50k files handled by init/index/plan/execute with `huge_repo` overrides, no crash/OOM/timeout/unbounded read. |

3/4 closed or healthy; G5 partial (cargo deferred). **But the overall tagging bar fails on “WS-4 P2–P6
resolved”.**

---

## Secondary findings (not `[FIELD]` gates, but real)

1. **Spurious `conflict_3way_failed` on trivial tasks.** The impl-tournament a/b/ab 3-way merge collides
   even for one-line fixes, *triggering* the cascade. Eliminating these spurious conflicts would avoid
   the residual path entirely for most tasks.
2. **`execute` exits 2 / status `in_progress` on a delivered fix (P1).** Even when the fix lands and the
   test passes, autodev did not cleanly *finalize* (commit/approve) — the critic/review loop is heavy
   and budget-bound. “Delivered-but-not-finalized” is recoverable by the benchmark’s worktree-diff
   capture, but in production it would look like an incomplete run.
3. **Lint gate robustness** (see fixes table): env/config-absent lint failures block delivery via an
   unwinnable loop. Recommend: eslint “no config” → skip; make lint env-failures non-blocking (`warn`).
4. **Plan-phase cost/latency is high and variable**: ~9 m/$6.6 (P1) to ~22 m/$11.7 (P2) for trivial
   tasks; the planning tournament + critic dominate. (50k-repo plan was *faster*, ~7 m — size is not the
   driver.)
5. **Execute-phase cost not aggregated** in `run-summary.jsonl` (shows `cost_usd: 0.0`) — a metrics gap.

---

## Recommendation (prioritized next steps before any v1.0 tag)

1. **Fix the corrective-retry `"no plan initialized"` residual** (THE blocker). Bind the corrective /
   blocker-handling `PlanManager` to the **main-repo cwd** (where the plan is persisted), or persist the
   plan snapshot into the worktree before status updates. The live traces in `/tmp/p4work/*` (esp. P2)
   make this now-deterministically diagnosable. Add an engagement test that drives a conflict→corrective
   path and asserts the corrective task runs against a loaded plan.
2. **Suppress spurious `conflict_3way_failed`** on trivial single-file fixes (tournament merge strategy)
   — removes the cascade trigger for most tasks.
3. **Lint-gate robustness**: eslint no-config → skip; lint env-failures → non-blocking `warn`.
4. **Re-run this matrix** (P1, P2, P4, P6 minimum; add P3 on a cargo host, P5 for the refactor guard)
   after (1)–(3); the tagging bar is met when P2/P4/P6 deliver-or-fail-loud AND all `[FIELD]` green.

## Caveats
- Reconstructed P3–P6 fixtures are faithful to the WS-4 *specs*, not byte-identical to the lost originals
  (acceptance is behavioral, so it holds). P3 not executed (no cargo); P5 skipped (quota).
- Lint gate disabled for the run (env-fragile; orthogonal to delivery + FIELD gates).
- Costs approximate (execute-phase not summed); ~1.9 h of serial probe wall-clock + the cheap A4 probe.
