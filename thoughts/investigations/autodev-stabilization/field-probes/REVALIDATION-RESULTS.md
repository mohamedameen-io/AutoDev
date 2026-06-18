# REVALIDATION-RESULTS.md — Phase 4 field re-validation of `stabilization-v1`

## ✓ v2 VALIDATION RE-RUN — 2026-06-18 (HEAD 569bda5, F-1/F-2/F-5 verified)

**Build under test:** `stabilization-v1` @ `569bda5` · Isolated venv rebuilt from HEAD; F-1/F-2/F-5 fix markers verified present · **Adapter:** `claude_code` · **Method:** serial, lint ENABLED · P3 (Rust) DEFERRED — no `cargo` on host.

### TL;DR

**F-5 FIELD-VALIDATED.** P4 (task_007 Python feature): 40-min timeout in v1 → 136-line clean delivery in ~18 min in v2. `BINARY_PATCH_ERR = 0` across **all 5 probes** (P1/P2/P4/P5/P6). The binary-`.pyc` defect that dominated hard-task non-delivery is fully removed. F-2 bounded churn held. WS-4 residual absent.

**Bar NOT a clean sweep: 2/5 delivered (P2, P4). v1.0 NOT READY.** Two new findings: **F-6** (per-task sparse worktree omits test-harness files → spurious `qa_gate_failed`) and **F-7** (plan-phase 40-min timeout on complex tasks under high latency). P6 execute non-convergence persists under bounded genuine churn. All F-6/F-7/P6 issues are aggravated by unusually high claude latency this session — re-measure under normal latency before concluding they are code issues.

### v2 Scorecard

| Probe | Lang / type | Outcome | Diff | Wall | Cause |
|---|---|---|---|---|---|
| **P1** `task_002` | JS / bug | **FAIL** (empty diff) | 0 | ~9 m | **F-6 NEW**: architect scoped to `files=[index.js]`; sparse worktree lacked `package.json` → `npm test` ENOENT → `qa_gate_failed ×3` → `error_max_turns` → `infra_circuit_open` → blocked. F-1's violation did NOT recur (variance gave a cleaner plan). |
| **P2** `task_004` | Go / bug | **PASS** | 1-line | ~7 m | Clean control; environment healthy. |
| **P3** `task_006` | Rust / bug | **DEFERRED** | — | — | No `cargo` on host. |
| **P4** `task_007` | Python / feature | **PASS** | 136-line | ~18 m | **F-5 field-validated headline**: was 40-min `.pyc`-conflict-loop TIMEOUT in v1; delivers cleanly in v2. |
| **P5** `task_008` | Python / refactor | **FAIL** (timeout) | 0 | ~40 m | **F-7 NEW**: PLAN command timed out at 2400 s — heavy refactor tournament + ~80 s/call claude latency. 0 `.pyc` errors. 0 execute churn (F-5 removed prior execute-churn). |
| **P6** `task_009` | Python / 50k | **FAIL** (timeout) | 0 | ~40 m | Execute timed out on bounded genuine churn (2 `conflict_abandon` + 3 `conflict_3way`, 3 correctives). **0 `.pyc` errors** (F-5 held at 50k scale). Did not converge within budget + latency. |

**Result: 2/5 delivered (P2, P4). P3 deferred. P1/P5/P6 FAIL.**

### Cross-probe scan (all 5 probes)

| Signal | Count | Notes |
|---|---|---|
| `BINARY_PATCH_ERR` | **0** | F-5 fully removed the binary-`.pyc` apply defect — holds at 50k scale (P6). |
| `block_path_plan_uninitialized` | **0** | WS-4 residual stays gone (A1 fix holds). |
| `worker_exception` | **0** | Genuinely absent across all 5 probes. |
| `.pyc` conflict errors | **0** | No `.pyc` churn on any probe (P4/P5/P6 all clean). |
| `corrective_nonconvergent_ceiling` | 0 (did not fire) | F-2 ceiling not needed — no unbounded loop; genuine churn in P6 stayed bounded. |
| `infra_circuit_open` | 1 (P1 only) | Consequence of F-6 ENOENT qa_gate chain. |

### v2 Findings

