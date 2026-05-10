# Anti-Fragility Playbook (v0.24.0+)

A field guide for diagnosing and recovering from AutoDev failures.
Each entry maps a **symptom** to the relevant ledger op / log event,
the underlying mechanism, and the canonical operator action.

This document complements `docs/huge_repo_guide.md` (cascading
defaults for huge repos) and ADR-0043 (huge_repo_mode design).

---

## Symptom triage table

| Symptom | Likely log / ledger signal | Mechanism |
|---|---|---|
| Run hung > 10 min, no progress | `qa.hallucination_guard.regex_timeout` | Watchdog (v0.22.1 A1) fired; worst-case regex skipped per-file |
| `autodev resume` reports stuck tasks | `PhaseStuckError` (v0.22.2 B2) | Non-terminal-non-pending tasks; reaper runs next on resume |
| Lock file 0 bytes (legacy) | (no signal — silent) | Pre-v0.23.0 C3 lockfile; harmless |
| Lock file refuses to clear | `lockfile.held_by_active_process pid=X` | Another orchestrator is alive; kill it before resuming |
| Architect emitted bad paths | `EditScopeViolation … (normalized: …)` | Path validator (v0.22.4 B4) — retry envelope sent automatically |
| Evidence written but task pending | `reconcile_evidence …promoted=N` | Atomic-evidence reconciler (v0.22.3 B3) auto-promoted on resume |
| Subprocess leaked after kill | none (unexpected) | File a bug — v0.23.0 C3's CancelledError path should kill children |
| Plan tournament > 60 min on huge repo | `plan_phase.huge_repo_fast_path` | Fast-path (v0.23.0 C4) should fire — check `is_huge` probe |
| Secretscan flooding the log | `secretscan_huge_repo` warn | Auto-skip (v0.22.1 A2) didn't fire — check `is_huge` |

---

## Recovery recipes

### "AutoDev is unresponsive but the lock is still held"

1. `ps aux | grep autodev` — confirm the process is alive.
2. `cat .autodev/.lock` — note the recorded PID.
3. If the PID is the unresponsive process: `kill <pid>`. The C3
   SIGTERM handler logs `cli.shutdown_signal received signal=SIGTERM`
   and runs the `plan_lock` `finally` to release. Wait 30 s.
4. If `kill` doesn't release: `kill -9 <pid>`. The fcntl advisory
   lock dies with the process; the lock file content lingers but is
   harmless (next acquire sees the dead PID and overwrites).
5. `autodev resume`. The reconciler + reaper run automatically.

### "Run completed cleanly but I want to inspect what got skipped"

```bash
autodev metrics regex-timeouts --top 20
# Shows the most-frequent regex_timeout files. Investigate whether
# a real bug got hidden — the watchdog skipped them so the gate
# couldn't surface real findings.
```

### "Recover an interrupted Unity-class run"

The 2026-05-09 Unity stall is the canonical recovery test. After
v0.22.1 + v0.22.2 + v0.22.3 + v0.23.0:

```bash
cd /path/to/unity
ls .autodev/.lock                          # check for stale lock
# (v0.23.0 C3: dead-PID locks auto-clear on resume)
autodev resume
# 1. reconcile_evidence_vs_ledger promotes orphan success-evidence to coded
# 2. reap_orphans reverts wedged tasks back to pending
# 3. dispatcher re-picks up reverted-or-promoted tasks fresh
# 4. v0.22.1 A1 prevents the regex hang on retry
```

If this fails (PhaseStuckError despite the reaper having run): file
a bug with the offending task IDs and the last 50 ledger entries.

### "Operator wants to flag a regex pattern for follow-up"

Every `regex_timeout` carries `{path, timeout_s, gate}` in the
ledger. Pipe through your incident tracker:

```bash
autodev metrics regex-timeouts --report jsonl | jq '.path' | sort -u
# Unique offenders. Each row is a (file, gate) pair —
# the same file can timeout multiple times if it grows.
```

### "Auditing a long-running run for FSM correctness"

```bash
autodev metrics export-corpus --out /tmp/corpus.jsonl
# Redacted tournament + phase-review outcomes for retrospective
# analysis. Source text is hashed; payload metadata is preserved.
# Phase 6 corpus collection per ADR-0042.
```

---

## When to file a bug vs. when to expect this

**Expected behaviors** (no bug):
- `regex_timeout` events on huge headers (Unity-class C++ files
  routinely contain templates that the watchdog skip-and-warns).
- `secretscan_huge_repo` warns when skipping on huge repos.
- `plan_phase.huge_repo_fast_path` log when single-branch fallback
  fires.
- Multiple `attempt_started` ops for the same task (retry attempts).

**Likely bugs** (file an issue):
- `reap_orphans` runs on a clean (just-completed) workspace.
- `reconcile_evidence` promotes more than 1 task per resume after a
  clean shutdown.
- `EditScopeViolation` with `normalized:` form that's empty.
- Subprocess from `claude_code` adapter outliving the orchestrator
  (D-6 finding from 2026-05-09 should prevent this; if observed,
  C3's CancelledError path has a regression).
- Watchdog timeout on a file that completes in under 10 seconds
  outside the gate.

---

## Forensic tools

| Need | Tool |
|---|---|
| Inspect ledger schema + chain | `python -c "from state.ledger import stream_entries; for e in stream_entries(Path('.')): print(e.seq, e.op)"` |
| Validate hash chain | `state.ledger.read_entries(cwd)` raises on corruption |
| Find orphan evidence | `ls .autodev/evidence/*-developer.json` and grep ledger for matching `update_task_status(coded)` |
| Inspect recent task dispatches | grep ledger for `attempt_started` ops |
| Show ledger size growth | `wc -l .autodev/plan-ledger.jsonl` (Unity stall: 140 entries / 2.97 MB) |
