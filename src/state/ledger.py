"""Append-only JSONL plan ledger with CAS hash chaining + replay.

Each ledger entry is a JSON object on its own line. Entries form a
hash-chained append-only log:

    entry[n].prev_hash == entry[n-1].self_hash

The genesis entry has ``prev_hash == ""``. Replay walks the chain and raises
:class:`~errors.LedgerCorruptError` if any link is broken (tampering,
truncated mid-write, or an entry was dropped).

Supported ops:

  - ``init_plan``: embeds the initial Plan payload so the ledger is
    self-sufficient (no plan.json required for replay).
  - ``update_plan``: overwrites Plan with a new payload (coarse-grained).
  - ``update_task_status``: mutates a single task's status. v0.30.0 Bug
    4: payload may also carry forensic-only ``api_error_status`` (int)
    and ``last_adapter_subtype`` (str) keys propagated from the most
    recent adapter result. These are NOT mutated onto :class:`Task`
    (the model has no field for them) — they live on the ledger entry
    purely so post-mortems can grep ``op="update_task_status" status="blocked"``
    entries for the API status / subtype that triggered the block,
    without diving into ``.autodev/debug/*.txt`` traceback dumps.
  - ``adapter_failure``: v0.30.0 Bug 4 audit-only op. Appended once
    per ``AgentResult`` with ``success=False`` from the
    :func:`execute_phase.delegate` call site (regardless of whether
    the failure is fatal, transient, or eventually retried). Payload
    shape: ``{task_id: str, api_error_status: int | None,
    subtype: str | None, error: str | None, attempt_n: int}``.
    Replay treats it as a no-op — it is forensics for "how many
    transient failures preceded the block / completion" without
    grepping ``.autodev/debug/``.
  - ``append_evidence``: audit-only record that evidence was produced.
  - ``mark_blocked`` / ``mark_complete``: task terminals.
  - ``snapshot``: embeds the current Plan — lets replay short-circuit from
    the last snapshot instead of walking from genesis.

All disk I/O here assumes the caller holds :func:`plan_lock`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from errors import LedgerCorruptError
from autologging import get_logger
from state.paths import autodev_root, ledger_path
from state.schemas import Plan


logger = get_logger(__name__)


LedgerOp = Literal[
    "init_plan",
    "update_plan",
    "update_task_status",
    "append_evidence",
    "mark_blocked",
    "mark_complete",
    "snapshot",
    "plan_tournament_complete",
    "impl_tournament_complete",
    # v0.9.0: phase-review tournament ops.
    # ``phase_review_complete`` is audit-only (no plan mutation, mirrors
    # ``plan_tournament_complete``). ``append_corrective_tasks`` and
    # ``update_phase_meta`` mutate the plan — see :func:`_apply_op`.
    "phase_review_complete",
    "append_corrective_tasks",
    "update_phase_meta",
    # v0.11.0: parallel execute_phase ops.
    # ``mark_in_flight`` / ``clear_in_flight`` are observability-only —
    # the in-flight set is in-memory on the PlanManager and resumes
    # rebuild it from scratch (NOT replayed). ``mark_blocked_descendants``
    # is a single-entry batch cascade-block carrying the phase_id, the
    # failed task id, and the list of newly-blocked task ids; replay
    # walks the list and applies status="blocked" + blocked_reason.
    "mark_in_flight",
    "clear_in_flight",
    "mark_blocked_descendants",
    # v0.12.0: multi-branch plan-tournament audit ops. All three are
    # observability-only — they do NOT mutate plan state. Replay treats
    # them as no-ops. Payload contents:
    # - ``multi_branch_plan_tournament_start``:
    #   ``{spec_hash, n_branches, branch_seeds: list[str]}`` (seeds
    #   stringified because ``int(spec_hash, 16)`` exceeds JSON safe int).
    # - ``multi_branch_meta_merge_complete``:
    #   ``{spec_hash, n_survivors, n_steps, meta_passes}``.
    # - ``multi_branch_plan_tournament_complete``:
    #   ``{spec_hash, n_branches, n_survivors, final_hash}``.
    "multi_branch_plan_tournament_start",
    "multi_branch_meta_merge_complete",
    "multi_branch_plan_tournament_complete",
    # v0.15.0: stuck-recovery escalation ladder + PRM ops. All four are
    # observability-only — they do NOT mutate plan state. The
    # task.status / blocked_reason mutations live in the regular
    # ``update_task_status`` op emitted alongside.
    # - ``stuck_refine``:  payload ``{task_id, reason, critic_response_excerpt}``.
    # - ``stuck_pivot``:   same payload shape.
    # - ``soft_blocker_handoff``: same payload shape.
    # - ``course_correction_emitted``: payload
    #   ``{task_id, taxonomy, pattern, suggestion}`` from PRM detector.
    "stuck_refine",
    "stuck_pivot",
    "soft_blocker_handoff",
    "course_correction_emitted",
    # v0.26.1 patch G: architect-consult rung fired for a task. Audit-only
    # — does NOT mutate plan state. Payload shape:
    # ``{task_id, reason, architect_response_excerpt}`` (mirrors
    # ``stuck_pivot`` / ``soft_blocker_handoff`` shape). The corrective
    # sub-tasks landing from the architect's response are recorded via
    # the regular ``append_corrective_tasks`` op emitted alongside.
    "architect_consult",
    # v0.26.2 Phase 3: persistent-failure drop fired during the
    # architect-retry loop. Audit-only — does NOT mutate plan state on
    # replay (the new plan with the dropped entry is persisted via
    # ``init_plan`` alongside). Payload shape:
    # ``{path: str, reason: str, suggestion: str, attempt: int,
    #   recurrence_count: int}`` where ``attempt`` is 1-indexed
    # architect-attempt number and ``recurrence_count`` is the
    # number of times the same ``(path, reason)`` had recurred.
    "scope_entry_dropped",
    # v0.16.0: drift-verifier complete. Audit-only — does NOT mutate plan
    # state. Payload shape: ``{phase_id, passed, drift_findings: list[str],
    # evidence_path}``. Replay treats it as a no-op; the actual outcome
    # override (accept_phase=False with corrective_direction) is recorded
    # via the regular ``append_corrective_tasks`` op emitted alongside.
    "drift_verifier_complete",
    # v0.17.0 S4: repeated-hypothesis detector tagged a multi-branch
    # branch. Audit-only — advisory; does NOT mutate plan state. Payload
    # shape: ``{spec_hash, branch_index, family, similarity}`` where
    # ``similarity`` is the rounded bigram-Jaccard score that triggered
    # the match. Used by forensics + future plateau detection to
    # reconstruct "we tried this same approach before".
    "hypothesis_repeat_detected",
    # v0.17.0 S2: web-search escalation rung fired for a task. Audit-only
    # — does NOT mutate plan state. Payload shape:
    # ``{task_id, query, results_count, search_count_after}``. The
    # actual ``WEB_CONTEXT:`` splice into the next critic prompt is the
    # caller's responsibility; this op is forensics.
    "web_search_invoked",
    # v0.18.0 A2: multi-branch phase-review fan-out audit ops. All three
    # are observability-only — they do NOT mutate plan state. Replay
    # treats them as no-ops.
    # - ``multi_branch_phase_review_start``:
    #   ``{phase_id, n_branches, lanes: list[str | None]}``.
    # - ``multi_branch_phase_review_meta_merge_complete``:
    #   ``{phase_id, n_survivors, n_branches, accept_votes, reject_votes,
    #     majority_accept}``.
    # - ``multi_branch_phase_review_complete``:
    #   ``{phase_id, n_branches, n_survivors, winner, accept_phase}``.
    "multi_branch_phase_review_start",
    "multi_branch_phase_review_meta_merge_complete",
    "multi_branch_phase_review_complete",
    # v0.18.0 B2: plateau detector telemetry. Audit-only — advisory.
    # - ``plateau_detected``:
    #   ``{family | None, window, kind: "per_family" | "cross_family",
    #     event_count: int, winner_promoted_count: int}``.
    # - ``plateau_forced_lane_change``:
    #   ``{branch_index, prior_lane, new_lane, family | None}``.
    "plateau_detected",
    "plateau_forced_lane_change",
    # v0.21.0 A2: multi-branch impl-tournament fan-out audit ops. All
    # observability-only — they do NOT mutate plan state. Replay treats
    # them as no-ops. Payload contents:
    # - ``multi_branch_impl_start``:
    #   ``{task_id, n_branches, lanes: list[str | None]}``.
    # - ``multi_branch_impl_meta_merge_complete``:
    #   ``{task_id, n_survivors, n_branches}``.
    # - ``multi_branch_impl_complete``:
    #   ``{task_id, n_branches, n_survivors, winner_diff_bytes}``.
    "multi_branch_impl_start",
    "multi_branch_impl_meta_merge_complete",
    "multi_branch_impl_complete",
    # v0.21.0 B2: speculative-execution audit ops. Observability-only.
    # - ``speculative_started``:
    #   ``{task_id, parent_task_id}``.
    # - ``speculative_rolled_back``:
    #   ``{task_id, parent_task_id, reason}``.
    # - ``speculative_committed``:
    #   ``{task_id, parent_task_id}``.
    "speculative_started",
    "speculative_rolled_back",
    "speculative_committed",
    # v0.22.2 B3: pre-flight marker emitted before developer dispatch so
    # resume can detect orphan evidence (evidence written but
    # ``update_task_status(coded)`` never reached because the process died
    # between :line 1727 (write_evidence) and :line 1774 (status=coded)).
    # Audit-only — NOT mutated by ``_apply_op``. Payload shape:
    # ``{task_id, attempt_n, started_at, session_id}``. Used by
    # :meth:`PlanManager.reconcile_evidence_vs_ledger` at resume time.
    "attempt_started",
    # v0.22.2 B3: emitted by ``reconcile_evidence_vs_ledger`` summarizing
    # the auto-promotions and discrepancies it acted on at resume.
    # Audit-only. Payload: ``{promoted: list[str], discrepancies: list[dict]}``.
    "reconcile_evidence",
    # v0.22.2 B1: emitted at end of ``PlanManager.reap_orphans`` summarizing
    # the wedged tasks reverted to ``pending`` on resume. Audit-only;
    # the actual ``update_task_status`` ops emitted per-task carry the
    # state mutation. Payload: ``{reaped_task_ids: list[str], reason: str}``.
    "reap_orphans",
    # v0.23.0 C6: emitted by the ``hallucination_guard`` watchdog (and any
    # future regex-on-content QA gate) when the per-file timeout fires.
    # Audit-only. Payload: ``{path, timeout_s, gate}``. Lets operators
    # surface common offenders via ``autodev metrics regex-timeouts``
    # rather than reading raw stderr.
    "regex_timeout",
    # v0.27 Phase 4 (audit §4): granular persistent-drop telemetry. All
    # audit-only — the mutation of plan state lives in the regular
    # ``init_plan`` / ``snapshot`` op emitted alongside. Payload shape:
    # ``{path: str, reason: str, attempt: int, recurrence_count: int}``
    # for the four ``_dropped`` variants; ``{task_id: str, reason: str}``
    # for ``task_auto_skipped``; ``{exc_class: str, attempt: int,
    # recurrence_count: int, archived_path: str | None}`` for the
    # ``architect_persistent_*_error`` variants. The v0.26.2 catch-all
    # ``scope_entry_dropped`` op is preserved for back-compat (older
    # ledgers replay cleanly); new code emits the granular variant
    # alongside so forensics can see exactly which site lost the entry.
    "task_files_entry_dropped",
    "task_files_new_entry_dropped",
    "task_extended_scope_entry_dropped",
    "phase_edit_scope_entry_dropped",
    "task_auto_skipped",
    "architect_persistent_parse_error",
    "architect_persistent_pyd_error",
    # v0.27 Phase 5 (audit §5): post-tournament structural-validity
    # gate rejected the refined plan markdown. Audit-only; the plan
    # state falls back to the pre-tournament version (already
    # captured in the prior ``init_plan`` op). Payload:
    # ``{reason: str, attempt: int}`` where reason is a short
    # category ("parse_error" | "validate_files_exist" |
    # "persistent_drop_refused").
    "tournament_output_rejected_structurally",
    # v0.27 Phase 3 (audit §3): one task in the plan declared a file
    # outside its resolved edit_scope. The granular per-task block
    # replaces v0.26.2's blanket "block every pending task in every
    # phase" behaviour. Audit-only; the actual status transition
    # flows through the regular ``update_task_status`` op emitted
    # alongside. Payload: ``{task_id, phase_id, file_path, message}``.
    "task_blocked_scope_violation",
    # v0.27 Phase 7 (audit §7): role agent emitted an ``ESCALATE:``
    # line at the start of its response. Audit-only; the actual
    # routing to the architect-consult rung lives in the orchestrator
    # caller. Payload: ``{role: str, reason: str, raw_response: str}``.
    "agent_escalated",
    # v0.28.0 Bug 8: ``autodev requeue`` CLI fired a typed task-status
    # reset. Audit-only — the per-task ``status: blocked → pending``
    # transitions flow through the regular ``update_task_status`` ops
    # emitted alongside (one per requeued task). Payload shape:
    # ``{task_ids: list[str], reset_phase_review: bool, source: str}``
    # where ``source`` is the CLI flag ("--task" | "--phase" |
    # "--infrastructure" | "--all-blocked" | "interactive") that
    # triggered the requeue, useful for forensics on "which infra
    # incident motivated this resume".
    "requeue",
    # v0.29.0 Bug 9: ``autodev rewind --to-phase N`` CLI fired a typed
    # multi-phase reset to undo a force-accept. Audit-only — the per-task
    # ``status → pending`` transitions and per-phase ``review_status →
    # None`` mutations flow through the regular ``update_task_status``
    # / ``update_phase_meta`` ops emitted alongside. Payload shape:
    # ``{target_phase_id: str, reset_task_ids: list[str],
    # reset_phase_ids: list[str], archive_dir: str | None,
    # archived_paths: list[str]}`` where ``archive_dir`` is the
    # repo-relative path under ``.autodev/rewound/`` that holds the
    # quarantined evidence/tournament artifacts (``None`` when nothing
    # needed archiving — idempotent re-run).
    "rewind",
    # v0.30.0 Bug 4: per-adapter-failure audit breadcrumb. Appended
    # by :func:`execute_phase.delegate` whenever the adapter returns
    # ``success=False`` (regardless of whether the worker eventually
    # retries the call, escalates to the architect, or blocks the
    # task). Audit-only — does NOT mutate plan state. Payload shape:
    # ``{task_id: str, api_error_status: int | None,
    # subtype: str | None, error: str | None, attempt_n: int}``.
    # The ``attempt_n`` is the developer's retry_count at dispatch
    # time so post-mortems can correlate "Nth retry that failed"
    # against the regular ``update_task_status`` retry-count bumps.
    # Best-effort write — ledger append failures here MUST NOT mask
    # the underlying adapter failure for the caller.
    "adapter_failure",
    # v0.31.0 (Phase 3): per-(task_id, role) budget escalation
    # breadcrumb. Appended by :func:`execute_phase.delegate` whenever
    # the per-(task_id, role) tracker observes a consecutive
    # ``error_max_turns`` and decides to bump the dispatch's
    # ``max_turns`` / ``timeout_s`` for the next attempt. Audit-only;
    # does NOT mutate plan state. Payload shape:
    # ``{task_id: str, role: str, prior_max_turns: int,
    # new_max_turns: int, prior_timeout_s: int | None,
    # new_timeout_s: int | None, attempt: int}`` where ``attempt`` is
    # the 0-based escalation index (1 = second attempt / 1.5×;
    # 2 = third attempt / 2.0×). Replay treats it as a no-op — the
    # actual ``AgentInvocation`` budget mutation lives only in the
    # caller's local state at the moment of dispatch.
    "budget_escalation",
    # v0.32.0 Phase 1.2: plan-phase architect budget escalation
    # breadcrumb. Mirrors ``budget_escalation`` but emitted only by
    # the plan-phase architect-retry loop (scope_id="plan_phase").
    # Audit-only; replay no-op. Payload shape:
    # ``{from_max_turns: int, to_max_turns: int, attempt: int,
    # reason: str}``.
    "plan_phase_budget_escalation",
    # RECOVERY-CONTRACT §7 Step 2 (gate R4): resume-safe per-(scope_id, role)
    # consecutive-``error_max_turns`` COUNTER value. Audit-only; replay no-op.
    # Persisted on EVERY counter change in the production path (last-value-wins:
    # the current attempt for a key = the ``attempt`` of the highest-seq
    # ``budget_cycle`` op for that key). Rehydrated into the
    # ``BudgetEscalationTracker`` on construction so ``autodev resume`` does NOT
    # restart the escalation ladder at 0. Distinct from ``budget_escalation``
    # (which records the max_turns BUMP event); this op records the COUNTER.
    # Payload shape: ``{scope_id: str, role: str, attempt: int}`` (attempt=0 on
    # reset).
    "budget_cycle",
    # v0.32.0 Phase 2: review-tournament lifecycle ops. All four are
    # audit-only — they do NOT mutate plan state on replay (the
    # underlying task status changes flow through the regular
    # ``update_task_status`` ops emitted by the orchestrator's
    # retry/escalation FSM). Payload shapes:
    #
    # - ``review_tournament_started``:
    #   ``{tournament_id: str, task_id: str, diff_bytes: int,
    #   convergence_k: int, max_rounds: int, num_judges: int}``.
    #   Emitted once at the start of the loop so forensics can
    #   correlate "this task triggered a review tournament" with the
    #   final outcome.
    # - ``review_tournament_judged``:
    #   ``{tournament_id: str, task_id: str, round: int, winner: str,
    #   borda_scores: dict[str, int], valid_judges: int}``.
    #   Emitted once per round (one per Borda tally). Lets
    #   post-mortems reconstruct "which round flipped, which round
    #   stuck" without parsing the on-disk artifact tree.
    # - ``review_tournament_converged``:
    #   ``{tournament_id: str, task_id: str, rounds: int,
    #   final_winner: str, final_verdict: str}``.
    #   Emitted on the do-nothing convergence path (A wins
    #   ``convergence_k`` times in a row). The orchestrator advances
    #   or soft-blocks WITHOUT another developer-refine cycle.
    # - ``review_tournament_escalated``:
    #   ``{tournament_id: str, task_id: str, rounds: int,
    #   final_winner: str, escalation_reason: str}``.
    #   Emitted on the max-rounds-without-convergence path. The
    #   orchestrator routes the task to ``critic_sounding_board`` so
    #   the existing escalation ladder takes over.
    "review_tournament_started",
    "review_tournament_judged",
    "review_tournament_converged",
    "review_tournament_escalated",
    # v0.32.0 Phase 4.5: knowledge-aware retry-loop telemetry. All three
    # are audit-only — they do NOT mutate plan state on replay. The
    # underlying task-status changes flow through the regular
    # ``update_task_status`` ops emitted by the orchestrator's
    # retry/escalation FSM. Mirrors the ``course_correction_emitted``
    # op shape.
    #
    # - ``repetition_loop_detected``: payload
    #   ``{task_id: str, discard_count: int, pivot_count: int,
    #   target_files: list[str], detected_at_attempt: int}``. Emitted
    #   when :func:`orchestrator.escalation_ladder.next_step` overrides
    #   REFINE → PIVOT (or continue → PIVOT) because the PRM rule-based
    #   detector observed three consecutive identical
    #   ``(role, action, target_files)`` dispatches. Lets forensics see
    #   "the ladder gated this task on a repetition loop, not on the
    #   ordinary discard-count threshold".
    # - ``recovery_action_chosen``: payload
    #   ``{task_id: str, action: str, reason: str, next_step: str}``
    #   where ``action`` is one of the
    #   :data:`orchestrator.repetition_recovery.RecoveryAction` literals
    #   and ``next_step`` is the resolved ladder rung. Emitted once per
    #   stuck-recovery decision; pairs with the regular ``stuck_*`` ops
    #   that follow.
    # - ``tactic_switch``: payload
    #   ``{task_id: str, prior_tactic: str | None, new_tactic: str,
    #   guidance_source: str}``. Emitted when the recovery policy
    #   switches from one tactic to another (e.g. refine → pivot, or
    #   refine_minimal → refine_with_kb). ``guidance_source`` is one
    #   of ``"critic" | "kb_lookup" | "architect" | "prm_pattern"``.
    "repetition_loop_detected",
    "recovery_action_chosen",
    "tactic_switch",
    # v0.33.0 A1: file-existence validator admitted a path because a
    # sibling task in the same plan declared it ``[new]``. Audit-only;
    # plan state is unchanged. Payload:
    # ``{task_id, path, declaring_task_id}``. Lets forensics distinguish
    # "real on-disk file" from "deferred to creator-task" admissions.
    "path_validation_resolved_via_plan_global",
    # v0.34.0 B2: sparse-checkout worktree was expanded with sibling
    # C/C++ headers so the QA gates retain include-chain context.
    # Audit-only. Payload: ``{task_id, added_paths: int, mode: str}``.
    "sparse_worktree_expanded",
    # v0.34.0 B3: drift verifier exited because the corrective patch
    # was ≥90% identical to the prior patch. Audit-only. Payload:
    # ``{task_id, similarity: float, attempt: int}``.
    "drift_convergence_failure",
    # v0.35.0 C1: knowledge entry was soft-flagged as low-yield because
    # its applied_count crossed the floor with a success ratio below
    # the ceiling. Audit-only — the flip lives in knowledge.jsonl and
    # the per-decision counts live in quarantine_audit.jsonl; the
    # ledger op exists so ops dashboards can count quarantine events
    # without scraping the per-project audit file. Payload:
    # ``{entry_id, applied_count, succeeded_after_count, reason}``.
    "knowledge_entry_quarantined",
    # v0.35.0 C1 prerequisite: an injected lesson preceded a
    # successful task completion and its succeeded_after_count was
    # incremented. Audit-only. Payload:
    # ``{entry_id, task_id, role, tier}``.
    "knowledge_lesson_credited",
    # v0.35.0 C2: a critic-derived TournamentEvent was rejected by
    # the evidence-quality gate before bumping confirmations. Audit-
    # only — the underlying critic output is still observable via
    # the normal critic-dispatch trace. Payload:
    # ``{role, reason, family, event_type}``.
    "critic_evidence_rejected",
    # v0.35.0 C3: promotion to hive was rejected because the entry
    # failed the new conjunct (confirmations floor or
    # succeeded_after_count > 0). Audit-only. Payload:
    # ``{entry_id, reason: "min_confirmations" | "no_success"}``.
    "knowledge_entry_promotion_rejected",
    # v0.36.0 F1: per-architect-attempt failure breadcrumb. Audit-only;
    # plan state mutations flow through ``init_plan`` ops emitted on
    # the eventual successful attempt or the recovery path. Payload:
    # ``{attempt: int, model: str, duration_s: float,
    # rejection_count: int, primary_class: str}``.
    "architect_attempt_failed",
    # v0.36.0 F1: per-recovery-tier transition. Audit-only. Payload:
    # ``{tier: int, outcome: "applied" | "noop" | "failed",
    # reason: str, from_state: str | None, to_state: str | None}``.
    "recovery_tier_attempted",
    # v0.36.0 D1: per-rejection breadcrumb classifying the architect's
    # path-validation miss into a design class so F3's status surface
    # can render the right action template. Audit-only. Payload:
    # ``{task_id: str, path: str, class: str}``.
    "path_rejection_recorded",
    # v0.36.0 D3: architect retry routed to a different model because
    # the failure was structural (missing-on-disk, new-md-deliverable)
    # rather than reasoning. Audit-only. Payload:
    # ``{attempt: int, from_model: str, to_model: str,
    # rejection_class: str}``.
    "architect_model_changed_for_retry",
    # v0.36.0 E1: huge-repo multiplier was applied at dispatch time.
    # Audit-only; the actual budget mutation lives only in the
    # :class:`AgentInvocation` constructed at the call site. Payload:
    # ``{role: str, base: int, multiplier: float, effective: int}``.
    "huge_repo_multiplier_applied",
    # v0.36.0 E2: retry-attempt budget multiplier was applied. Audit-
    # only; same rationale as ``huge_repo_multiplier_applied``. Payload:
    # ``{task_id: str, attempt: int, base: int, effective: int}``.
    "retry_budget_scaled",
    # v0.36.0 F2: structured probe failure (3-attempt backoff
    # exhausted, or final attempt raised). Audit-only — the CLI
    # surfaces the exception body separately via exit code 5. Payload:
    # ``{adapter: str, attempt: int, last_error: str,
    # suggestion: str, final: bool}``. ``final=True`` only on the
    # terminal attempt.
    "network_probe_failed",
    # v0.36.0 G1: spec-validator rejection at the ``autodev plan``
    # entry point. Audit-only. Payload:
    # ``{path: str, reasons: list[str]}``. ``path`` carries the first
    # 200 chars of the intent string (CLI passes intent text, not a
    # file path) so post-mortems can correlate the rejection back to
    # the operator's command line.
    "spec_validation_failed",
    # v0.37.0 H2: phase exhausted its cumulative correction-task budget
    # (``cfg.max_corrective_tasks_per_phase``). Fired from one of two
    # sites: (a) the orchestrator's architect-refine / phase-review
    # paths when ``remaining_budget == 0`` BEFORE the parser is invoked,
    # carrying ``{phase_id, task_id, cap: int, action: str}``; (b) the
    # plan_manager's defensive cap when an upstream caller bypasses the
    # budget computation, carrying
    # ``{phase_id, cap: int, dropped: int, defended: True}``. The
    # ``defended`` flag distinguishes the two so dashboards can spot
    # the upstream regression separately from the legitimate cap-hit.
    # Plan state mutations (``review_status="capped"``, task soft-block)
    # flow through the regular ``update_phase_meta`` /
    # ``update_task_status`` ops emitted alongside.
    "corrective_cap_reached",
    # v0.38.0 HK10: per-boot adapter-selection breadcrumb. Audit-only.
    # Fired exactly once per CLI entry (plan / execute / resume /
    # tournament phase-review) right after :func:`adapters.detect.get_adapter`
    # returns. Payload mirrors the ``detect_platform.selected`` log:
    # ``{platform: str, source: "preferred" | "trigger_context" |
    # "env" | "fitness" | "fallback", trigger_context_detected: bool,
    # healthcheck_ok: bool}``. Replay treats it as a no-op — the
    # selected adapter is recreated by ``get_adapter`` at resume time.
    # Forensics goal: answer "which selection arm fired" without
    # re-grepping stdout logs after the fact, and let v0.39
    # retrospectives quantify how often the fallback path actually
    # runs.
    "adapter_selected",
    # v0.38.0 I3 (HK5): stuck skip_corrective_round loop suspected.
    # Audit-only — fired when a phase's
    # ``Phase.metadata["skip_corrective_count"]`` counter reaches
    # ``>= 3`` without an intervening successful corrective round.
    # Payload shape: ``{phase_id: str, count: int, action: str}`` where
    # ``action`` is the configured
    # :attr:`AutodevConfig.corrective_cap_action` so the retrospective
    # can correlate the loop against the cap-action regime in force.
    # The orchestrator does NOT auto-soft-block on this in v0.38.0 —
    # the goal is to collect frequency data first. Plan state is not
    # mutated by this op; the counter itself flows through the regular
    # ``update_phase_meta`` op.
    "skip_corrective_loop_detected",
    # F-2 (field-finding): phase-scoped NON-CONVERGENCE ceiling tripped. Audit-
    # only, LOUD fail-fast breadcrumb — fired when a phase has regenerated
    # ``cfg.resolver.max_corrective_cycles_per_phase`` consecutive same-
    # ``failure_class`` correctives without forward progress, so the resolver
    # STOPS minting and declines (the originating ``block_task`` then commits the
    # single terminal block). This is the distinct, greppable attribution for the
    # unbounded corrective-regeneration loop that timed hard tasks out at the
    # 40-min execute wall. Payload: ``{task_id, phase_id, failure_class, cycles,
    # ceiling, reason}``. Plan state is not mutated by this op (the per-phase
    # counter flows through ``update_phase_meta``; the block flows through
    # ``update_task_status``); replay treats it as a no-op.
    "corrective_nonconvergent_ceiling",
    # v0.38.0 I2 (HK4): ``autodev requeue --capped-phases`` was invoked.
    # Audit-only breadcrumb mirroring the existing ``requeue`` op
    # rationale — the per-task ``status: blocked → pending`` transitions
    # flow through the regular ``update_task_status`` ops emitted
    # alongside by :meth:`PlanManager.requeue_tasks`. Payload shape:
    # ``{phases: list[str], task_count: int}`` where ``phases`` is the
    # set of phase ids whose ``review_status == "capped"`` matched the
    # selector and ``task_count`` is the number of blocked tasks
    # actually queued for the requeue (post-idempotency-filter, may
    # be 0 on a no-op re-run). Forensics goal: correlate H2/I3 cap-fires
    # with operator-triggered bulk recovery without scraping CLI logs.
    "requeue.capped_phases_selected",
    # v0.39.0 (Cluster A2b): runtime auto-soft-pass fallback fired. After
    # >=2 consecutive test-runner ``capture_failed`` diagnoses on a huge
    # repo, the orchestrator auto-enables
    # ``treat_unrunnable_tests_as_no_tests`` in-memory for the rest of the
    # session. Audit-only — the flag lives only on the in-memory cfg (the
    # profile is never written to ``.autodev/config.json``), so replay
    # treats this op as a forensic no-op. Payload shape:
    # ``{task_id: str, reason: str, consecutive_capture_failed: int}``.
    "auto_soft_pass_enabled",
    # v0.39.0 (Cluster C2): a ``complex`` task on a huge repo looks
    # under-decomposed (too many files / one very large file), or it
    # exhausted its scaled budget with ``error_max_turns``. Audit-only —
    # the orchestrator never mutates or rejects the plan in response; the
    # op is a forensic breadcrumb that lets retrospectives correlate
    # huge-repo brute-force failures with the tasks that should have been
    # split. Emitted from two sites: the planner advisory in
    # ``plan_phase._advise_task_decomposition`` (``source="planner_advisory"``,
    # post-parse, before ``init_plan``) and the runtime developer-failure
    # block in ``execute_phase`` (``source="runtime"``, on the first one or
    # two ``error_max_turns`` retries). Payload shape:
    # ``{task_id: str, source: str, attempt: int, file_count: int,
    # files: list[str], complexity: str}``. Replay treats it as a no-op.
    "task_under_decomposed",
    # Phase 0 (cost/time telemetry): per-invocation cost breadcrumb. Emitted
    # once for EVERY adapter round-trip — the main delegate path AND every
    # tournament invocation (judges + developers/test_engineers), which
    # otherwise bypass ``GuardrailEnforcer.post_invocation`` and so are
    # invisible to the in-memory ``plan_cost_usd`` accumulator. The
    # per-run total is recovered by summing these ops' ``cost_usd`` over
    # the run window. Audit-only — no plan-state mutation, so replay is a
    # forensic no-op. Payload shape:
    # ``{role: str, task_id: str | None, cost_usd: float, duration_s: float}``.
    "invocation_cost",
    # Tier J (huge-repo): accept an APPROVED-but-turn-exhausted task as done.
    # Emitted by ``execute_phase`` when a research/empty-diff task already has
    # a reviewer ``APPROVED`` verdict on record but the developer keeps
    # hitting ``error_max_turns`` / ``error_max_turns_escalation_exhausted``
    # on broad huge-repo exploration. Rather than losing the approved result
    # to a ``user_decision_required`` soft-block, the orchestrator accepts the
    # approved (empty-diff) artifact and completes the task. Audit-only — the
    # actual ``status="complete"`` transition flows through the regular
    # ``update_task_status`` op emitted alongside; replay is a no-op forensic
    # breadcrumb. WS-2a: the payload carries ``needs_verification=True`` and
    # the ``complete`` transition's meta carries
    # ``completion_reason="accepted_approved_on_exhaustion"`` (the persisted,
    # whitelisted task-metadata marker) because the reviewer statically
    # APPROVED but the tests never ran — so downstream / reporting can
    # distinguish this from a test-verified completion. Payload shape:
    # ``{task_id: str, verdict: str, subtype: str, diff_empty: bool,
    # needs_verification: bool}``.
    "accepted_approved_on_exhaustion",
    # WS5 (ask_human dead-end → best-effort-commit): under
    # ``cfg.resolver.on_ask_human="best_effort_commit"``, when the recovery
    # ladder would resolve to ``ask_human`` the orchestrator committed whatever
    # non-empty diff existed in the task's worktree and completed the task,
    # STAMPED ``needs_human_review`` so a benchmark scorer treats it as its OWN
    # terminal category (a best-effort commit pending review — NOT normally
    # "solved"). Audit-only — the ``status="complete"`` transition flows through
    # the regular ``update_task_status`` op emitted alongside; replay is a no-op
    # forensic breadcrumb. Payload:
    # ``{task_id: str, needs_human_review: bool, failure_class: str | None,
    # diff_empty: bool}``.
    "best_effort_committed_on_ask_human",
    # WS5 (fail mode): under ``cfg.resolver.on_ask_human="fail"`` the ladder
    # resolved to ``ask_human`` and the run raised ``AskHumanDeadEndError`` to
    # exit loudly instead of silently blocking. Audit-only breadcrumb emitted
    # immediately before the raise; the run aborts (no plan mutation), so replay
    # is a no-op. Payload: ``{task_id: str, failure_class: str | None}``.
    "ask_human_fail_fast",
    # WS3 (validated-patch recovery on ANY terminal block except TESTS_FAILED):
    # a task discarded over a failure that does NOT demonstrate the fix is wrong
    # (merge-conflict exhaustion, test-diagnosis-hardfail, turn-budget/infra) —
    # despite an already-validated result (genuine, non-soft-passed reviewer
    # ``APPROVED`` + a validated diff, preferring a converged tournament winner) —
    # had that validated patch re-applied UNFORCED to live ``main`` at the
    # ``block_task`` chokepoint and was completed instead of blocked. Stamped
    # ``resolver_action="conflict_fallback_recovered"`` + ``needs_human_review``
    # (+ ``needs_verification``) so nothing downstream mistakes it for a normal
    # clean pass. Audit-only — the ``status="complete"`` transition flows through
    # the regular ``update_task_status`` op emitted alongside (via
    # ``_walk_task_to_complete``); replay is a no-op forensic breadcrumb. Payload:
    # ``{task_id: str, failure_class: str, needs_human_review: bool,
    # needs_verification: bool, resolver_action: str, diff_source: str}``.
    "recovered_validated_patch_on_conflict_exhaustion",
    # Gap 5 (containment): a developer diff was confined ENTIRELY to
    # AutoDev's own ``.autodev/`` directory (evidence / ledger / tournament
    # / index state) instead of the target repository's code — the agent
    # perceived AutoDev's internal run-mechanics as the task scope. The
    # orchestrator rejects the diff as invalid task output and routes the
    # task through the regular retry/escalate path. Audit-only — the actual
    # task-status transition (retry / escalate / block) flows through the
    # ``update_task_status`` ops emitted alongside; replay is a no-op
    # forensic breadcrumb. Payload shape:
    # ``{task_id: str, files: list[str]}`` (files capped at 20).
    "containment_violation_autodev_paths",
    # ADR-0044: framing-phase audit breadcrumbs. They never mutate plan state
    # (the architect / plan tournament record the plan); replay treats them as
    # no-ops. Payloads: framing_classified={classification, confidence,
    # signals_fired}; framing_strategy_chosen={chosen_approach_name, altitude}.
    "framing_classified",
    "framing_strategy_chosen",
    # ADR-0046: diagnosis-phase audit breadcrumbs. Like the framing ops they
    # run AHEAD of the architect/plan (before ``init_plan``) and never mutate
    # plan state — replay treats them as no-ops. Payloads:
    # diagnosis_loop_built={method, fidelity, deterministic};
    # bug_reproduced={symptom, reproduced} / repro_unavailable_live={symptom,
    # fidelity, artifact}; hypotheses_ranked={count}; cause_confirmed={cause,
    # seam}; seam_finding={seam, recurrence_at_seam, no_correct_seam}.
    "diagnosis_loop_built",
    "bug_reproduced",
    "repro_unavailable_live",
    "hypotheses_ranked",
    "cause_confirmed",
    "seam_finding",
    # ADR-0045: intake & clarification audit breadcrumbs. Like the framing /
    # diagnosis ops they run AHEAD of the architect/plan (before ``init_plan``)
    # and never mutate plan state — replay treats them as no-ops. Payloads:
    # intake_assessed={ok, missing}; intake_gathered={n_facts, sources};
    # intake_enriched={chars}; intake_questions_posed={count};
    # intake_answered / intake_defaults_assumed={count}; spec_locked={spec_hash}.
    "intake_assessed",
    "intake_gathered",
    "intake_enriched",
    "intake_questions_posed",
    "intake_answered",
    "intake_defaults_assumed",
    "spec_locked",
    # ADR-0047: Universal Blocker Resolver audit breadcrumbs. All three are
    # audit-only — they NEVER mutate plan state (the resolver's chosen action
    # applies via the regular ``update_task_status`` / ``append_corrective_tasks``
    # / ``budget_escalation`` ops emitted alongside). Resume re-reads them to
    # reconstruct the per-blocker resolution budget WITHOUT re-invoking the
    # resolver (loop-safety + determinism). Payload shapes:
    # - ``blocker_escalated``: ``{task_id, phase_id, failure_class, failing_role,
    #   raw_error_excerpt, recovery_already_tried: list[str]}``. Emitted when a
    #   terminal site routes a blocker to ``resolve_blocker``.
    # - ``resolution_chosen``: ``{task_id, failure_class, action,
    #   rationale_excerpt, params}``. Emitted once the resolver picks an action.
    # - ``resolution_outcome``: ``{task_id, action, outcome: "applied" |
    #   "fell_through" | "ask_human" | "observed", reason}``. Emitted after the
    #   site applies (or declines) the action. The ``"observed"`` outcome (Phase
    #   1A Step 1) marks an observability-only breadcrumb on the quarantine path:
    #   the site is recording that a recovery-class transition is about to
    #   happen (e.g. ``quarantine_pending_operator``), not that the resolver
    #   chose-and-applied an action.
    "blocker_escalated",
    "resolution_chosen",
    "resolution_outcome",
    # WS3-silent-degrade: KB-consult outage at the ``consult_knowledge`` rung.
    # Audit-only; payload ``{task_id, failure_class, err}``. The resolver declines
    # (no recovery) and refunds the per-blocker cycle, so a KB outage never burns
    # the bounded recovery budget. Replay no-op.
    "resolver_kb_failed",
    # Phase 1A Step 1 (RECOVERY-CONTRACT §7.1): the conflict-escalation critic's
    # merge-strategy DECISION. Audit-only — NEVER mutates plan state (the chosen
    # branch's terminal block, if any, applies via ``block_task`` →
    # ``update_task_status`` alongside). Payload shape:
    # ``{task_id, action: "rebase-and-retry" | "abandon-task" | "rewrite",
    #   conflict_files: list[str], rewrite_rounds: int}``. Records the critic's
    # CHOICE (previously invisible — zero ledger ops) so post-mortems can audit
    # how a 3-way merge conflict was resolved before any block.
    "conflict_critic_decision",
    # Step 5 (RECOVERY-CONTRACT §7 Part 4): the terminal ``block_task`` commit
    # found the PlanManager's ledger unexpectedly empty/absent ("no plan
    # initialized"). Audit-only attributable breadcrumb so the field-observed
    # ``worker_exception: "no plan initialized"`` on the conflict→corrective path
    # is never silent/misclassified. The block still re-raises (genuine state
    # corruption stays loud); this op records WHERE. Payload:
    # ``{task_id, failure_class, raw_error, err}``. Replay is a forensic no-op.
    "block_path_plan_uninitialized",
    # Gate-closer A (G6): the QA-gate dispatch detected an UNSUPPORTED language
    # — ``detect_language`` returned ``None`` while the repo carries source, OR
    # a recognised-but-NON-RUNNABLE language (e.g. ``elixir`` / ``dotnet`` /
    # ``ruby`` / ``swift`` / ``cpp``; not in ``qa.detect.RUNNABLE_TEST_LANGUAGES``).
    # Without this op the unsupported case was INVISIBLE: every per-language gate
    # vacuously soft-passed ("language not detected, skipping") and the dispatch
    # returned the all-clear. The op makes the degrade-loud decision auditable.
    # Audit-only — it NEVER mutates plan state (the task-status transition, if
    # any, flows through the regular ``update_task_status`` op emitted alongside
    # by the retry/escalate FSM that consumes the blocking detail string).
    # Payload shape: ``{language: str | None, reason: str, has_source: bool}``
    # where ``language`` is the detected non-runnable language or ``None`` for
    # "no recognised signal but source present". A genuinely-empty repo (no
    # source at all) does NOT emit this op — that stays the legit ``no_source``
    # pass. Replay is a forensic no-op, tolerated even before any ``init_plan``.
    "language_unsupported",
    # v1.0 B1: planner-side over-engineering/tech-debt advisory. Emitted by
    # ``plan_phase._advise_over_engineering`` after the plan is parsed. Audit-
    # only — NEVER mutates plan state (the plan is NOT rejected or modified
    # in response). Payload shape:
    # ``{task_id: str, smell: str, source: "planner_advisory",
    #   attempt: int, ...smell-specific fields...}``
    # where ``smell`` is ``"dependency_manifest"`` or ``"new_file_bloat"``.
    # Replay is a forensic no-op.
    "over_engineering_advisory",
    # v1.0 B2: reviewer-side over-engineering/tech-debt advisory. Emitted by
    # ``orchestrator.phase_review_runner._emit_reviewer_advisory`` after the
    # phase-review tournament completes. Audit-only — NEVER mutates plan state,
    # NEVER changes the tournament verdict, and NEVER blocks execution. The
    # recording is best-effort: a failure to append this op is swallowed so the
    # verdict path is unaffected. Payload shape:
    # ``{phase_id: str, note: str, source: "reviewer_advisory"}``.
    # Replay is a forensic no-op.
    "reviewer_over_engineering_advisory",
    # F-7 (field-finding): the plan-tournament cumulative WALL-CLOCK ceiling
    # tripped. Audit-only, LOUD fail-fast breadcrumb — fired by
    # ``orchestrator.plan_tournament_runner.run_plan_tournament`` when the
    # tournament loop raises a ``plan_phase_wall_budget_exceeded``
    # ``TournamentError`` (cumulative elapsed exceeded
    # ``cfg.guardrails.plan_phase_wall_budget_s``, checked BETWEEN passes).
    # This is the distinct, greppable attribution for the previously-opaque
    # "timed out after 2400s" external SIGKILL: the runner emits this op,
    # then re-raises so the existing plan-phase salvage path recovers the
    # best on-disk incumbent. Plan state is NOT mutated by this op (the
    # salvage / fallback flows through the regular plan-phase code paths);
    # replay treats it as a no-op, and it is tolerated even when plan is
    # None (it can fire before ``init_plan`` during the plan phase). Payload:
    # ``{spec_hash, branch_index, budget_s, elapsed_s, passes_completed,
    #   tournament_id, reason}``.
    "plan_phase_wall_budget_exceeded",
    # Task 1 (wall-budget fix, sibling of F-7): the impl-tournament
    # cumulative WALL-CLOCK ceiling tripped. Audit-only, LOUD fail-fast
    # breadcrumb — fired by
    # ``orchestrator.impl_tournament_runner.run_impl_tournament`` when the
    # tournament loop raises an ``impl_phase_wall_budget_exceeded``
    # ``TournamentError`` (cumulative elapsed exceeded
    # ``cfg.guardrails.impl_phase_wall_budget_s``, checked BETWEEN passes).
    # This is the distinct, greppable attribution for what would otherwise
    # be an opaque external SIGKILL: the runner emits this op, then
    # re-raises so the caller's existing recovery path (retry / escalate /
    # fall back to the pre-tournament bundle) takes over. Plan state is NOT
    # mutated by this op; replay treats it as a no-op, and it is tolerated
    # even when plan is None. Payload: ``{tournament_id, task_id, budget_s,
    # reason}``.
    "impl_phase_wall_budget_exceeded",
    # Task 2 (wall-budget fix, DAG-wide sibling of the two above): the
    # WHOLE-execute-phase cumulative WALL-CLOCK ceiling tripped. Audit-only,
    # LOUD fail-fast breadcrumb — fired by
    # ``orchestrator.execute_phase.run_execute_phase`` when a
    # ``ExecutePhaseWallBudgetExceededError`` propagates out of the DAG /
    # retry loops (cumulative elapsed exceeded
    # ``cfg.guardrails.execute_phase_wall_budget_s``, checked BETWEEN
    # tasks/retries/rounds). This is the distinct, greppable attribution for
    # the previously-opaque external SIGKILL that killed the SWE-bench pilot
    # at 1800s: the runner emits this op, then re-raises. Plan state is NOT
    # mutated by this op — the task in flight at breach time is left as-is
    # and the orphan-reap sweep reverts it to pending on the next resume;
    # replay treats it as a no-op. Payload: ``{budget_s, elapsed_s,
    # tasks_processed, task_ids_processed, reason}``.
    "execute_phase_wall_budget_exceeded",
    # F-4 (field-finding): apply-time edit-scope WARN breadcrumb. Emitted by
    # ``orchestrator.execute_phase._apply_with_conflict_escalation`` when the
    # ``enforce_apply_time_edit_scope`` policy is ``"warn"`` and the
    # developer's worktree diff touches a file outside the resolved
    # effective scope. Audit-only — purely advisory: the diff is STILL
    # applied (warn mode never blocks), so this op NEVER mutates plan state.
    # Best-effort: a failure to append it is swallowed so apply is never
    # derailed. Replay is a forensic no-op, tolerated even when plan is None.
    # Payload shape:
    # ``{task_id: str, out_of_scope_files: list[str], effective_scope:
    #   list[str], policy: "warn"}``.
    "edit_scope_apply_violation",
]


class LedgerEntry(BaseModel):
    """One append-only ledger record.

    The ``self_hash`` field is computed over all other fields (with
    ``prev_hash`` included). See :func:`compute_hash`.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int  # monotonically increasing, starts at 1
    timestamp: str  # ISO 8601, UTC
    session_id: str
    op: LedgerOp
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str  # self_hash of entry[n-1]; "" for genesis
    self_hash: str  # hash of this entry excluding self_hash itself


