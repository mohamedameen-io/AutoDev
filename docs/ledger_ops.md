# Plan-ledger ops catalogue

The plan-ledger (`.autodev/plan-ledger.jsonl`) is the append-only,
hash-chained record of every state mutation and forensic breadcrumb a
run produces. Each entry is a single line of JSON with the canonical
shape:

```json
{
  "seq": 17,
  "timestamp": "2026-05-16T12:34:56.789012+00:00",
  "session_id": "...",
  "op": "<op_name>",
  "payload": { ... },
  "prev_hash": "...",
  "self_hash": "..."
}
```

This document catalogues the ops added in v0.33 through v0.36 along
with their payload shapes. The full canonical list lives in
`src/state/ledger.py`'s `LedgerOp` Literal; ops not catalogued below
were stable before v0.33 and their shapes are documented inline in
that module.

## v0.33.0 — Plan-phase unblock (Tier A)

### `path_validation_resolved_via_plan_global`

Audit-only. Plan-level `[new]` admission unblocked a path the per-task
validator was about to reject. Plan state is unchanged because the
path was admitted, not dropped.

```json
{"task_id": "1.1", "path": "notes/foo.md", "declaring_task_id": "1.2"}
```

## v0.34.0 — Execute-phase unblock (Tier B)

### `sparse_worktree_expanded`

Audit-only. Sparse-checkout was widened to admit sibling C/C++
headers so include-chain QA gates keep working on partial trees.

```json
{"task_id": "1.1", "added_paths": 4, "mode": "headers"}
```

### `drift_convergence_failure`

Audit-only. Drift verifier exited early because the corrective patch
was ≥90% identical to the prior patch.

```json
{"task_id": "1.1", "similarity": 0.97, "attempt": 2}
```

### `hallucination_finding_downgraded`

Audit-only. Hallucination-guard finding downgraded to warning because
the project runs sparse checkouts.

## v0.35.0 — Knowledge-base hygiene (Tier C)

### `knowledge_entry_quarantined`

Audit-only. Entry was soft-flagged as low-yield because its
`applied_count` crossed the floor with a success ratio below the
ceiling. The flip itself lives in `.autodev/knowledge.jsonl`; the per-
decision counts live in `.autodev/quarantine_audit.jsonl`.

```json
{
  "entry_id": "...",
  "applied_count": 12,
  "succeeded_after_count": 0,
  "reason": "low_success_ratio"
}
```

### `knowledge_lesson_credited`

Audit-only. An injected lesson preceded a successful task completion
and its `succeeded_after_count` was incremented.

```json
{"entry_id": "...", "task_id": "1.1", "role": "developer", "tier": "swarm"}
```

### `critic_evidence_rejected`

Audit-only. A critic-derived TournamentEvent was rejected by the
evidence-quality gate before bumping confirmations.

```json
{"role": "critic", "reason": "thin_evidence", "family": "...", "event_type": "..."}
```

### `knowledge_entry_promotion_rejected`

Audit-only. Promotion to hive was rejected because the entry failed
the new conjunct (confirmations floor or `succeeded_after_count > 0`).

```json
{"entry_id": "...", "reason": "min_confirmations"}
```

## v0.36.0 — Retries, budgets, forensics, spec hygiene (Tiers D + E + F + G)

### `architect_attempt_failed`

Audit-only. Per-architect-attempt failure breadcrumb. Records model +
duration + the primary rejection class so `autodev status --blocked`
can render an attempt timeline without scanning the orchestrator's
structured log.

```json
{
  "attempt": 2,
  "model": "claude-opus-4-7",
  "duration_s": 1.23,
  "rejection_count": 4,
  "primary_class": "new_md_deliverable"
}
```

### `recovery_tier_attempted`

Audit-only. Per-recovery-tier transition. One op per tier (4–7) with
`outcome ∈ {"applied", "noop", "failed"}` and the from/to state.

```json
{
  "tier": 4,
  "outcome": "applied",
  "reason": "recurrent_path_failure",
  "from_state": "undegraded",
  "to_state": "dropped:notes/foo.md"
}
```

### `path_rejection_recorded`

Audit-only. Per-rejection breadcrumb classifying the architect's path-
validation miss into a design class so the status surface can render
the right action template.

```json
{"task_id": "1.1", "path": "notes/foo.md", "class": "new_md_deliverable"}
```

### `architect_model_changed_for_retry`

Audit-only. Architect retry routed to a different model because the
failure was structural (missing-on-disk, new-md-deliverable) rather
than reasoning.

```json
{
  "attempt": 2,
  "from_model": "claude-opus-4-7",
  "to_model": "sonnet",
  "rejection_class": "missing_on_disk"
}
```

### `huge_repo_multiplier_applied`

Audit-only. Huge-repo multiplier was applied at dispatch time. The
actual budget mutation lives only in the `AgentInvocation` constructed
at the call site — this op is the forensic counter.

```json
{"role": "explorer", "base": 10, "multiplier": 3.0, "effective": 30}
```

### `retry_budget_scaled`

Audit-only. Retry-attempt budget multiplier was applied (retry attempt
≥ 2 by default).

```json
{"task_id": "1.1", "attempt": 2, "base": 20, "effective": 40}
```

### `network_probe_failed`

Audit-only. Per-attempt probe failure. Emitted both on each retry
attempt AND on the final attempt; the `final` flag distinguishes.

```json
{
  "adapter": "claude_code",
  "attempt": 3,
  "last_error": "PONG probe timed out after 10s",
  "suggestion": "check VPN / proxy / adapter health",
  "final": true
}
```

### `spec_validation_failed`

Audit-only. CLI front-gate rejection at `autodev plan`.

```json
{"path": "fix", "reasons": ["spec_too_short", "spec_no_scope_markers"]}
```

### `plan_phase_budget_escalation` (payload corrected in v0.36)

Existing op; the v0.32.0 `{0 → 0}` placeholder in the recovery-tier
breadcrumb has been replaced with truthful `base_max → retry_max`
values. Wire shape is unchanged; old readers continue to parse.

```json
{
  "from_max_turns": 5,
  "to_max_turns": 7,
  "attempt": 3,
  "reason": "phase1_4_recovery_entered"
}
```

## Replay semantics

All ops in this document are audit-only — replay treats them as no-ops
on the in-memory plan. The actual state mutations (task statuses,
phase metadata, edit-scope changes) flow through the existing
`update_task_status`, `update_phase_meta`, and `append_corrective_tasks`
ops emitted alongside. New code adding ops follows the same pattern:
register the literal in `LedgerOp`, add the name to the audit-only
tuple in `_apply_op`, document the payload shape here.