- **F-5** (**FIELD-VALIDATED** `fca31b3;c925962;81d9438`): P4 delivers 136-line clean feature in ~18 m. `BINARY_PATCH_ERR=0` across all 5 probes. The binary-`.pyc` diff defect (`git diff` missing `--binary --full-index`) was the root cause of hard-task non-delivery in v1. **Fully resolved.**
- **F-6** (OPEN / high): Per-task sparse worktree omits test-harness files outside `edit_scope`. `package.json` / test files / deps absent → `npm test` ENOENT → `qa_gate_failed ×3` → circuit open. Under-scope mirror of F-1: the QA gate needs read-only test-harness files that are orthogonal to the edit scope. Fix: ensure sparse worktree always includes the full test harness regardless of `edit_scope`.
- **F-7** (OPEN / medium — LATENCY CONFOUND): Plan-phase can exceed the 40-min per-command timeout on complex tasks. P5 plan timed out at 2400 s; claude latency was ~80 s/call this session. Re-measure under normal latency (<30 s/call) before concluding this is a code issue. If confirmed: investigate plan tournament budget / adaptive timeout for complex refactor tasks.
- **P6 execute non-convergence**: 0 `.pyc` errors (F-5 holds); genuine conflict churn (2 `conflict_abandon` + 3 `conflict_3way`, 3 correctives) did not converge within the 40-min execute budget at this session's latency. Same latency confound as F-7 — re-measure before treating as a code issue.

### v2 Overall status

| Item | Status |
|---|---|
| F-1 (scope recovery) | **FIXED** `a5c9bb1` — unit-validated; not field-exercised v2 (variance) |
| F-2 (non-convergence ceiling) | **FIXED** `9ecf8ee` — bounded churn held; ceiling not needed in v2 |
| F-3 (harness bytes-JSON) | **FIXED** `e87fb5f` |
| F-4 (apply-time enforcement dormant + binary-blindness) | **OPEN** / deferred |
| F-5 (binary-.pyc diff defect) | **FIXED + FIELD-VALIDATED** `fca31b3;c925962;81d9438` |
| F-6 (sparse worktree omits test harness) | **OPEN** / high / new in v2 |
| F-7 (plan-phase timeout under high latency) | **OPEN** / medium / new in v2 — latency confound |
| Unit gate | **GREEN** (4320 passed / 6 skipped, ruff + mypy clean) |
| Field bar | **NOT CLEARED** — 2/5; F-6, F-7, P6 non-convergence remain |
| v1.0 tag | **NOT READY** — fix F-6; confirm F-7/P6 under normal latency |

---

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

### Findings (status as of 2026-06-18, updated v2 re-run)

- **F-1** (**FIXED `a5c9bb1`**): `edit_scope_violation` + ineffective `narrow_scope` recovery. Plan repair (`repair_phase_edit_scope`) now admits a phase's tasks' declared concrete files into `edit_scope`; run after the drop/empty-guard pass; P0 empty-guard preserved; `tournament promote` CLI bypass also closed. Reproduce-first + reviewed. Resolves the P1 scope-block. *(Not field-exercised in v2 — variance gave a cleaner plan.)*
- **F-2** (**FIXED `9ecf8ee`**): Hard-task budget-churn → 40–56 min execute timeouts (P4/P5/P6). Phase-scoped non-convergence ceiling (`max_corrective_cycles_per_phase`=3) bounds same-failure-class corrective regeneration — emits loud `corrective_nonconvergent_ceiling` op + terminal block instead of churning to the 40-min execute timeout. Resets on a different `failure_class` or forward progress (legitimate recovery preserved). Reproduce-first + reviewed, replay-safe. *(Ceiling did not need to fire in v2 — no unbounded loop observed; bounded genuine churn in P6 is a separate phenomenon.)*
- **F-3** (FIXED `e87fb5f` / infra): Benchmark harness crashed (`TypeError: Object of type bytes is not JSON serializable`) on `subprocess.TimeoutExpired` — raw bytes from stdout/stderr not decoded before JSON serialization. Fixed.
- **F-4** (OPEN / high / deferred): Execute-flow apply-time scope enforcement is **dormant** — all 3 `apply_patch_to_main` callers pass `edit_scope=None`, so the developer's actual diff is never scope-checked at apply time. Only declaration-level pre-flight guards scope. Activating real diff-level enforcement is a separate, higher-risk change (could surface latent violations in existing plans). Deferred. **Additional gap (surfaced by F-5):** `extract_files_from_diff` ignores binary file headers (`Binary files … differ`), so binary edits are invisible to the edit_scope gate entirely. Breadcrumb at `worktree.py` gate (commit `81d9438`).
- **F-5** (**FIXED `fca31b3` + `c925962` + `81d9438`** — **FIELD-VALIDATED v2 2026-06-18**): What was recorded as genuine-conflict non-convergence was in fact a **binary-.pyc diff defect** — `get_diff_vs_base` ran `git diff` without `--binary --full-index`, causing `.pyc` binary hunks to fail `git apply --check --3way` (rc=1 "cannot apply binary patch without full index line") → spurious `conflict_3way_failed`. Fix: `--binary --full-index` + `filter_generated_from_diff` seam + fixture `.gitignore` seeding. **v2 evidence:** P4 (Py feature) delivered 136-line diff in ~18 m (was 40-min timeout in v1). `BINARY_PATCH_ERR=0` across all 5 probes. F-5 holds at 50k scale (P6: 0 `.pyc` errors). **Fully resolved.**
- **F-6** (OPEN / high — **NEW v2 2026-06-18**): Per-task sparse worktree omits test-harness files outside `edit_scope`. Architect scoped P1 to `files=[index.js]`; `package.json` absent → `npm test` ENOENT → `qa_gate_failed ×3` → `infra_circuit_open`. Under-scope mirror of F-1: the QA gate requires read-only test-harness files (package.json, test files, deps) that are outside the edit scope. Fix: sparse worktree must include the full test harness regardless of `edit_scope`.
- **F-7** (OPEN / medium — **NEW v2 2026-06-18 — LATENCY CONFOUND**): Plan-phase can exceed the 40-min per-command timeout on complex tasks under high claude latency. P5 (Py refactor) plan timed out at 2400 s; session latency was ~80 s/call. 0 `.pyc` errors; 0 execute churn (F-5 removed prior churn). Re-measure under normal latency (<30 s/call) before treating as a code issue.