def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def compute_hash(entry_dict_without_hash: dict[str, Any]) -> str:
    """Return a 16-char SHA-256 prefix of the canonical JSON form."""
    canon = json.dumps(entry_dict_without_hash, sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _entry_without_self_hash(entry: LedgerEntry) -> dict[str, Any]:
    d = entry.model_dump(mode="json")
    d.pop("self_hash", None)
    return d


async def append_entry(
    cwd: Path,
    op: LedgerOp,
    payload: dict[str, Any],
    session_id: str,
) -> LedgerEntry:
    """Append one entry to the ledger. Caller must hold :func:`plan_lock`.

    Computes ``seq = prev.seq + 1`` and ``prev_hash = prev.self_hash`` by
    reading the last line of the file.
    """
    lp = ledger_path(cwd)
    lp.parent.mkdir(parents=True, exist_ok=True)

    prev_seq, prev_hash = _read_last_entry_head(lp)

    entry_body: dict[str, Any] = {
        "seq": prev_seq + 1,
        "timestamp": _iso_now(),
        "session_id": session_id,
        "op": op,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    self_hash = compute_hash(entry_body)
    entry_body["self_hash"] = self_hash

    entry = LedgerEntry.model_validate(entry_body)

    line = json.dumps(entry.model_dump(mode="json"), sort_keys=True) + "\n"
    _atomic_append(lp, line)
    logger.info("ledger.append", op=op, seq=entry.seq, session_id=session_id)
    return entry


def _read_last_entry_head(path: Path) -> tuple[int, str]:
    """Return ``(last_seq, last_self_hash)`` for the existing ledger.

    Returns ``(0, "")`` if the file is missing or empty. Raises
    :class:`LedgerCorruptError` if the last non-empty line is malformed —
    we refuse to append after a corrupt tail because a correct ``prev_hash``
    cannot be computed.
    """
    if not path.exists():
        return (0, "")
    last_line: str | None = None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                last_line = stripped
    if last_line is None:
        return (0, "")
    try:
        last = json.loads(last_line)
    except json.JSONDecodeError as exc:
        raise LedgerCorruptError(
            f"last ledger line is not valid JSON: {exc}. "
            "Manual recovery required — inspect the trailing line of "
            f"{path} and either remove it or restore from a snapshot."
        ) from exc
    seq = last.get("seq")
    self_hash = last.get("self_hash")
    if not isinstance(seq, int) or not isinstance(self_hash, str):
        raise LedgerCorruptError(
            f"last ledger line is missing seq/self_hash fields: {path}"
        )
    return (seq, self_hash)


def _atomic_append(path: Path, line: str) -> None:
    """Append ``line`` durably.

    Strategy: clone-existing-contents-to-tmp (reflink when available, else
    copy), append, then ``os.replace``. This preserves the same crash-safety
    guarantees while avoiding a full read on filesystems that support reflinks.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # Tempfile in same dir to keep replace atomic across filesystems.
    fd, tmp_path = tempfile.mkstemp(prefix=".ledger.", suffix=".tmp", dir=str(parent))
    try:
        os.close(fd)
        tmp = Path(tmp_path)

        if path.exists():
            if not _clone_file(path, tmp):
                tmp.write_bytes(path.read_bytes())

        with tmp.open("ab") as fh:
            fh.write(line.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Best-effort cleanup on failure; the live file is untouched because
        # we only write to tmp_path until os.replace.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _clone_file(src: Path, dst: Path) -> bool:
    """Best-effort reflink clone ``src`` -> ``dst``.

    Returns ``True`` when the filesystem/runtime supports an O(1)-style clone
    (APFS clonefile on macOS or FICLONE ioctl on Linux); returns ``False`` for
    unsupported filesystems/platforms so callers can fall back to byte-copy.
    """
    clonefile = getattr(os, "clonefile", None)
    if clonefile is not None:
        try:
            os.unlink(dst)
        except OSError:
            pass
        try:
            clonefile(str(src), str(dst))
            return True
        except OSError:
            pass

    try:
        import fcntl
    except ImportError:
        return False

    # Linux FICLONE ioctl: clone src inode into dst (copy-on-write where supported).
    ficlone = 0x40049409
    try:
        with src.open("rb") as src_fh, dst.open("wb") as dst_fh:
            fcntl.ioctl(dst_fh.fileno(), ficlone, src_fh.fileno())
        return True
    except OSError:
        return False


def stream_entries(cwd: Path) -> "Iterator[LedgerEntry]":
    """v0.24.0 D1: streaming line-by-line ledger reader.

    Yields :class:`LedgerEntry` objects without materializing the full
    list, while still validating each entry's schema and the
    incremental hash chain in-flight. Use this for forensic walks on
    multi-MB ledgers (Unity's 2026-05-09 ledger was 2.97 MB / 140
    entries; future production runs may push this much higher).

    For small ledgers and most code paths, :func:`read_entries` is the
    convenient buffered alternative. Both functions share the chain
    invariants — corruption raises :class:`LedgerCorruptError` from
    either entry point.
    """
    lp = ledger_path(cwd)
    if not lp.exists():
        return
    prev_hash = ""
    prev_seq = 0
    with lp.open("r", encoding="utf-8") as fh:
        for idx, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerCorruptError(
                    f"ledger line {idx} is not valid JSON: {exc}. "
                    "Inspect and repair (or restore from snapshot)."
                ) from exc
            try:
                entry = LedgerEntry.model_validate(obj)
            except Exception as exc:
                raise LedgerCorruptError(
                    f"ledger line {idx} failed schema validation: {exc}"
                ) from exc

            if entry.seq != prev_seq + 1:
                raise LedgerCorruptError(
                    f"ledger line {idx}: seq jumped from {prev_seq} to {entry.seq}"
                )
            if entry.prev_hash != prev_hash:
                raise LedgerCorruptError(
                    f"ledger line {idx}: prev_hash mismatch "
                    f"(expected {prev_hash!r}, saw {entry.prev_hash!r})"
                )
            body = _entry_without_self_hash(entry)
            want = compute_hash(body)
            if want != entry.self_hash:
                raise LedgerCorruptError(
                    f"ledger line {idx}: self_hash mismatch "
                    f"(computed {want}, stored {entry.self_hash})"
                )

            yield entry
            prev_hash = entry.self_hash
            prev_seq = entry.seq


def read_entries(cwd: Path) -> list[LedgerEntry]:
    """Read + validate every ledger entry.

    v0.24.0 D1: thin buffered wrapper around :func:`stream_entries`.

    Raises :class:`LedgerCorruptError` on:
      - malformed JSON on any non-empty line
      - validation failure for any entry
      - broken prev_hash / self_hash chain
      - non-monotonic ``seq``
    """
    return list(stream_entries(cwd))


def replay_ledger(cwd: Path) -> tuple[Plan | None, list[LedgerEntry]]:
    """Reconstruct the current :class:`Plan` from the ledger.

    Applies ops in order:
      - ``init_plan`` sets the initial Plan (payload contains serialized Plan).
      - ``snapshot`` replaces the in-memory Plan wholesale.
      - ``update_plan`` replaces with the new Plan payload.
      - ``update_task_status`` mutates one task's status (+ blocked_reason /
        retry_count / escalated if present).
      - ``mark_blocked`` / ``mark_complete`` mutate status.
      - ``append_evidence`` records the evidence path on the task but does
        not change status.

    Returns ``(None, [])`` if the ledger is empty.
    """
    entries = read_entries(cwd)
    if not entries:
        return None, entries

    plan: Plan | None = None
    for entry in entries:
        plan = _apply_op(plan, entry)
    return plan, entries


def _apply_op(plan: Plan | None, entry: LedgerEntry) -> Plan | None:
    """Apply a single ledger op to ``plan`` and return the updated plan."""
    op = entry.op
    payload = entry.payload

    if op in ("init_plan", "update_plan", "snapshot"):
        plan_payload = payload.get("plan")
        if plan_payload is None:
            raise LedgerCorruptError(
                f"entry seq={entry.seq} op={op} is missing payload.plan"
            )
        return Plan.model_validate(plan_payload)

    if op == "plan_tournament_complete":
        # Audit-only breadcrumb. Appended during the plan phase BEFORE the
        # plan is persisted via ``init_plan``, so it may legitimately appear
        # before any plan-containing op during replay. Do NOT mutate plan.
        return plan

    if op == "impl_tournament_complete":
        # Audit-only breadcrumb. Appended during the execute phase after the
        # impl tournament completes. Does NOT mutate plan state.
        return plan

    if op == "phase_review_complete":
        # v0.9.0: audit-only breadcrumb appended after the phase-review
        # tournament completes. Plan mutations (review_status, corrective
        # tasks) are recorded by ``update_phase_meta`` and
        # ``append_corrective_tasks`` separately, so this op never touches
        # plan state.
        return plan

    if op in ("framing_classified", "framing_strategy_chosen"):
        # ADR-0044: audit-only breadcrumbs appended during the framing phase,
        # BEFORE the plan is persisted via ``init_plan`` (framing runs ahead of
        # the architect). Like ``plan_tournament_complete`` they may legitimately
        # precede any plan-containing op on replay, so they MUST return early
        # here — before the ``plan is None`` guard below — and never mutate plan.
        return plan

    if op in (
        "diagnosis_loop_built",
        "bug_reproduced",
        "repro_unavailable_live",
        "hypotheses_ranked",
        "cause_confirmed",
        "seam_finding",
    ):
        # ADR-0046: audit-only breadcrumbs appended during the diagnosis phase,
        # which (like framing) runs BEFORE the plan is persisted via ``init_plan``.
        # They never mutate plan state and may legitimately precede any
        # plan-containing op on replay, so they MUST return early here — before
        # the ``plan is None`` guard below.
        return plan

    if op in (
        "intake_assessed",
        "intake_gathered",
        "intake_enriched",
        "intake_questions_posed",
        "intake_answered",
        "intake_defaults_assumed",
        "spec_locked",
    ):
        # ADR-0045: audit-only breadcrumbs appended during the intake phase, which
        # runs at the very FRONT of the plan pipeline (before framing, before the
        # plan is persisted via ``init_plan``). They never mutate plan state and
        # may legitimately precede any plan-containing op on replay, so they MUST
        # return early here — before the ``plan is None`` guard below.
        return plan

    if op in (
        "blocker_escalated",
        "resolution_chosen",
        "resolution_outcome",
        "conflict_critic_decision",
        "resolver_kb_failed",
    ):
        # ADR-0047: audit-only breadcrumbs for the Universal Blocker Resolver.
        # WS3-silent-degrade: ``resolver_kb_failed`` records a KB-consult outage
        # at the ``consult_knowledge`` rung (KB starvation) — audit-only, never
        # mutates plan state (the resolver simply declines and the per-blocker
        # cycle is refunded by ``_maybe_resolve_blocker``).
        # Phase 1A Step 1 adds ``conflict_critic_decision`` (the conflict
        # critic's merge-strategy choice) to the same audit-only set — it never
        # mutates plan state; the chosen branch's terminal block (if any)
        # applies via the regular ``update_task_status`` op emitted alongside.
        # The resolver fires during execute_phase (plan already persisted), but
        # these ops never mutate plan state — the chosen action applies via the
        # regular ``update_task_status`` / ``append_corrective_tasks`` /
        # ``budget_escalation`` ops emitted alongside. Resume re-reads them to
        # rebuild the per-blocker resolution budget without re-invoking the
        # resolver. Returned early here (before the ``plan is None`` guard) so
        # replay is order-independent, mirroring the framing/intake/diagnosis ops.
        return plan

    if op in ("mark_in_flight", "clear_in_flight"):
        # v0.11.0: audit-only breadcrumbs for the parallel dispatcher.
        # In-flight is in-memory on the PlanManager; resumes do NOT
        # restore it (by design — a crash mid-flight rolls back the
        # underlying task and resume picks it up fresh).
        return plan

    if op in (
        "multi_branch_plan_tournament_start",
        "multi_branch_meta_merge_complete",
        "multi_branch_plan_tournament_complete",
    ):
        # v0.12.0: audit-only breadcrumbs for the multi-branch dispatcher.
        # Plan state is unaffected — branches each carry their own
        # ``plan_tournament_complete`` ops, and the meta-merged final
        # markdown is consumed downstream into ``init_plan``. These ops
        # are forensics for "how did we get the merged plan", not state.
        return plan

    if op == "drift_verifier_complete":
        # v0.16.0: audit-only breadcrumb appended after the drift
        # verifier completes. Plan mutations (accept_phase override,
        # corrective tasks) flow through the regular phase-review ops
        # so this op never touches plan state.
        return plan

    if op in (
        "stuck_refine",
        "stuck_pivot",
        "soft_blocker_handoff",
        "course_correction_emitted",
        # v0.26.1: architect-consult rung audit op (registered in the
        # Literal but missing a handler — added here to keep
        # ``replay_ledger`` from crashing if anyone serializes a real
        # session ledger and re-applies it).
        "architect_consult",
        # v0.26.2 Phase 3: persistent-failure drop audit op. Mutation
        # of plan.edit_scope / task.files lives in the ``init_plan``
        # entry emitted later in the same plan phase; this op is the
        # forensic breadcrumb for "what got dropped".
        "scope_entry_dropped",
    ):
        # v0.15.0: audit-only breadcrumbs for the stuck-recovery
        # escalation ladder + PRM. The task.status / blocked_reason
        # changes are recorded by the regular ``update_task_status``
        # op emitted alongside; these ops are forensics for "what
        # path did the ladder/PRM take" so a human can reconstruct
        # the decision after the fact.
        return plan

    if op == "hypothesis_repeat_detected":
        # v0.17.0 S4: advisory tag. Multi-branch dispatcher logged a
        # repeat-hypothesis match against prior discards. Plan state is
        # unaffected — the branch still runs; the tag is forensics only.
        return plan

    if op == "web_search_invoked":
        # v0.17.0 S2: audit-only forensics for the WEB_SEARCH ladder rung.
        # Plan state is unaffected; the WEB_CONTEXT splice into the next
        # critic prompt is the executor's responsibility.
        return plan

    if op in (
        "multi_branch_phase_review_start",
        "multi_branch_phase_review_meta_merge_complete",
        "multi_branch_phase_review_complete",
    ):
        # v0.18.0 A2: multi-branch phase-review fan-out audit ops.
        # Plan state mutations (review_status, corrective tasks) flow
        # through the regular ``update_phase_meta`` /
        # ``append_corrective_tasks`` ops; these three are forensics.
        return plan

    if op in ("plateau_detected", "plateau_forced_lane_change"):
        # v0.18.0 B2: plateau-detector telemetry. Audit-only.
        return plan

    if op in (
        "multi_branch_impl_start",
        "multi_branch_impl_meta_merge_complete",
        "multi_branch_impl_complete",
    ):
        # v0.21.0 A2: multi-branch impl-tournament audit ops. The
        # winning diff is applied to main via the regular impl-tournament
        # path; these ops are forensics for the fan-out + meta-merge.
        return plan

    if op in (
        "speculative_started",
        "speculative_rolled_back",
        "speculative_committed",
    ):
        # v0.21.0 B2: speculative-execution audit ops. The actual task
        # status transitions live in regular ``update_task_status``
        # entries emitted alongside.
        return plan

    if op in (
        "attempt_started",
        "reconcile_evidence",
        "reap_orphans",
        "regex_timeout",
    ):
        # v0.22.2 B1+B3 / v0.23.0 C6: audit-only ops. All actual state
        # mutations flow through ``update_task_status`` ops emitted
        # alongside (or the per-task ``revert_task_to_pending`` path for
        # reap_orphans). Replay treats these as no-ops — they are
        # forensics for "what did the resume / watchdog do".
        return plan

    if op in (
        "task_files_entry_dropped",
        "task_files_new_entry_dropped",
        "task_extended_scope_entry_dropped",
        "phase_edit_scope_entry_dropped",
        "task_auto_skipped",
        "architect_persistent_parse_error",
        "architect_persistent_pyd_error",
        # v0.27 Phase 5: post-tournament structural-validity gate
        # rejection. Audit-only — the plan-state falls back to the
        # pre-tournament snapshot.
        "tournament_output_rejected_structurally",
        # v0.27 Phase 3: granular per-task edit_scope violation block.
        # Audit-only — the task transitions to ``blocked`` via the
        # regular ``update_task_status`` op emitted alongside.
        "task_blocked_scope_violation",
        # v0.27 Phase 7: role agent emitted ESCALATE: line. Audit-only.
        "agent_escalated",
        # v0.28.0 Bug 8: ``autodev requeue`` CLI breadcrumb. The actual
        # task transitions live in ``update_task_status`` ops emitted
        # alongside (one per requeued task); replay treats the
        # breadcrumb itself as a no-op.
        "requeue",
        # v0.29.0 Bug 9: ``autodev rewind`` CLI breadcrumb. The actual
        # multi-phase task / review-status transitions live in the
        # ``update_task_status`` and ``update_phase_meta`` ops emitted
        # alongside (one per affected task / phase); replay treats the
        # breadcrumb itself as a no-op.
        "rewind",
        # v0.30.0 Bug 4: per-adapter-failure audit breadcrumb. The
        # actual task-status transitions (retry / block) flow through
        # the regular ``update_task_status`` ops emitted alongside
        # by the orchestrator's retry/escalation FSM; this op is a
        # forensic counter for "how many transient adapter failures
        # preceded the eventual outcome".
        "adapter_failure",
        # v0.31.0 (Phase 3): per-(task_id, role) budget escalation
        # breadcrumb. The actual budget bump lives only in the caller's
        # local ``AgentInvocation`` state at dispatch time — the
        # invocation is not persisted and the escalation tracker is
        # in-memory only. Replay treats this op as a no-op forensic
        # counter for "how many times we bumped the per-(task, role)
        # budget before the underlying agent succeeded or hard-failed".
        "budget_escalation",
        # v0.32.0 Phase 1.2: plan-phase architect budget escalation.
        # Audit-only; replay no-op. Same shape rationale as
        # ``budget_escalation`` but scoped to the plan-phase
        # architect retry loop.
        "plan_phase_budget_escalation",
        # RECOVERY-CONTRACT §7 Step 2 (gate R4): resume-safe budget COUNTER.
        # Audit-only; replay no-op. The counter lives in the in-memory
        # ``BudgetEscalationTracker`` (rehydrated from these ops on construction
        # via last-value-wins) — replay must NOT mutate plan state here.
        "budget_cycle",
        # v0.32.0 Phase 2: review-tournament lifecycle ops. All
        # audit-only — they do NOT mutate plan state. The underlying
        # task status changes (retry / escalate / soft-block) flow
        # through the regular ``update_task_status`` ops emitted by
        # the orchestrator's retry/escalation FSM. Mirrors the
        # ``impl_tournament_complete`` op shape.
        "review_tournament_started",
        "review_tournament_judged",
        "review_tournament_converged",
        "review_tournament_escalated",
        # v0.32.0 Phase 4.5: knowledge-aware retry-loop telemetry. All
        # three are audit-only — the underlying ``update_task_status``
        # ops emitted by the retry/escalation FSM carry the actual
        # plan-state mutations. Replay treats these as no-ops.
        "repetition_loop_detected",
        "recovery_action_chosen",
        "tactic_switch",
        # v0.33.0 A1: plan-global ``[new]`` admission breadcrumb.
        # Audit-only; the plan is unchanged because the path was
        # admitted, not dropped.
        "path_validation_resolved_via_plan_global",
        # v0.34.0 B2: sparse-checkout header expansion breadcrumb.
        # Audit-only; the actual sparse-pattern update lives in git's
        # own sparse-checkout state, not in the plan.
        "sparse_worktree_expanded",
        # v0.34.0 B3: drift-verifier convergence-failure breadcrumb.
        # Audit-only; the escalation that follows flows through the
        # regular phase-review corrective-direction path.
        "drift_convergence_failure",
        # v0.35.0 Tier C: knowledge-store hygiene ops. All audit-only —
        # the underlying state (quarantined flag, succeeded_after_count
        # bump, rejection-skip) lives in the per-project knowledge
        # JSONL and not in plan state, so replay treats them as no-ops.
        "knowledge_entry_quarantined",
        "knowledge_lesson_credited",
        "critic_evidence_rejected",
        "knowledge_entry_promotion_rejected",
        # v0.36.0 Tiers D/E/F/G: retry, budget, forensics, spec-hygiene
        # telemetry. All audit-only — the actual state mutations
        # (escalations, model swaps, dispatched budgets, CLI exit codes)
        # live elsewhere; the ledger ops are forensic breadcrumbs that
        # let ``autodev status --blocked`` and offline post-mortems
        # reconstruct the architect-recovery decision tree.
        "architect_attempt_failed",
        "recovery_tier_attempted",
        "path_rejection_recorded",
        "architect_model_changed_for_retry",
        "huge_repo_multiplier_applied",
        "retry_budget_scaled",
        "network_probe_failed",
        "spec_validation_failed",
        # v0.37.0 H2: per-phase corrective-task cap was reached. Audit-
        # only — the plan-state mutations (``review_status="capped"`` on
        # the phase-review path, ``status="blocked"`` + ``recovery_hint``
        # on the architect-refine path) flow through the regular
        # ``update_phase_meta`` / ``update_task_status`` ops emitted
        # alongside. Replay treats this op as a no-op.
        "corrective_cap_reached",
        # v0.38.0 HK10: per-boot adapter-selection breadcrumb. Audit-only.
        # The actual adapter resolution lives in
        # :func:`adapters.detect.get_adapter` at the next session boot;
        # this op is observability for "which selection arm fired".
        "adapter_selected",
        # v0.38.0 I3 (HK5): stuck skip_corrective_round loop detected.
        # Audit-only — counter state is persisted via the regular
        # ``update_phase_meta`` op emitted alongside (the
        # ``Phase.metadata["skip_corrective_count"]`` delta).
        "skip_corrective_loop_detected",
        # F-2 (field-finding): phase-scoped non-convergence ceiling tripped.
        # Audit-only no-op on replay — the per-phase counter is persisted via the
        # regular ``update_phase_meta`` op and the terminal block via
        # ``update_task_status``, both emitted alongside.
        "corrective_nonconvergent_ceiling",
        # v0.38.0 I2 (HK4): ``autodev requeue --capped-phases`` audit
        # breadcrumb. Replay is a no-op — the per-task transitions live
        # in the ``update_task_status`` ops emitted alongside (one per
        # requeued task) and the phase ``review_status`` reset flows
        # through the regular ``update_phase_meta`` op.
        "requeue.capped_phases_selected",
        # v0.39.0 (Cluster A2b): runtime auto-soft-pass fallback fired.
        # Audit-only — the ``treat_unrunnable_tests_as_no_tests`` flip is
        # in-memory on the session cfg only and never persisted, so replay
        # is a no-op forensic breadcrumb.
        "auto_soft_pass_enabled",
        # v0.39.0 (Cluster C2): under-decomposed huge-repo task breadcrumb.
        # Audit-only — the orchestrator never mutates/rejects the plan in
        # response (the planner advisory and runtime telemetry are purely
        # observational), so replay is a no-op forensic breadcrumb.
        "task_under_decomposed",
        # Phase 0 (cost/time telemetry): per-invocation cost breadcrumb.
        # Audit-only — purely observational (the per-run cost summary is
        # computed by summing these ops over the run window); replay is a
        # forensic no-op.
        "invocation_cost",
        # Tier J (huge-repo): accept-approved-on-exhaustion breadcrumb.
        # Audit-only — the ``status="complete"`` transition flows through the
        # regular ``update_task_status`` op emitted alongside; replay is a
        # no-op forensic breadcrumb.
        "accepted_approved_on_exhaustion",
        # WS5 (ask_human dead-end): best-effort-commit completion + fail-fast
        # breadcrumbs. Both audit-only — the ``status="complete"`` transition
        # (best_effort) flows through the regular ``update_task_status`` op
        # emitted alongside, and the fail-fast op precedes a run-aborting raise
        # (no plan mutation). Replay is a no-op forensic breadcrumb.
        "best_effort_committed_on_ask_human",
        "ask_human_fail_fast",
        # WS3 (validated-patch recovery on ANY terminal block except
        # TESTS_FAILED): validated patch re-applied to live ``main`` + task
        # completed at ``block_task`` instead of blocked. Audit-only — the
        # ``status="complete"`` transition flows through the regular
        # ``update_task_status`` op emitted alongside; replay is a no-op forensic
        # breadcrumb.
        "recovered_validated_patch_on_conflict_exhaustion",
        # Gap 5 (containment): developer diff confined to AutoDev's own
        # ``.autodev/`` directory was rejected as invalid task output.
        # Audit-only — the task-status transition (retry / escalate / block)
        # flows through the regular ``update_task_status`` ops emitted
        # alongside by the retry/escalation FSM; replay is a no-op
        # forensic breadcrumb.
        "containment_violation_autodev_paths",
        # Step 5 (RECOVERY-CONTRACT §7 Part 4): terminal-block ledger-absence
        # breadcrumb. Audit-only — the block re-raises so a genuine corruption
        # still surfaces; this op only records WHERE it happened. Replay is a
        # forensic no-op (and is tolerated even when plan is None, since this op
        # is precisely the empty-ledger case).
        "block_path_plan_uninitialized",
        # Gate-closer A (G6): unsupported-language QA-gate-dispatch breadcrumb.
        # Audit-only — never mutates plan state (the blocking detail it pairs
        # with flows through the regular retry/escalate path's
        # ``update_task_status`` op). Tolerated even when plan is None so the
        # op is order-independent on replay, mirroring the other dispatch-time
        # audit ops above.
        "language_unsupported",
        # v1.0 B1: planner-side over-engineering advisory. Audit-only — fired
        # by ``plan_phase._advise_over_engineering`` after the plan is parsed.
        # NEVER mutates plan state; replay is a forensic no-op. Tolerated even
        # when plan is None so the op is order-independent on replay (it fires
        # right after plan parsing, before the ``init_plan`` op in some code
        # paths). Payload: ``{task_id, smell, source, attempt, ...}``.
        "over_engineering_advisory",
        # v1.0 B2: reviewer-side over-engineering advisory. Audit-only — fired
        # by ``phase_review_runner._emit_reviewer_advisory`` after the phase-
        # review tournament completes. NEVER mutates plan state or changes the
        # verdict; replay is a forensic no-op. Payload:
        # ``{phase_id, note, source}``.
        "reviewer_over_engineering_advisory",
        # F-7 (field-finding): plan-tournament cumulative wall-clock ceiling
        # tripped. Audit-only no-op on replay — the runner re-raises a
        # ``TournamentError`` after emitting this op and the salvage / fallback
        # flows through the regular plan-phase code paths (no plan mutation
        # here). Tolerated even when plan is None: the op fires during the plan
        # phase and may precede ``init_plan``, mirroring the other plan-phase
        # dispatch-time audit ops above.
        "plan_phase_wall_budget_exceeded",
        # Task 1 (wall-budget fix, sibling of F-7): impl-tournament
        # cumulative wall-clock ceiling tripped. Audit-only no-op on replay —
        # the runner re-raises a ``TournamentError`` after emitting this op;
        # plan state is not mutated here. Tolerated even when plan is None,
        # mirroring ``plan_phase_wall_budget_exceeded`` above.
        "impl_phase_wall_budget_exceeded",
        # Task 2 (wall-budget fix, DAG-wide sibling): whole-execute-phase
        # cumulative wall-clock ceiling tripped. Audit-only no-op on replay —
        # ``run_execute_phase`` re-raises ``ExecutePhaseWallBudgetExceededError``
        # after emitting this op; plan state is NOT mutated here (the
        # in-flight task is left as-is for the orphan-reap sweep to revert on
        # the next resume). Tolerated even when plan is None, mirroring the
        # wall-budget ops above.
        "execute_phase_wall_budget_exceeded",
        # F-4 (field-finding): apply-time edit-scope WARN breadcrumb. Audit-
        # only — warn mode applies the diff regardless, so this op NEVER
        # mutates plan state; replay is a forensic no-op. Tolerated even when
        # plan is None (mirrors the other execute-time advisory ops), though
        # in practice it fires during execute with the plan already
        # persisted. Payload: ``{task_id, out_of_scope_files,
        # effective_scope, policy}``.
        "edit_scope_apply_violation",
    ):
        # v0.27 Phase 4-5: granular drop / persistent-error telemetry +
        # post-tournament structural-validity rejection.
        # All audit-only — the plan-state mutation flows through the
        # ``init_plan`` / ``snapshot`` op emitted in the same
        # plan-phase. ``task_auto_skipped`` is paired with an
        # ``update_task_status`` op that transitions the task to
        # ``skipped``; the telemetry op records *why*.
        return plan

    if plan is None:
        raise LedgerCorruptError(
            f"entry seq={entry.seq} op={op} applied before any init_plan"
        )

    if op == "update_task_status":
        task_id = payload.get("task_id")
        status = payload.get("status")
        if not isinstance(task_id, str) or not isinstance(status, str):
            raise LedgerCorruptError(
                f"entry seq={entry.seq} update_task_status missing task_id/status"
            )
        task = _find_task(plan, task_id)
        if task is None:
            # Plan-structure drift from the ledger — surface as corruption.
            raise LedgerCorruptError(
                f"entry seq={entry.seq} references unknown task_id={task_id}"
            )
        task.status = status  # type: ignore[assignment]
        if "blocked_reason" in payload:
            task.blocked_reason = payload["blocked_reason"]
        if "retry_count" in payload:
            task.retry_count = int(payload["retry_count"])
        if "escalated" in payload:
            task.escalated = bool(payload["escalated"])
        if "evidence_bundle" in payload:
            task.evidence_bundle = payload["evidence_bundle"]
        # v0.29.0 Bug 6: typed block class. Pre-v0.29.0 ledger entries
        # omit the field; the migration shim in
        # :func:`PlanManager._load_sync` backfills from blocked_reason.
        if "block_reason_class" in payload:
            cls = payload["block_reason_class"]
            if cls in (None, "verdict", "infrastructure", "cap"):
                task.block_reason_class = cls
        # v0.32.0 (Phase 5, Gap G): replay the structured recovery hint
        # so a resumed PlanManager surfaces the same actionable text
        # the original block site populated. Pre-v0.32.0 entries omit
        # the field; the load path simply leaves ``task.recovery_hint``
        # at its ``None`` default.
        if "recovery_hint" in payload:
            from state.schemas import RecoveryHint as _RecoveryHint

            raw_hint = payload["recovery_hint"]
            if raw_hint is None:
                task.recovery_hint = None
            else:
                try:
                    task.recovery_hint = _RecoveryHint.model_validate(raw_hint)
                except Exception:  # noqa: BLE001 - tolerate legacy payloads
                    task.recovery_hint = None
        # Step 5 (RECOVERY-CONTRACT §7 Part 3): replay the resolver guidance
        # onto ``Task.metadata`` so a full ledger-replay (no-snapshot) path
        # restores the same note the snapshot fast-path carries. Mirrors the
        # write side in ``PlanManager.update_task_status``. Merge so other
        # metadata keys survive; pre-Step-5 entries simply omit these keys.
        for _mkey in ("resolver_note", "resolver_action"):
            if _mkey not in payload:
                continue
            _new_md = dict(task.metadata or {})
            if payload[_mkey] is None:
                _new_md.pop(_mkey, None)
            else:
                _new_md[_mkey] = str(payload[_mkey])
            task.metadata = _new_md
        return plan

    if op == "mark_blocked":
        task_id = payload.get("task_id")
        if not isinstance(task_id, str):
            raise LedgerCorruptError(
                f"entry seq={entry.seq} mark_blocked missing task_id"
            )
        task = _find_task(plan, task_id)
        if task is not None:
            task.status = "blocked"
            task.blocked_reason = payload.get("reason")
        return plan

    if op == "mark_complete":
        task_id = payload.get("task_id")
        if not isinstance(task_id, str):
            raise LedgerCorruptError(
                f"entry seq={entry.seq} mark_complete missing task_id"
            )
        task = _find_task(plan, task_id)
        if task is not None:
            task.status = "complete"
        return plan

    if op == "append_evidence":
        task_id = payload.get("task_id")
        path = payload.get("path")
        if isinstance(task_id, str) and isinstance(path, str):
            task = _find_task(plan, task_id)
            if task is not None:
                task.evidence_bundle = path
        return plan

    if op == "append_corrective_tasks":
        # v0.9.0: append corrective sub-tasks injected after a B/AB phase
        # review winner. Idempotent: re-applying skips tasks already
        # present (matched by id), and re-merges ``corrective_task_ids``
        # without duplication. Mirrors the locked semantics of the
        # PlanManager method that emitted the op.
        from state.schemas import Task as _Task  # local: avoid cycle at import

        phase_id = payload.get("phase_id")
        raw_tasks = payload.get("tasks") or []
        if not isinstance(phase_id, str) or not isinstance(raw_tasks, list):
            raise LedgerCorruptError(
                f"entry seq={entry.seq} append_corrective_tasks malformed"
            )
        phase = _find_phase(plan, phase_id)
        if phase is None:
            raise LedgerCorruptError(
                f"entry seq={entry.seq} references unknown phase_id={phase_id}"
            )
        existing_task_ids = {t.id for t in phase.tasks}
        for raw in raw_tasks:
            try:
                t = _Task.model_validate(raw)
            except Exception as exc:
                raise LedgerCorruptError(
                    f"entry seq={entry.seq} corrective task invalid: {exc}"
                ) from exc
            if t.id not in existing_task_ids:
                phase.tasks.append(t)
                existing_task_ids.add(t.id)
            if t.id not in phase.corrective_task_ids:
                phase.corrective_task_ids.append(t.id)
        # Status transition payload (caller writes the explicit status
        # alongside the task list so replay reproduces exactly).
        new_status = payload.get("review_status")
        if isinstance(new_status, str):
            phase.review_status = new_status  # type: ignore[assignment]
        # WS4 D1: mirror the live ``append_corrective_tasks`` path, which
        # re-runs implicit dependency inference on the FULL phase after the
        # append. The op payload only carries the newly-appended tasks, so an
        # edge the live path inferred onto a PRE-EXISTING task is otherwise
        # reproduced ONLY via the snapshot net — pure-op replay (this function)
        # would yield ``depends_on=[]`` for it. ``infer_dependencies`` is
        # idempotent (only touches empty ``depends_on``, edges point strictly
        # backward in declaration order), so re-running it here is safe and
        # deterministic. Lazy import: a top-level ``orchestrator`` import would
        # cycle — ``orchestrator`` imports ``state.plan_manager`` →
        # ``state.ledger`` at package-init time.
        from orchestrator.dependency_inference import infer_dependencies

        infer_dependencies(phase)
        return plan

    if op == "update_phase_meta":
        # v0.9.0: update arbitrary phase-level metadata fields. Currently
        # carries ``baseline_commit``, ``review_status``, and
        # ``end_checkpoint_commit`` (v0.21.0 B1). Idempotent: re-applying
        # overwrites with the same values.
        # v0.38.0 I3: ``metadata`` is shallow-merged into
        # ``Phase.metadata`` (substrate for HK5's skip_corrective_count).
        phase_id = payload.get("phase_id")
        if not isinstance(phase_id, str):
            raise LedgerCorruptError(
                f"entry seq={entry.seq} update_phase_meta missing phase_id"
            )
        phase = _find_phase(plan, phase_id)
        if phase is None:
            raise LedgerCorruptError(
                f"entry seq={entry.seq} references unknown phase_id={phase_id}"
            )
        if "baseline_commit" in payload:
            val = payload["baseline_commit"]
            phase.baseline_commit = val if isinstance(val, str) else None
        if "review_status" in payload:
            val = payload["review_status"]
            phase.review_status = val if isinstance(val, str) else None  # type: ignore[assignment]
        if "end_checkpoint_commit" in payload:
            val = payload["end_checkpoint_commit"]
            phase.end_checkpoint_commit = val if isinstance(val, str) else None
        if "metadata" in payload:
            md_delta = payload.get("metadata")
            if isinstance(md_delta, dict):
                merged = dict(phase.metadata or {})
                merged.update(md_delta)
                phase.metadata = merged
        return plan

    if op == "mark_blocked_descendants":
        # v0.11.0: cascade-block emitted when a worker fails. Single
        # ledger entry carries the failed task id, the reason, and the
        # full list of descendant task ids that were transitioned from
        # ``pending`` to ``blocked``. Replay walks each id and applies
        # status="blocked" with a structured ``blocked_reason``.
        # v0.29.0 Bug 6: payload may also carry the inherited
        # ``block_reason_class`` so replay rebuilds the typed enum on
        # cascaded descendants exactly as the original mutation did.
        # Pre-v0.29.0 ledger entries omit the field — the migration
        # shim in :func:`PlanManager._load_sync` backfills it from the
        # blocked_reason string after replay.
        phase_id = payload.get("phase_id")
        failed_task_id = payload.get("failed_task_id")
        reason = payload.get("reason", "")
        blocked_ids = payload.get("blocked_task_ids") or []
        block_reason_class = payload.get("block_reason_class")
        if block_reason_class not in (None, "verdict", "infrastructure", "cap"):
            block_reason_class = None
        if not isinstance(phase_id, str) or not isinstance(failed_task_id, str):
            raise LedgerCorruptError(
                f"entry seq={entry.seq} mark_blocked_descendants malformed"
            )
        if not isinstance(blocked_ids, list):
            raise LedgerCorruptError(
                f"entry seq={entry.seq} mark_blocked_descendants malformed list"
            )
        phase = _find_phase(plan, phase_id)
        if phase is None:
            return plan
        for tid in blocked_ids:
            if not isinstance(tid, str):
                continue
            for t in phase.tasks:
                if t.id == tid:
                    t.status = "blocked"
                    t.blocked_reason = (
                        f"upstream-failure:{failed_task_id}:{reason}"
                    )
                    if block_reason_class is not None:
                        t.block_reason_class = block_reason_class
                    break
        return plan

    # Unknown op — fail loudly rather than silently produce wrong state.
    raise LedgerCorruptError(f"entry seq={entry.seq} has unknown op={op!r}")


def _find_task(plan: Plan, task_id: str) -> Any:
    for phase in plan.phases:
        for task in phase.tasks:
            if task.id == task_id:
                return task
    return None


def _find_phase(plan: Plan, phase_id: str) -> Any:
    """Locate a phase by id. v0.9.0 helper for the new ledger ops."""
    for phase in plan.phases:
        if phase.id == phase_id:
            return phase
    return None


async def snapshot_plan(cwd: Path, plan: Plan, session_id: str) -> LedgerEntry:
    """Persist ``plan`` atomically to ``plan.json`` AND append a snapshot entry.

    Caller must hold :func:`plan_lock`.

    Order:
      1. Write ``plan.json`` atomically (tmp -> os.replace).
      2. Append a ``snapshot`` ledger entry containing the full plan payload.

    Both steps run under the same lock, so a crash between them at worst
    leaves a new plan.json with an out-of-date ledger — replay still works
    from the embedded Plan in the ledger, and the next successful snapshot
    makes them consistent again.
    """
    pp = autodev_root(cwd) / "plan.json"
    pp.parent.mkdir(parents=True, exist_ok=True)
    payload = plan.model_dump(mode="json")
    raw = json.dumps(payload, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(prefix=".plan.", suffix=".tmp", dir=str(pp.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, pp)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return await append_entry(
        cwd,
        op="snapshot",
        payload={"plan": payload},
        session_id=session_id,
    )


__all__ = [
    "LedgerEntry",
    "LedgerOp",
    "append_entry",
    "compute_hash",
    "read_entries",
    "stream_entries",
    "replay_ledger",
    "snapshot_plan",
]
