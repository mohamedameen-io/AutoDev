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
        return plan

    if op == "update_phase_meta":
        # v0.9.0: update arbitrary phase-level metadata fields. Currently
        # carries ``baseline_commit``, ``review_status``, and
        # ``end_checkpoint_commit`` (v0.21.0 B1). Idempotent: re-applying
        # overwrites with the same values.
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