### What the stabilization commits validated

A3 lint: validated in production (lint ENABLED, 0 lint-block events).
A5: confirmed prior `cost_usd 0.0` was a symptom of the ledger-wipe (watermark already correct after A1).
A4/B1/B2/B3: landed; full unit gate green (4299 passed / 6 skipped, ruff + mypy clean at original re-run; 4320 passed / 6 skipped after F-1/F-2/F-5 fixes).
A1 (`7b40ce4`): exclude `.autodev/` from `git clean -fd` + idempotent reload-retry — the ledger-wipe root cause.
A2 (`491382e`): auto `--3way` before critic, removing the *spurious*-conflict trigger (trivial tasks); genuine conflicts on hard tasks' corrective regeneration persist → F-2/F-5.

The 11 commits (589b2bf..HEAD): `ec293c5` A3 lint, `7c5d9f3` B1 necessity-ladder, `7b40ce4` A1 ledger-wipe fix, `491382e` A2 auto-3way, `94e3dec`+`f81a185` A4 finalize/exit-code, `1e70861` A5 cost metric, `3da18ef` B2 reviewer advisory, `2d74af3` B3 effort-modulation, `95ae3b2` final-review polish, `e87fb5f` harness bytes fix.

---

## POST-RE-RUN FIXES — 2026-06-18

**Build:** `stabilization-v1` @ `81d9438` · **Unit gate:** 4320 passed / 6 skipped (ruff + mypy clean) · **Field re-run:** PENDING

### Correction: `conflict_3way_failed` was NOT zero across all probes

The RE-RUN VERDICT above originally stated "0 occurrences of … `conflict_3way_failed` … across all 5 probe ledgers." This was **incorrect** — the scan counted ledger *op names* but `conflict_3way_failed` surfaces as a `failure_class` value inside `blocker_escalated` ops. Actual counts: **task_007=3, task_008=9, task_009=6** — it is the engine of the F-2 budget-churn loop. What IS genuinely absent across all 5 probes is the WS-4 `block_path_plan_uninitialized` / `worker_exception` residual (that part of the verdict stands — A1 fixed it). A2 eliminates *spurious* conflicts on trivial tasks; genuine conflicts on hard-task corrective regeneration were never eliminated.

### F-1 — FIXED (`a5c9bb1`)

Plan repair (`repair_phase_edit_scope`) admits a phase's tasks' declared concrete files into `edit_scope`. Run after the drop/empty-guard pass; P0 empty-guard preserved; `tournament promote` CLI bypass also closed. Reproduce-first + reviewed. Resolves the P1 scope-block.

### F-2 — FIXED (`9ecf8ee`)

Phase-scoped non-convergence ceiling (`max_corrective_cycles_per_phase`=3) bounds same-`failure_class` corrective regeneration — emits loud `corrective_nonconvergent_ceiling` op + terminal block instead of churning to the 40-min execute timeout. Resets on a different `failure_class` or forward progress (legitimate recovery preserved). Reproduce-first + reviewed, replay-safe. **NOTE: makes hard tasks fail-loud-fast; does NOT make them deliver (see F-5).**

### F-4 — OPEN / DEFERRED (expanded 2026-06-18)

Apply-time edit_scope enforcement is dormant: all 3 `apply_patch_to_main` callers pass `edit_scope=None`. Only declaration-level pre-flight guards scope. Activating real diff-level enforcement is a separate, higher-risk change (could surface latent violations in existing plans). Deferred.

**Additional gap surfaced by F-5 investigation:** `extract_files_from_diff` (called by the apply-time scope gate) only matches `--- a/` lines — it silently ignores `Binary files a/... and b/... differ` headers. Binary edits are therefore **invisible to `edit_scope` gating entirely**. Now that F-5 makes binary patches apply cleanly, activating apply-time enforcement must also teach the parser the `diff --git`/`Binary files…differ` header format. A breadcrumb comment marks this gap at `worktree.py`'s edit_scope gate (commit `81d9438`).

### F-5 — FIXED (`fca31b3` + `c925962` + `81d9438`) — MISDIAGNOSIS CORRECTED

**Was recorded as:** genuine-conflict non-convergence — hard tasks fail-loud-fast but don't deliver.

**Debunked by read-only investigation.** The recurring `conflict_3way_failed` on hard tasks was NOT a genuine source merge conflict / convergence problem. Root cause: `get_diff_vs_base` (`src/orchestrator/worktree.py`) ran `git diff` **without `--binary --full-index`**. A changed tracked binary — the benchmark fixture's pytest-regenerated `__pycache__/*.pyc` — emitted a malformed binary hunk with an abbreviated index line. `git apply --check --3way` returned rc=1 (`"cannot apply binary patch … without full index line"`) producing spurious `conflict_3way_failed` in a loop.

Three independent observations refuted the convergence hypothesis: (1) the source `.py` change always applied cleanly (critic saw `conflict_files=[]`); (2) main never drifted between corrective attempts; (3) the TS task (no `.pyc`) delivered cleanly.

**Fix (reproduce-first, git-only, reviewed):**
1. Added `--binary --full-index` to `get_diff_vs_base` (`fca31b3`).
2. Excluded generated cruft (`__pycache__/*.pyc`, lockfiles, `*.min.*`) from delivered diffs via a single seam `filter_generated_from_diff` in new `src/adapters/git_utils.py` (consolidated source of truth, replacing `execute_phase.py`'s local predicate) (`fca31b3`).
3. Seeded `.gitignore` in the benchmark fixture init (`benchmarks/runner/task_runner.py:_init_git_repo`) since a bare `git add .` had swept the fixture `.pyc` into the initial commit (`c925962`).
4. Added binary `edit_scope` breadcrumb in `worktree.py` + cleanups (`81d9438`).

**Expected field effect:** hard tasks (P4/P5/P6) should now deliver (the binary-.pyc apply blocker is removed), or fail-loud-fast via the F-2 ceiling if genuine convergence issues remain. Field re-run PENDING to confirm.

### Overall status (updated after v2 re-run 2026-06-18)

| Item | Status |
|---|---|
| F-1 (scope recovery) | **FIXED** `a5c9bb1` — unit-validated; not field-exercised v2 (variance) |
| F-2 (non-convergence ceiling) | **FIXED** `9ecf8ee` — ceiling held; bounded P6 churn is separate |
| F-3 (harness bytes-JSON) | **FIXED** `e87fb5f` |
| F-4 (apply-time enforcement dormant + binary-blindness) | **OPEN** / deferred |
| F-5 (binary-.pyc diff defect) | **FIXED + FIELD-VALIDATED** `fca31b3;c925962;81d9438` |
| F-6 (sparse worktree omits test harness) | **OPEN** / high / new v2 |
| F-7 (plan-phase timeout under high latency) | **OPEN** / medium / new v2 — latency confound |
| Unit gate | **GREEN** (4320 passed / 6 skipped, ruff + mypy clean) |
| Field re-run | **DONE v2** — 2/5 delivered (P2, P4); F-6/F-7/P6 remain |
| v1.0 tag | **NOT READY** — fix F-6; confirm F-7/P6 under normal latency |

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
