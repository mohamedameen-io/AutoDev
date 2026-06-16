"""Plan-drafting finite-state machine.

Flow:

  1. Write ``spec.md`` with the user's intent.
  2. Call ``explorer`` -> :class:`ExploreEvidence` via adapter.
  3. Call ``domain_expert`` -> :class:`SMEEvidence`.
  4. Call ``architect`` with spec + evidence -> plan markdown.
  5. Parse plan markdown into a :class:`Plan`. If parsing fails, retry once
     with an explicit format hint.
  6. If ``cfg.tournaments.plan.enabled``: run :class:`PlanTournament` to
     refine the plan markdown in place. The tournament IS the gate.
  7. Save to the ledger + plan.json.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from adapters.types import AgentInvocation, AgentResult
from errors import AutodevError, TournamentError
from autologging import get_logger
from orchestrator.delegation_envelope import DelegationEnvelope
from orchestrator.diagnosis_phase import run_diagnosis_phase
from orchestrator.file_existence_validator import validate_files_exist
from orchestrator.framing_phase import run_framing_phase
from orchestrator.intake_phase import run_intake_phase
from orchestrator.plan_parser import (
    PlanParseError,
    parse_plan_markdown,
)
from orchestrator.multi_branch_tournament import (
    multi_branch_parent_dir,
    run_multi_branch_plan_tournament,
)
from orchestrator.plan_tournament_runner import (
    _plan_tournament_id,
    run_plan_tournament,
)
from state.evidence import write_evidence
from state.paths import autodev_root, debug_dir, ensure_autodev_dir, spec_path
from state.schemas import (
    ExploreEvidence,
    Phase,
    Plan,
    SMEEvidence,
    Task,
)
from tournament.effort import resolve_role_effort
from tournament.state import (
    TournamentArtifactStore,
    latest_incumbent_md_across_branches,
)


if TYPE_CHECKING:
    from orchestrator import Orchestrator
    from state.file_index import CandidateDigest


logger = get_logger(__name__)


# v0.26.2 Phase 3: bounded retry loop constants. Two distinct names for
# the same numeric value — they mean different things (per Plan-agent
# feedback Q6 fix #5):
#   - ``_MAX_ARCHITECT_ATTEMPTS``: how many times the architect is called
#     before the orchestrator gives up (outer loop bound).
#   - ``_DROP_AT_RECURRENCE``: how many times the SAME (raw, reason)
#     pair must recur before the persistent-failure drop fires.
# Keeping them separate prevents the "coincidence trap" where future
# tuning of one bleeds into the other.
_MAX_ARCHITECT_ATTEMPTS: int = 3
_DROP_AT_RECURRENCE: int = 3


def _spec_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _try_read_plan_from_file(cwd: Path, text: str) -> str:
    """Fallback: if the architect wrote the plan to a file instead of returning
    it as text, read the file content.  Returns the file content if found,
    otherwise returns the original text unchanged."""

    # Common paths where the architect might write the plan.
    candidates = [
        cwd / ".autodev" / "plan.md",
        cwd / ".swarm" / "plan.md",
    ]
    for p in candidates:
        if p.exists():
            content = p.read_text(encoding="utf-8").strip()
            if content and "# Plan:" in content or "## Phase" in content:
                logger.info(
                    "plan_phase.read_plan_from_file",
                    path=str(p),
                    bytes=len(content),
                )
                return content
    return text


def _persist_failed_architect_plan(cwd: Path, plan_md: str) -> Path:
    """v0.26.2 Phase 1a: persist the architect's rejected markdown.

    Writes to ``.autodev/debug/architect-failed-<unix-ms>.md`` so the
    operator can inspect the exact output that failed validation.
    Without this, the markdown lives only in memory and is lost on
    retry/abort — making the 2026-05-12 Unity QNX failure undebuggable.

    Returns the written path so the caller can log it.
    """
    dbg = debug_dir(cwd)
    dbg.mkdir(parents=True, exist_ok=True)
    out = dbg / f"architect-failed-{int(time.time() * 1000)}.md"
    out.write_text(plan_md or "", encoding="utf-8")
    return out


def _retry_hint_text() -> str:
    """v0.26.2 Phase 3: shared retry-hint text used by the retry-env
    builder. Encodes both v0.22.4 path-shape feedback and the v0.26.2
    typed retry-envelope contract."""
    return (
        "Please use EXACTLY the canonical format. "
        "Return the plan as your text response, do NOT write to files. "
        "Paths must be plain repo-relative strings — no surrounding "
        "backticks, quotes, parentheticals, or trailing punctuation. "
        "List one path per line in EDIT_SCOPE blocks, comma-separated "
        "in `Files:` lines.\n\n"
        "If your previous plan listed files that do not exist on disk, "
        "either (a) correct the path, or (b) if you intend to CREATE "
        "the file in the task, prefix the path with [new] in the "
        "Files: line, e.g.:\n"
        "    Files: src/foo.cpp, [new] src/foo_test.cpp\n\n"
        "When path_error_raw / path_error_reason / path_error_suggestion "
        "are present in the CONTEXT block, fix that single path — do "
        "not re-draft the whole plan."
    )


def _build_retry_env(
    base_env: DelegationEnvelope,
    *,
    prior_plan_md: str,
    exc: Exception | None,
    errors_seen: dict[tuple[str, str], int],
    dropped_entries: list[str],
) -> DelegationEnvelope:
    """v0.26.2 Phase 3 / v0.27 Phase 4: build a retry envelope that carries:

    * the prior architect attempt (truncated to 2000 chars),
    * the stringified exception in ``parse_error`` (back-compat),
    * the typed ``path_error_*`` fields when ``exc`` is a
      :class:`PathValidationError`,
    * the accumulated ``prior_errors`` list so the architect can see
      "you've now emitted this same path 2 times — fix it",
    * the list of entries previously dropped on this plan-phase run.

    v0.27 (Commit 6) refactor: the context payload is now constructed
    through :class:`orchestrator.retry_envelope.TypedRetryEnvelope`.
    Behaviour is unchanged — the model is serialised via
    :meth:`TypedRetryEnvelope.as_context_dict` so the wire JSON
    matches v0.26.2 byte-for-byte.

    Returns a new :class:`DelegationEnvelope` (does not mutate the input).
    """
    from orchestrator.path_validator import PathValidationError
    from orchestrator.retry_envelope import PriorError, TypedRetryEnvelope

    # v0.36.0 D1: derive error_class for each prior error. The
    # ``errors_seen`` dict is keyed by ``(raw, reason)`` so we
    # reclassify on the fly via the same helper the validator uses;
    # non-path keys (parse-error class strings) fall through to the
    # default ``missing_on_disk`` class which renders the generic
    # diagnosis paragraph — fine since those entries never group with
    # real path failures.
    from orchestrator.file_existence_validator import _classify_rejection  # noqa: PLC0415

    prior_errors = [
        PriorError(
            raw=r,
            reason=s,
            count=n,
            error_class=_classify_rejection(r) if s else "missing_on_disk",
        )
        for (r, s), n in errors_seen.items()
    ]
    # v0.32.0 Phase 1.1: highlight the top-5 most-recurrent failures
    # so the architect-prompt's `{rejection_history}` block calls them
    # out explicitly instead of burying them in the prior_errors list.
    # Sort descending by count, ties broken by raw string for stable
    # rendering across attempts.
    most_recent_failures = sorted(
        prior_errors, key=lambda e: (-e.count, e.raw)
    )[:5]
    envelope = TypedRetryEnvelope(
        prior_attempt=prior_plan_md[:2000] if prior_plan_md else "",
        parse_error=str(exc) if exc is not None else "",
        prior_errors=prior_errors,
        most_recent_failures=most_recent_failures,
        dropped_entries=list(dropped_entries),
        hint=_retry_hint_text(),
        path_error_raw=exc.raw if isinstance(exc, PathValidationError) else "",
        path_error_reason=(
            exc.reason if isinstance(exc, PathValidationError) else ""
        ),
        path_error_suggestion=(
            (exc.suggestion or "") if isinstance(exc, PathValidationError) else ""
        ),
    )
    return base_env.model_copy(
        update={"context": {**base_env.context, **envelope.as_context_dict()}}
    )


@dataclass(frozen=True)
class _DropReport:
    """v0.27 Phase 4: per-site record of where ``_drop_entry_from_plan``
    removed an entry. Used by the caller to emit granular ledger ops
    (``task_files_entry_dropped`` vs. ``phase_edit_scope_entry_dropped``
    etc.) instead of the v0.26.2 catch-all ``scope_entry_dropped``.
    """

    plan_edit_scope: bool = False
    phase_edit_scope_phase_ids: tuple[str, ...] = ()
    task_files_task_ids: tuple[str, ...] = ()
    task_files_new_task_ids: tuple[str, ...] = ()
    task_extended_scope_task_ids: tuple[str, ...] = ()

    @property
    def any_dropped(self) -> bool:
        return (
            self.plan_edit_scope
            or bool(self.phase_edit_scope_phase_ids)
            or bool(self.task_files_task_ids)
            or bool(self.task_files_new_task_ids)
            or bool(self.task_extended_scope_task_ids)
        )


def _drop_entry_from_plan(
    plan: Plan,
    bad_path: str,
    *,
    include_files_new: bool = False,
) -> tuple[Plan, bool, _DropReport]:
    """v0.26.2 Phase 3 / v0.27 Phase 4: drop ``bad_path`` from every
    validator-walked scope site, returning ``(new_plan, was_dropped,
    drop_report)``.

    Drops from:
      - ``plan.edit_scope``
      - every ``phase.edit_scope`` (when non-None — ``None`` means inherit)
      - every ``task.files``
      - every ``task.extended_scope``
      - every ``task.files_new`` (v0.27 — opt-in via ``include_files_new``)

    Defaults preserve v0.26.2 behaviour (``files_new`` untouched). The
    persistent-drop helper uses ``include_files_new=True`` as a
    fallback when no other site contains the bad path.

    A path matches if it equals an entry OR if it equals the entry with
    a trailing ``/`` trimmed (the validator treats scope entries as
    directory prefixes).

    Uses ``model_copy(update=...)`` at every level so Pydantic field
    validators re-fire on the new objects. Direct in-place mutation would
    NOT re-trigger validation and would leave the new model in a
    potentially-invalid state.

    The third tuple element :class:`_DropReport` records which sites
    were touched so the caller can emit granular ledger ops.
    """
    norm = bad_path.rstrip("/")

    def _filter(entries: list[str]) -> tuple[list[str], bool]:
        kept = [e for e in entries if e.rstrip("/") != norm]
        return kept, len(kept) != len(entries)

    new_plan_edit, dropped_at_plan_level = _filter(list(plan.edit_scope))

    new_phases: list[Phase] = []
    phase_edit_scope_phase_ids: list[str] = []
    task_files_task_ids: list[str] = []
    task_files_new_task_ids: list[str] = []
    task_extended_scope_task_ids: list[str] = []

    for phase in plan.phases:
        new_phase_edit: list[str] | None = phase.edit_scope
        if phase.edit_scope is not None:
            ph_kept, ph_dropped = _filter(list(phase.edit_scope))
            if ph_dropped:
                phase_edit_scope_phase_ids.append(phase.id)
                new_phase_edit = ph_kept

        new_tasks: list[Task] = []
        any_task_mutated = False
        for task in phase.tasks:
            t_files, t_files_dropped = _filter(list(task.files))
            t_ext, t_ext_dropped = _filter(list(task.extended_scope))
            if include_files_new:
                t_files_new, t_files_new_dropped = _filter(
                    list(task.files_new)
                )
            else:
                t_files_new, t_files_new_dropped = list(task.files_new), False
            if t_files_dropped:
                task_files_task_ids.append(task.id)
            if t_ext_dropped:
                task_extended_scope_task_ids.append(task.id)
            if t_files_new_dropped:
                task_files_new_task_ids.append(task.id)

            if t_files_dropped or t_ext_dropped or t_files_new_dropped:
                any_task_mutated = True
                new_tasks.append(
                    task.model_copy(
                        update={
                            "files": t_files,
                            "extended_scope": t_ext,
                            "files_new": t_files_new,
                        }
                    )
                )
            else:
                new_tasks.append(task)

        if new_phase_edit is not phase.edit_scope or any_task_mutated:
            new_phases.append(
                phase.model_copy(
                    update={
                        "edit_scope": new_phase_edit,
                        "tasks": new_tasks,
                    }
                )
            )
        else:
            new_phases.append(phase)

    report = _DropReport(
        plan_edit_scope=dropped_at_plan_level,
        phase_edit_scope_phase_ids=tuple(phase_edit_scope_phase_ids),
        task_files_task_ids=tuple(task_files_task_ids),
        task_files_new_task_ids=tuple(task_files_new_task_ids),
        task_extended_scope_task_ids=tuple(task_extended_scope_task_ids),
    )
    if report.any_dropped:
        new_plan = plan.model_copy(
            update={
                "edit_scope": new_plan_edit,
                "phases": new_phases,
            }
        )
        return new_plan, True, report
    return plan, False, report


async def _validate_with_persistent_drop(
    orch: "Orchestrator",
    plan: Plan,
    cwd: Path,
    errors_seen: dict[tuple[str, str], int],
    dropped_entries: list[str],
    attempt: int,
) -> Plan:
    """v0.26.2 Phase 3: validate ``plan`` against the filesystem.

    On a :class:`PathValidationError`, this helper checks whether the
    same ``(raw, reason)`` pair has ALREADY recurred at least
    ``_DROP_AT_RECURRENCE`` times across prior architect attempts. If
    so, drop the bad entry from every validator-walked site and
    re-validate the new plan; otherwise re-raise to let the outer retry
    loop fire a fresh architect call.

    Note (Plan-agent fix #1): ``errors_seen[key]`` is **never mutated
    here**. The outer ``except`` block in :func:`run_plan_phase` is the
    sole writer of the counter — that ensures the architect gets exactly
    ``_DROP_AT_RECURRENCE`` chances at the prompt level before any
    auto-drop fires.

    Hard empty-scope guard: if dropping would leave
    ``plan.edit_scope == []`` (the documented whole-repo sentinel) the
    drop is refused and the original :class:`PathValidationError` is
    re-raised — silent widening to whole-repo would be a P0 risk.
    """
    from orchestrator.path_validator import PathValidationError

    while True:
        try:
            # v0.33.0 A1: collect plan-global ``[new]`` admissions here,
            # emit ledger ops below — ``validate_files_exist`` is sync
            # but ``ledger_append`` is async, so the validator surfaces
            # resolutions via this out-channel rather than acquiring the
            # async lock itself.
            resolutions: list[dict[str, str]] = []
            validate_files_exist(plan, cwd, resolutions=resolutions)
            for res in resolutions:
                await orch.plan_manager.ledger_append(
                    op="path_validation_resolved_via_plan_global",
                    payload=res,
                )
            return plan
        except PathValidationError as exc:
            key = (exc.raw, exc.reason)
            # Read-only check: anticipate the next outer-loop increment.
            # If we haven't met the recurrence threshold yet, bubble up
            # so the outer except increments + retries the architect.
            if errors_seen.get(key, 0) + 1 < _DROP_AT_RECURRENCE:
                raise
            # Threshold met — try to drop the bad entry. First pass
            # leaves ``files_new`` alone (v0.26.2 contract); if nothing
            # came off, retry with the files_new opt-in so v0.27 can
            # also clean up bogus ``[new]``-tagged paths the v0.26.2
            # logic stranded.
            new_plan, was_dropped, report = _drop_entry_from_plan(
                plan, exc.raw, include_files_new=False
            )
            if not was_dropped:
                new_plan, was_dropped, report = _drop_entry_from_plan(
                    plan, exc.raw, include_files_new=True
                )
            if not was_dropped:
                # Bad path nowhere in the plan — caller must surface
                # the original error.
                raise
            # Hard empty-scope guard: only fires when plan.edit_scope
            # was non-empty before AND would become empty after the
            # drop. Empty plan.edit_scope is the whole-repo sentinel —
            # silently widening here would be the P0 risk this entire
            # phase guards against.
            if plan.edit_scope and not new_plan.edit_scope:
                raise
            # v0.27 Phase 4 empty-guards: a phase-level edit_scope
            # override that becomes empty (was non-None and non-empty,
            # now an empty list) silently widens the phase back to the
            # plan's whole scope — also a P0 silent-widen risk. Refuse.
            for old_phase, new_phase in zip(plan.phases, new_plan.phases):
                if (
                    old_phase.edit_scope is not None
                    and old_phase.edit_scope
                    and new_phase.edit_scope is not None
                    and not new_phase.edit_scope
                ):
                    raise
            # v0.26.2 back-compat: keep emitting the catch-all
            # ``scope_entry_dropped`` op so existing forensics tools
            # (and ledgers replayed by older versions) still work.
            await orch.plan_manager.ledger_append(
                op="scope_entry_dropped",
                payload={
                    "path": exc.raw,
                    "reason": exc.reason,
                    "suggestion": exc.suggestion or "",
                    "attempt": attempt + 1,
                    "recurrence_count": errors_seen.get(key, 0) + 1,
                },
            )
            # v0.27 Phase 4 granular telemetry: one op per site
            # touched, with payload pinning which task/phase id lost
            # the entry. The ``init_plan`` op emitted later carries
            # the actual state mutation.
            await _emit_granular_drop_ops(
                orch,
                report,
                bad_path=exc.raw,
                reason=exc.reason,
                attempt=attempt + 1,
                recurrence_count=errors_seen.get(key, 0) + 1,
            )
            plan = new_plan
            dropped_entries.append(exc.raw)
            # v0.27 Phase 4: a task that lost ALL its files (both
            # ``files`` and ``files_new`` are empty post-drop) has no
            # work left — auto-skip it rather than dispatching an
            # empty worker.
            plan = await _auto_skip_empty_tasks(orch, plan)
            # Loop continues — re-validate the mutated plan. Because
            # the path was just removed from every validator-walked
            # site, the next call CANNOT raise on the same key —
            # guaranteed termination.


async def _emit_granular_drop_ops(
    orch: "Orchestrator",
    report: _DropReport,
    *,
    bad_path: str,
    reason: str,
    attempt: int,
    recurrence_count: int,
) -> None:
    """Emit one granular ledger op per site touched by a drop.

    Pairs with the catch-all ``scope_entry_dropped`` op so v0.26.2 forensics
    tools keep working; the granular ops let v0.27+ telemetry point at the
    specific (task_id, phase_id) that lost an entry.
    """
    base = {
        "path": bad_path,
        "reason": reason,
        "attempt": attempt,
        "recurrence_count": recurrence_count,
    }
    if report.plan_edit_scope:
        # The plan-level drop is already covered by the catch-all op —
        # no granular variant needed (the v0.26.2 schema is sufficient).
        pass
    for phase_id in report.phase_edit_scope_phase_ids:
        await orch.plan_manager.ledger_append(
            op="phase_edit_scope_entry_dropped",
            payload={**base, "phase_id": phase_id},
        )
    for task_id in report.task_files_task_ids:
        await orch.plan_manager.ledger_append(
            op="task_files_entry_dropped",
            payload={**base, "task_id": task_id},
        )
    for task_id in report.task_files_new_task_ids:
        await orch.plan_manager.ledger_append(
            op="task_files_new_entry_dropped",
            payload={**base, "task_id": task_id},
        )
    for task_id in report.task_extended_scope_task_ids:
        await orch.plan_manager.ledger_append(
            op="task_extended_scope_entry_dropped",
            payload={**base, "task_id": task_id},
        )


# v0.39.0 (Cluster C2b): a ``complex`` task touching this many or more
# files on a huge repo is treated as a decomposition smell — the architect
# should likely have split it into 2–3 ``medium`` tasks scoped to one
# subsystem each. Advisory only; never gates the plan.
_SCOPE_BREADTH_THRESHOLD: int = 6
# v0.39.0 (Cluster C2b): a single literal file larger than this byte count
# is also a decomposition smell (one mega-file dominating a complex task).
_LARGE_FILE_BYTES: int = 100_000


async def _advise_task_decomposition(
    orch: "Orchestrator", plan: Plan, cwd: Path
) -> None:
    """v0.39.0 (Cluster C2b): post-parse decomposition advisory for huge repos.

    On Unity-class repos, ``complex`` tasks that touch many files (or one
    very large file) tend to exhaust even their auto-scaled turn budget and
    fail with ``error_max_turns`` — they should have been split into finer
    ``medium`` tasks scoped to a single subsystem (each of which gets its
    own scaled budget and fails independently). This helper logs a warning
    and emits a best-effort ``task_under_decomposed`` ledger breadcrumb
    (``source="planner_advisory"``) for each such task so retrospectives can
    correlate the failure with the plan shape.

    Purely observational: it NEVER mutates or rejects the plan, NEVER changes
    control flow, and NEVER raises (the entire body is wrapped defensively).
    No-op on small repos (gated on ``_repo_capacity.is_huge``).
    """
    try:
        if not getattr(
            getattr(orch, "_repo_capacity", None), "is_huge", False
        ):
            return
        for phase in plan.phases:
            for task in getattr(phase, "tasks", []) or []:
                if task.complexity != "complex":
                    continue
                files = task.files or []
                breadth_smell = len(files) >= _SCOPE_BREADTH_THRESHOLD
                large_file_smell = False
                if not breadth_smell:
                    # Cheap large-file check on literal (non-glob) paths
                    # only — globs / patterns are skipped (os.stat would
                    # raise on them and we don't want to expand). The whole
                    # check is best-effort; OSError (missing / permission)
                    # is swallowed.
                    for rel in files:
                        if not rel or any(c in rel for c in "*?[]"):
                            continue
                        try:
                            if os.stat(cwd / rel).st_size > _LARGE_FILE_BYTES:
                                large_file_smell = True
                                break
                        except OSError:
                            continue
                if not (breadth_smell or large_file_smell):
                    continue
                logger.warning(
                    "plan_phase.task_under_decomposed_advisory",
                    task_id=task.id,
                    complexity="complex",
                    file_count=len(files),
                    files=files[:10],
                )
                try:
                    await orch.plan_manager.ledger_append(
                        op="task_under_decomposed",
                        payload={
                            "task_id": task.id,
                            "source": "planner_advisory",
                            "attempt": 0,
                            "file_count": len(files),
                            "files": files[:10],
                            "complexity": task.complexity,
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "plan_phase.ledger_append_failed",
                        op="task_under_decomposed",
                        err=str(exc),
                    )
    except Exception as exc:  # noqa: BLE001
        # Advisory must never break the plan phase.
        logger.warning(
            "plan_phase.task_decomposition_advisory_failed", err=str(exc)
        )


async def _auto_skip_empty_tasks(
    orch: "Orchestrator", plan: Plan
) -> Plan:
    """v0.27 Phase 4: tasks whose ``files`` AND ``files_new`` lists are
    both empty after a drop have no work left for the developer to do —
    auto-transition them to ``status="skipped"`` rather than dispatching
    a worker that would produce an empty diff.

    Emits ``task_auto_skipped`` (audit-only) for each transition.
    Returns the mutated plan (no-op when nothing was auto-skipped).
    """
    new_phases: list[Phase] = []
    any_skipped = False
    for phase in plan.phases:
        new_tasks: list[Task] = []
        phase_changed = False
        for task in phase.tasks:
            if (
                not task.files
                and not task.files_new
                and task.status == "pending"
            ):
                phase_changed = True
                any_skipped = True
                new_tasks.append(task.model_copy(update={"status": "skipped"}))
                await orch.plan_manager.ledger_append(
                    op="task_auto_skipped",
                    payload={
                        "task_id": task.id,
                        "reason": "all_files_dropped",
                    },
                )
            else:
                new_tasks.append(task)
        if phase_changed:
            new_phases.append(phase.model_copy(update={"tasks": new_tasks}))
        else:
            new_phases.append(phase)
    if any_skipped:
        return plan.model_copy(update={"phases": new_phases})
    return plan


async def run_plan_phase(orch: "Orchestrator", intent: str) -> Plan:
    """Execute the plan phase end-to-end and return the approved plan."""
    # v0.26.0: the v0.25.4 InlineAdapter+tournaments preflight check was
    # removed alongside InlineAdapter itself — no inline adapter exists,
    # so no mismatch is possible.
    cwd = orch.cwd

    ensure_autodev_dir(cwd)
    sp = spec_path(cwd)
    sp.write_text(intent.strip() + "\n", encoding="utf-8")
    spec_hash = _spec_hash(intent)
    logger.info("plan_phase.spec_written", bytes=len(intent))

    orch.guardrails.start_task("plan")
    try:
        explorer_env = DelegationEnvelope(
            task_id="plan",
            target_agent="explorer",
            action="explore",
            constraints=["Read-only: no edits, only Read/Glob/Grep."],
            acceptance="Produce a short findings summary relevant to the spec.",
            context={"spec": intent},
        )
        explorer_result = await _delegate(orch, "explorer", explorer_env)
        explore_ev = ExploreEvidence(
            task_id="plan",
            findings=explorer_result.text,
            files_referenced=[str(p) for p in explorer_result.files_changed],
        )
        await write_evidence(cwd, "plan-explore", explore_ev)

        domain_expert_env = DelegationEnvelope(
            task_id="plan",
            target_agent="domain_expert",
            action="consult",
            acceptance="Identify domain constraints and external references.",
            context={
                "spec": intent,
                "explorer_findings": explorer_result.text[:4000],
            },
        )
        domain_expert_result = await _delegate(orch, "domain_expert", domain_expert_env)
        sme_ev = SMEEvidence(
            task_id="plan",
            topic="plan",
            findings=domain_expert_result.text,
            confidence="MEDIUM",
        )
        await write_evidence(cwd, "plan-domain_expert", sme_ev)

        # ADR-0045: intake & clarification phase — flag-guarded, fail-safe
        # (mirrors the framing guard below). On success we REBIND the local
        # ``intent`` to the LOCKED ENRICHED SPEC and ``spec_hash`` to its hash
        # so EVERY downstream consumer (candidate_digest query, framing,
        # architect ``context["spec"]``) uses the enriched spec, not the raw
        # intent (intake design §2.3/§5.6). run_intake_phase is itself fail-safe
        # and flag-guards internally; the outer guard avoids the read-evidence
        # round-trip when intake is off. On degrade ``spec == raw intent`` so
        # the rebind is harmless. Intake must NEVER block planning.
        if orch.cfg.intake.enabled and not os.getenv("AUTODEV_INTAKE_DISABLED"):
            try:
                intake_outcome = await run_intake_phase(orch, intent)
                intent = intake_outcome.spec
                spec_hash = intake_outcome.spec_hash
            except Exception as exc:  # noqa: BLE001 — intake must never block planning
                logger.warning("plan_phase.intake_phase_failed", err=str(exc))

        # ADR-0046: diagnosis (reproduce-first) phase — flag-guarded, fail-safe.
        # run_diagnosis_phase internally gates on is-bug-fix + cfg.bug_only and
        # is fail-safe, so we do NOT pre-gate; the outer guard only avoids the
        # read-evidence round-trip when diagnosis is off. ``intent`` is now the
        # enriched spec. Diagnosis must NEVER block planning.
        diagnosis_outcome = None
        if orch.cfg.diagnosis.enabled and not os.getenv(
            "AUTODEV_DIAGNOSIS_DISABLED"
        ):
            try:
                diagnosis_outcome = await run_diagnosis_phase(
                    orch, intent, explorer_result.text[:4000]
                )
            except Exception as exc:  # noqa: BLE001 — diagnosis must never block planning
                logger.warning("plan_phase.diagnosis_phase_failed", err=str(exc))

        # v0.25.0: query the file/symbol index for candidate paths/symbols
        # relevant to the spec text. The architect's prompt has a CANDIDATE
        # FILES section instructing it to prefer these paths over inventing
        # new ones; missing/disabled index degrades to an empty string and
        # the architect prompt's "PREFER paths from this list" sentence
        # becomes a no-op (no broken behavior).
        candidate_digest_str = ""
        # ADR-0044: keep the STRUCTURED digest object (not just the rendered
        # string) so the framing signals can read .symbol_hits[].file_path /
        # .file_hits[].path — a string has neither.
        candidate_digest_obj: CandidateDigest | None = None
        if orch.cfg.index_enabled:
            db_path = orch.cwd / orch.cfg.index_path
            if db_path.exists():
                try:
                    from state.file_index import IndexQuery

                    digest = IndexQuery(db_path).get_candidates_for_spec(
                        spec_text=intent, limit=20
                    )
                    candidate_digest_obj = digest
                    candidate_digest_str = digest.render(max_chars=2500)
                except Exception as exc:  # noqa: BLE001 - never block plan
                    logger.warning(
                        "plan_phase.index_query_failed", err=str(exc)
                    )

        # ADR-0044: framing/altitude phase — flag-guarded, fail-safe. Poses the
        # patch-vs-architecture decision and threads the chosen altitude into the
        # architect. run_framing_phase re-reads evidence on resume (zero LLM calls)
        # and must NEVER block planning.
        framing_decision = None
        if orch.cfg.framing.enabled and not os.getenv("AUTODEV_FRAMING_DISABLED"):
            try:
                framing_decision = await run_framing_phase(
                    orch=orch,
                    intent=intent,
                    explorer_findings=explorer_result.text[:4000],
                    domain_expert_findings=domain_expert_result.text[:4000],
                    candidate_digest=candidate_digest_obj,
                    spec_hash=spec_hash,
                    diagnosis_signals=diagnosis_outcome,
                )
            except Exception as exc:  # noqa: BLE001 — framing must never block planning
                logger.warning("plan_phase.framing_phase_failed", err=str(exc))

        architect_env = DelegationEnvelope(
            task_id="plan",
            target_agent="architect",
            action="document",
            acceptance=(
                "IMPORTANT: Return the ENTIRE plan as your text response. "
                "Do NOT write it to a file. Do NOT use the Write or Task tool to create plan files. "
                "Your response text must BE the plan, in this exact markdown format:\n"
                "  # Plan: <title>\n"
                "  ## Phase <n>: <title>\n"
                "  ### Task <n.m>: <title>\n"
                "    - Description: <text>\n"
                "    - Files: file1, file2\n"
                "    - Acceptance:\n"
                "      - [ ] <criterion>\n"
            ),
            context={
                "spec": intent,
                "explorer_findings": explorer_result.text[:4000],
                "domain_expert_findings": domain_expert_result.text[:4000],
                "candidate_files": candidate_digest_str,
                # ADR-0044: the chosen altitude + classification. The architect
                # implements THIS strategy at THIS altitude (see architect.md
                # "FRAMING PHASE COUPLING"). Defaults keep behavior identical when
                # framing is disabled or degrades to local_defect.
                "chosen_strategy": (
                    framing_decision.chosen_approach.name
                    if framing_decision is not None
                    else "local_patch"
                ),
                "framing_classification": (
                    framing_decision.classification
                    if framing_decision is not None
                    else "local_defect"
                ),
                # ADR-0046: additive diagnosis context (harmless if architect.md
                # does not consume them yet). Empty / "unknown" when diagnosis
                # did not run (disabled, not-a-bug, or degraded).
                "diagnosed_cause": (
                    (diagnosis_outcome.confirmed_cause or "")
                    if diagnosis_outcome is not None and diagnosis_outcome.ran
                    else ""
                ),
                "diagnosis_seam": (
                    diagnosis_outcome.seam
                    if diagnosis_outcome is not None and diagnosis_outcome.ran
                    else "unknown"
                ),
            },
        )
        # v0.26.2 Phase 3: bounded architect-retry loop with persistent-
        # failure drop. Replaces v0.22.4's single-shot retry. The
        # architect is called up to ``_MAX_ARCHITECT_ATTEMPTS`` times;
        # on each failure the rejected markdown is archived (Phase 1a)
        # and a structured retry env is built (Phase 1b). When the SAME
        # ``(raw, reason)`` recurs ``_DROP_AT_RECURRENCE`` times,
        # ``_validate_with_persistent_drop`` drops the bad entry from
        # every validator-walked scope site and re-validates — with a
        # hard empty-scope guard that refuses to silently widen to
        # whole-repo.
        from pydantic import ValidationError as _PydValidationError

        from orchestrator.path_validator import PathValidationError

        plan: Plan
        plan_md: str = ""
        errors_seen: dict[tuple[str, str], int] = {}
        dropped_entries: list[str] = []
        last_exc: Exception | None = None
        # v0.32.0 Phase 1.4: track the parsed plan from the most
        # recent attempt that PARSED but failed validation, plus the
        # full list of archived dumps. Recovery tier 4 needs a
        # parsed plan to degrade scope; tier 7 needs the dump list
        # for the forensic summary.
        last_parsed_plan: Plan | None = None
        archived_dumps_paths: list[str] = []
        architect_spec = orch.registry.get("architect")
        retry_max = (
            (architect_spec.max_turns or 5) + 2 if architect_spec else 7
        )

        # v0.32.0 Phase 1.2: per-(scope_id, role) budget escalation
        # for the architect's plan-phase scope. Mirrors the Phase 3
        # execute-phase escalation but keyed on the literal scope
        # ``"plan_phase"`` so a session-wide architect ladder never
        # collides with any execute-phase task ladder.
        _PLAN_PHASE_SCOPE = "plan_phase"
        _budget_tracker = getattr(orch, "_budget_escalation_tracker", None)

        for attempt in range(_MAX_ARCHITECT_ATTEMPTS):
            substitutions: dict[str, str] = {}
            if attempt == 0:
                env = architect_env
                max_turns_override: int | None = None
            else:
                env = _build_retry_env(
                    architect_env,
                    prior_plan_md=plan_md,
                    exc=last_exc,
                    errors_seen=errors_seen,
                    dropped_entries=dropped_entries,
                )
                max_turns_override = retry_max
                # v0.32.0 Phase 1.1: render the architect's
                # ``{rejection_history}`` block from the typed
                # envelope so the architect sees its prior failures
                # called out in the system prompt itself, not just
                # buried in the CONTEXT block.
                from orchestrator.retry_envelope import (  # noqa: PLC0415
                    PriorError,
                    TypedRetryEnvelope,
                )

                _typed = TypedRetryEnvelope(
                    most_recent_failures=sorted(
                        [
                            PriorError(raw=r, reason=s, count=n)
                            for (r, s), n in errors_seen.items()
                        ],
                        key=lambda e: (-e.count, e.raw),
                    )[:5],
                )
                substitutions["rejection_history"] = (
                    _typed.render_rejection_history(attempt=attempt + 1)
                )

            # v0.32.0 Phase 1.2: query the budget tracker AFTER the
            # base ``max_turns_override`` is set so the escalation
            # bumps the post-retry-default budget rather than the
            # configured base. ``escalation_attempt > 0`` means the
            # prior call returned ``error_max_turns`` and we now apply
            # the 1.5×/2.0× curve. The breadcrumb is emitted before
            # the dispatch so post-mortems can correlate.
            if _budget_tracker is not None:
                _esc_attempt = _budget_tracker.current_attempt(
                    _PLAN_PHASE_SCOPE, "architect"
                )
                if _esc_attempt > 0:
                    _base_max = max_turns_override or (
                        (architect_spec.max_turns or 5) if architect_spec else 5
                    )
                    _new_max, _ = _budget_tracker.escalate_for(
                        _PLAN_PHASE_SCOPE,
                        "architect",
                        base_max_turns=_base_max,
                    )
                    if _new_max != _base_max:
                        try:
                            await orch.plan_manager.ledger_append(
                                op="plan_phase_budget_escalation",
                                payload={
                                    "from_max_turns": _base_max,
                                    "to_max_turns": _new_max,
                                    "attempt": _esc_attempt,
                                    "reason": "architect_max_turns_recurrence",
                                },
                            )
                        except Exception as exc:  # noqa: BLE001 — best-effort
                            logger.warning(
                                "plan_phase.budget_escalation_ledger_failed",
                                err=str(exc),
                            )
                        max_turns_override = _new_max

            # v0.36.0 D3: on retry attempt ≥ 2, swap the architect to a
            # cheaper model when the most-recent failure is structural
            # (missing_on_disk / new_md_deliverable). Opus burns money
            # on path-list corrections that sonnet handles just as well.
            # We mutate-then-restore the registry entry for this single
            # dispatch only (same pattern Tier 5 uses for the opus bump
            # in ``_run_phase1_4_recovery``).
            structural_model_swap: str | None = None
            original_architect_spec_for_swap = None
            if attempt >= 1 and isinstance(last_exc, PathValidationError):
                from orchestrator.plan_phase_recovery import (  # noqa: PLC0415
                    should_change_model_for_class,
                )

                current_model = (
                    architect_spec.model if architect_spec is not None else None
                )
                # Read the configured cheaper-model identifier; falls
                # back to the documented default ``"sonnet"`` if the
                # cfg lacks the architect entry (defensive — older
                # stub configs in unit tests).
                _arch_cfg = getattr(
                    orch.cfg, "agents", {}
                ).get("architect") if hasattr(orch.cfg, "agents") else None
                structural_retry_model = getattr(
                    _arch_cfg, "structural_retry_model", "sonnet"
                )
                err_class = getattr(
                    last_exc, "error_class", "missing_on_disk"
                )
                target = should_change_model_for_class(
                    current_model, err_class, structural_retry_model
                )
                if target is not None and architect_spec is not None:
                    structural_model_swap = target
                    original_architect_spec_for_swap = architect_spec
                    bumped = architect_spec.model_copy(update={"model": target})
                    orch.registry["architect"] = bumped
                    try:
                        await orch.plan_manager.ledger_append(
                            op="architect_model_changed_for_retry",
                            payload={
                                "attempt": attempt + 1,
                                "from_model": current_model or "",
                                "to_model": target,
                                "rejection_class": err_class,
                            },
                        )
                    except Exception as exc:  # noqa: BLE001 - best-effort
                        logger.warning(
                            "plan_phase.model_swap_ledger_failed",
                            err=str(exc),
                        )

            _dispatch_start_s = time.monotonic()
            try:
                architect_result = await _delegate(
                    orch,
                    "architect",
                    env,
                    max_turns_override=max_turns_override,
                    system_prompt_substitutions=substitutions,
                )
            finally:
                if (
                    structural_model_swap is not None
                    and original_architect_spec_for_swap is not None
                ):
                    orch.registry["architect"] = original_architect_spec_for_swap
            _dispatch_duration_s = time.monotonic() - _dispatch_start_s
            # v0.32.0 Phase 1.2: feed the adapter's subtype back into
            # the tracker so the NEXT attempt's escalation decision
            # sees this attempt's outcome. ``error_max_turns`` and
            # ``parse_failed`` count as escalation-worthy failures;
            # any other subtype (success, transport-class failure)
            # resets the counter.
            if _budget_tracker is not None:
                _sub = getattr(architect_result, "subtype", None)
                if _sub == "parse_failed":
                    # The escalator's record_failure semantics treat
                    # any non-error_max_turns subtype as a reset, but
                    # for the plan-phase architect we want parse
                    # failures to count too. Fold them into the same
                    # bucket as ``error_max_turns`` for tracking.
                    _budget_tracker.record_failure(
                        _PLAN_PHASE_SCOPE, "architect", "error_max_turns"
                    )
                else:
                    _budget_tracker.record_failure(
                        _PLAN_PHASE_SCOPE, "architect", _sub
                    )
            plan_md = architect_result.text
            # Fallback: if architect wrote to a file instead of returning
            # text, try reading the plan from known file locations.
            plan_md = _try_read_plan_from_file(cwd, plan_md)

            try:
                plan = parse_plan_markdown(plan_md, spec_hash=spec_hash)
                # v0.32.0 Phase 1.4: stash the parsed plan BEFORE
                # validation so the recovery tiers can degrade scope
                # against it even when validation rejects the run.
                last_parsed_plan = plan
                # v0.26.2 Phase 3: validate via the persistent-drop
                # helper — on recurrence threshold the bad entry is
                # dropped + a ``scope_entry_dropped`` ledger op is
                # appended. The empty-scope guard re-raises the
                # original error if dropping would empty plan.edit_scope.
                plan = await _validate_with_persistent_drop(
                    orch,
                    plan,
                    cwd,
                    errors_seen,
                    dropped_entries,
                    attempt,
                )
                break  # success — exit the outer retry loop
            except (
                PlanParseError,
                _PydValidationError,
                PathValidationError,
            ) as exc:
                last_exc = exc
                # Phase 1a: archive the rejected markdown for diagnostics.
                archived = _persist_failed_architect_plan(cwd, plan_md)
                archived_dumps_paths.append(str(archived))
                if isinstance(exc, PathValidationError):
                    key = (exc.raw, exc.reason)
                    errors_seen[key] = errors_seen.get(key, 0) + 1
                    # v0.36.0 D1: per-rejection class breadcrumb. The
                    # path-validation walk doesn't know which task the
                    # path lived on (the validator runs against the
                    # full plan tree); use the empty-string sentinel
                    # for ``task_id`` so F3's status surface still has
                    # a row to display.
                    try:
                        await orch.plan_manager.ledger_append(
                            op="path_rejection_recorded",
                            payload={
                                "task_id": "",
                                "path": exc.raw,
                                "class": getattr(
                                    exc, "error_class", "missing_on_disk"
                                ),
                            },
                        )
                    except Exception as op_exc:  # noqa: BLE001 - best-effort
                        logger.warning(
                            "plan_phase.path_rejection_ledger_failed",
                            err=str(op_exc),
                        )
                else:
                    # v0.27 Phase 4: route PlanParseError /
                    # PydValidationError through a typed error-class
                    # counter so the third recurrence emits a
                    # ``architect_persistent_*_error`` ledger op (caller
                    # can surface this in the doctor / metrics CLI).
                    err_class_key = (type(exc).__name__, "")
                    errors_seen[err_class_key] = (
                        errors_seen.get(err_class_key, 0) + 1
                    )
                    if errors_seen[err_class_key] >= _DROP_AT_RECURRENCE:
                        op_name = (
                            "architect_persistent_parse_error"
                            if isinstance(exc, PlanParseError)
                            else "architect_persistent_pyd_error"
                        )
                        await orch.plan_manager.ledger_append(
                            op=op_name,
                            payload={
                                "exc_class": type(exc).__name__,
                                "attempt": attempt + 1,
                                "recurrence_count": errors_seen[err_class_key],
                                "archived_path": str(archived),
                            },
                        )
                logger.warning(
                    "plan_phase.parse_failed_retrying",
                    err=str(exc),
                    archived_to=str(archived),
                    attempt=attempt + 1,
                    max_attempts=_MAX_ARCHITECT_ATTEMPTS,
                )
                # v0.36.0 F1: per-attempt failure breadcrumb. Records
                # model + duration + the primary rejection class so
                # ``autodev status --blocked`` can render an attempt
                # timeline without scanning the orchestrator's
                # structured log. Best-effort — never let ledger I/O
                # mask the upstream failure.
                try:
                    _primary_class = (
                        getattr(exc, "error_class", "missing_on_disk")
                        if isinstance(exc, PathValidationError)
                        else type(exc).__name__
                    )
                    _attempt_model = (
                        structural_model_swap
                        if structural_model_swap is not None
                        else (
                            architect_spec.model
                            if architect_spec is not None
                            else ""
                        )
                    )
                    await orch.plan_manager.ledger_append(
                        op="architect_attempt_failed",
                        payload={
                            "attempt": attempt + 1,
                            "model": _attempt_model or "",
                            "duration_s": float(
                                round(_dispatch_duration_s, 3)
                            ),
                            "rejection_count": int(
                                sum(errors_seen.values())
                            ),
                            "primary_class": _primary_class,
                        },
                    )
                except Exception as op_exc:  # noqa: BLE001
                    logger.warning(
                        "plan_phase.attempt_failed_ledger_failed",
                        err=str(op_exc),
                    )
                if attempt == _MAX_ARCHITECT_ATTEMPTS - 1:
                    # v0.32.0 Phase 1.4: enter the four-tier recovery
                    # path BEFORE re-raising. Each tier is best-effort
                    # — when none can produce a clean plan we hard-fail
                    # with the forensic summary as the exception body.
                    recovered_plan = await _run_phase1_4_recovery(
                        orch,
                        cwd=cwd,
                        last_parsed_plan=last_parsed_plan,
                        errors_seen=errors_seen,
                        archived_dumps=archived_dumps_paths,
                        last_exc=last_exc,
                        spec_hash=spec_hash,
                        architect_env=architect_env,
                        plan_md=plan_md,
                        dropped_entries=dropped_entries,
                        attempts=_MAX_ARCHITECT_ATTEMPTS,
                        retry_max=retry_max,
                    )
                    if recovered_plan is not None:
                        plan = recovered_plan
                        break
                    # Tier 7: hard-fail with forensic summary attached.
                    raise

        if orch.cfg.tournaments.plan.enabled:
            num_branches = orch.cfg.tournaments.plan.num_branches
            # v0.23.0 C4: plan-tournament huge-repo fast-path. Unity-scale
            # repos burned 80 min on the multi-branch plan tournament
            # (3 branches × 3-5 passes × 5 judges per branch). On huge
            # repos, fall back to a single-branch tournament so the user
            # gets a plan in <20 min instead. Operators can opt out by
            # setting ``cfg.tournaments.plan.huge_repo_overrides_disabled = True``
            # (treated as missing == False for back-compat).
            _is_huge_for_plan = bool(
                getattr(getattr(orch, "_repo_capacity", None), "is_huge", False)
            )
            _huge_overrides_disabled = bool(
                getattr(
                    orch.cfg.tournaments.plan,
                    "huge_repo_overrides_disabled",
                    False,
                )
            )
            if _is_huge_for_plan and not _huge_overrides_disabled and num_branches > 1:
                logger.info(
                    "plan_phase.huge_repo_fast_path",
                    original_num_branches=num_branches,
                    reason="is_huge=True; falling back to single-branch tournament",
                )
                num_branches = 1
            try:
                if num_branches > 1:
                    # v0.12.0 multi-branch path.
                    outcome = await run_multi_branch_plan_tournament(
                        orch,
                        plan_md,
                        intent,
                        spec_hash,
                        n_branches=num_branches,
                    )
                    refined_md = outcome.final_md
                    logger.info(
                        "plan_phase.multi_branch_tournament_done",
                        n_branches=num_branches,
                        n_survivors=sum(1 for b in outcome.branches if b.success),
                        meta_passes=len(outcome.meta_history),
                    )
                else:
                    # Legacy single-branch path (v0.11.x and earlier).
                    refined_md = await run_plan_tournament(
                        orch, plan_md, intent, spec_hash
                    )
            except TournamentError as exc:
                logger.warning("plan_phase.tournament_failed", err=str(exc))
                # Salvage path (v0.6.0 / Issue 2 + v0.12.0 multi-branch
                # extension): on a tournament error, try to recover the
                # latest persisted ``incumbent_after_NN.md`` rather than
                # dropping every refinement that already landed on disk.
                # For multi-branch runs, walk all per-branch dirs and
                # pick the highest-pass-num incumbent across them.
                # Falling through to ``refined_md = plan_md`` if recovery
                # fails preserves legacy behavior.
                refined_md = plan_md
                try:
                    if num_branches > 1:
                        # Multi-branch salvage walk.
                        parent = multi_branch_parent_dir(orch.cwd, spec_hash)
                        recovered_tuple = latest_incumbent_md_across_branches(
                            parent
                        )
                        if recovered_tuple is not None:
                            recovered_md, branch_idx, pass_num = recovered_tuple
                            refined_md = recovered_md
                            logger.info(
                                "plan_phase.multi_branch_recovered_from_disk",
                                branch_index=branch_idx,
                                pass_num=pass_num,
                                bytes=len(recovered_md),
                            )
                        else:
                            logger.info(
                                "plan_phase.multi_branch_no_incumbent_on_disk"
                            )
                    else:
                        # Legacy single-branch salvage.
                        artifact_dir = (
                            autodev_root(orch.cwd)
                            / "tournaments"
                            / _plan_tournament_id(spec_hash)
                        )
                        store = TournamentArtifactStore(artifact_dir)
                        recovered = store.latest_incumbent_md()
                        if recovered:
                            refined_md = recovered
                            logger.info(
                                "plan_phase.tournament_recovered_from_disk",
                                pass_num=store.latest_incumbent_pass_num(),
                                bytes=len(recovered),
                            )
                except Exception as recovery_exc:  # noqa: BLE001
                    logger.warning(
                        "plan_phase.tournament_recovery_failed",
                        err=str(recovery_exc),
                    )
            if refined_md and refined_md != plan_md:
                # v0.27 Phase 5 (audit §5): structural-validity gate on
                # the tournament-refined markdown. Three rejection modes:
                #   1. parse_error — refined_md isn't a parseable plan.
                #   2. validate_files_exist — parses, but lists paths
                #      that don't exist on disk.
                #   3. persistent_drop_refused — validation failed and
                #      the empty-scope guard refused the drop.
                # On any rejection, log + emit
                # ``tournament_output_rejected_structurally`` and fall
                # back to the pre-tournament plan (already in ``plan``
                # and ``plan_md``).
                fallback_plan = plan
                fallback_plan_md = plan_md
                rejection_reason: str | None = None
                try:
                    candidate_plan = parse_plan_markdown(
                        refined_md, spec_hash=spec_hash
                    )
                except PlanParseError as exc:
                    rejection_reason = "parse_error"
                    logger.warning(
                        "plan_phase.tournament_refined_unparseable",
                        err=str(exc),
                    )
                else:
                    try:
                        candidate_plan = await _validate_with_persistent_drop(
                            orch,
                            candidate_plan,
                            cwd,
                            # Fresh counters: the tournament gate is its
                            # own pass — pre-tournament error history
                            # shouldn't immediately trigger a drop.
                            errors_seen={},
                            dropped_entries=[],
                            attempt=0,
                        )
                        plan = candidate_plan
                        plan_md = refined_md
                        logger.info(
                            "plan_phase.tournament_applied",
                            pre_bytes=len(plan_md),
                            post_bytes=len(refined_md),
                        )
                    except PathValidationError as exc:
                        rejection_reason = "validate_files_exist"
                        logger.warning(
                            "plan_phase.tournament_refined_invalid_paths",
                            err=str(exc),
                        )
                if rejection_reason is not None:
                    await orch.plan_manager.ledger_append(
                        op="tournament_output_rejected_structurally",
                        payload={
                            "reason": rejection_reason,
                            "attempt": 0,
                        },
                    )
                    # v0.42.1 F1b (ADR-0047): the plan tournament's refined
                    # output was rejected and we silently fall back to the
                    # pre-tournament plan — THE Run-5 silent fallback. Route the
                    # degrade through the resolver so it leaves a breadcrumb
                    # (observability only — the fallback still applies).
                    try:
                        from orchestrator.blocker_resolver import (
                            record_phase_degrade,
                        )

                        await record_phase_degrade(
                            orch,
                            "plan_tournament",
                            RuntimeError(
                                rejection_reason
                                or "tournament_output_rejected"
                            ),
                        )
                    except Exception:  # noqa: BLE001 - never break planning
                        pass
                    plan = fallback_plan
                    plan_md = fallback_plan_md
            else:
                logger.info("plan_phase.tournament_no_change")

        # v0.39.0 (Cluster C2b): post-parse decomposition advisory. Runs on
        # the final approved plan, just before it's committed to the ledger.
        # Advisory only — never mutates/rejects the plan or raises.
        await _advise_task_decomposition(orch, plan, cwd)
        await orch.plan_manager.init_plan(plan)
        logger.info(
            "plan_phase.approved",
            plan_id=plan.plan_id,
            phases=len(plan.phases),
            tasks=sum(len(p.tasks) for p in plan.phases),
        )
        return plan
    finally:
        orch.guardrails.end_task("plan")


async def _run_phase1_4_recovery(
    orch: "Orchestrator",
    *,
    cwd: Path,
    last_parsed_plan: Plan | None,
    errors_seen: dict[tuple[str, str], int],
    archived_dumps: list[str],
    last_exc: Exception | None,
    spec_hash: str,
    architect_env: DelegationEnvelope,
    plan_md: str,
    dropped_entries: list[str],
    attempts: int,
    retry_max: int,
) -> Plan | None:
    """v0.32.0 Phase 1.4: run the four recovery tiers between the
    architect's third failure and the hard-fail re-raise.

    Returns a successfully-validated :class:`Plan` when one of the
    tiers (4 or 5) produces a clean plan; ``None`` when the caller
    should hard-fail. Always emits the recovery_hint + forensic
    summary onto the orchestrator's structured-log stream so the CLI
    surfacing in Phase 5 can pick them up.

    The helper is split out of the main loop so the recovery branch
    is testable in isolation without exercising the entire architect
    retry pipeline.
    """
    from orchestrator.path_validator import PathValidationError  # noqa: PLC0415
    from orchestrator.plan_phase_recovery import (  # noqa: PLC0415
        run_recovery_tiers,
    )

    architect_spec = orch.registry.get("architect")
    current_model = (
        architect_spec.model if architect_spec is not None else None
    )
    # Tier 5 target: the orchestrator config doesn't yet have a
    # canonical opus pin field; hard-coding the latest opus
    # identifier keeps the helper self-contained. When the config
    # grows a typed ``models.opus`` field, swap this for that value.
    opus_target = "claude-opus-4-7"

    outcome = run_recovery_tiers(
        plan=last_parsed_plan,
        errors_seen=errors_seen,
        archived_dumps=archived_dumps,
        last_exception=last_exc,
        attempts=attempts,
        current_architect_model=current_model,
        opus_model_id=opus_target,
    )

    # v0.36.0 F1: replace the v0.32.0 ``{0 → 0}`` lie with the truthful
    # from/to budget values. ``retry_max`` is the architect-retry
    # default budget; on entry the recovery path consumes one extra
    # dispatch at that budget (Tier 5 re-prompt), so the breadcrumb
    # carries ``base_max → retry_max``. Best-effort writes — never
    # mask the eventual hard-fail.
    _base_max = (
        (architect_spec.max_turns or 5) if architect_spec is not None else 5
    )
    try:
        await orch.plan_manager.ledger_append(
            op="plan_phase_budget_escalation",
            payload={
                "from_max_turns": int(_base_max),
                "to_max_turns": int(retry_max),
                "attempt": attempts,
                "reason": "phase1_4_recovery_entered",
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "plan_phase.recovery_breadcrumb_failed", err=str(exc)
        )

    # v0.36.0 F1: per-recovery-tier transition breadcrumbs. Tier 4
    # (scope degradation) and Tier 5 (model escalation) each get one
    # op — emitted with the from/to state and the outcome the tier
    # produced. Tier 6 (recovery_hint) and Tier 7 (forensic summary)
    # always succeed so we record them as "applied" with the
    # rendered hint class / dump count as the to_state.
    async def _emit_tier_op(
        tier: int,
        outcome: str,
        reason: str,
        from_state: str | None,
        to_state: str | None,
    ) -> None:
        try:
            await orch.plan_manager.ledger_append(
                op="recovery_tier_attempted",
                payload={
                    "tier": tier,
                    "outcome": outcome,
                    "reason": reason,
                    "from_state": from_state,
                    "to_state": to_state,
                },
            )
        except Exception as ex:  # noqa: BLE001
            logger.warning(
                "plan_phase.recovery_tier_ledger_failed", err=str(ex)
            )

    await _emit_tier_op(
        tier=4,
        outcome=(
            "applied" if outcome.degraded_plan is not None else "noop"
        ),
        reason=outcome.meta.get(
            "tier4_reason", outcome.meta.get("tier4_skipped_reason", "")
        ),
        from_state="undegraded",
        to_state=(
            f"dropped:{outcome.dropped_scope_entry}"
            if outcome.dropped_scope_entry
            else None
        ),
    )
    await _emit_tier_op(
        tier=5,
        outcome=(
            "applied" if outcome.escalated_model is not None else "noop"
        ),
        reason=outcome.meta.get("tier5_reason", ""),
        from_state=current_model,
        to_state=outcome.escalated_model,
    )
    await _emit_tier_op(
        tier=6,
        outcome="applied",
        reason="recovery_hint_emitted",
        from_state=None,
        to_state=outcome.meta.get("recovery_hint_class", ""),
    )
    await _emit_tier_op(
        tier=7,
        outcome="applied",
        reason="forensic_summary_built",
        from_state=None,
        to_state=f"dumps:{len(archived_dumps)}",
    )

    logger.warning(
        "plan_phase.recovery_tiers_engaged",
        recovery_hint_class=outcome.meta.get("recovery_hint_class", ""),
        recovery_hint_action=outcome.meta.get("recovery_hint_action", ""),
        archived_dumps=list(archived_dumps),
        forensic_summary=outcome.forensic_summary,
        tier4_did_degrade=outcome.degraded_plan is not None,
        tier5_escalated_model=outcome.escalated_model,
    )

    # Tier 4 — try the degraded plan first (no extra architect call
    # required, just re-validate).
    if outcome.degraded_plan is not None:
        try:
            validated = await _validate_with_persistent_drop(
                orch,
                outcome.degraded_plan,
                cwd,
                # Fresh counters: the degraded plan is its own pass.
                errors_seen={},
                dropped_entries=list(dropped_entries),
                attempt=0,
            )
            logger.info(
                "plan_phase.recovery_tier4_succeeded",
                dropped_scope_entry=outcome.dropped_scope_entry,
            )
            return validated
        except (
            PlanParseError,
            _PYD_VAL_ERROR,
            PathValidationError,
        ) as exc:  # noqa: BLE001
            logger.warning(
                "plan_phase.recovery_tier4_failed", err=str(exc)
            )
            # Fall through to Tier 5.

    # Tier 5 — re-prompt the architect under the bumped model. This
    # consumes one extra dispatch beyond the standard retry budget.
    if outcome.escalated_model is not None:
        try:
            env = _build_retry_env(
                architect_env,
                prior_plan_md=plan_md,
                exc=last_exc,
                errors_seen=errors_seen,
                dropped_entries=dropped_entries,
            )
            # Apply the model override via a temporary registry shim:
            # the registry is a plain dict so we mutate-and-restore
            # to keep the override scoped to this single call.
            original_spec = architect_spec
            if original_spec is not None:
                bumped_spec = original_spec.model_copy(
                    update={"model": outcome.escalated_model}
                )
                orch.registry["architect"] = bumped_spec
            try:
                bumped_result = await _delegate(
                    orch,
                    "architect",
                    env,
                    max_turns_override=retry_max,
                    system_prompt_substitutions={
                        "rejection_history": "",
                    },
                )
            finally:
                if original_spec is not None:
                    orch.registry["architect"] = original_spec
            bumped_md = _try_read_plan_from_file(cwd, bumped_result.text)
            bumped_plan = parse_plan_markdown(bumped_md, spec_hash=spec_hash)
            validated = await _validate_with_persistent_drop(
                orch,
                bumped_plan,
                cwd,
                errors_seen=dict(errors_seen),
                dropped_entries=list(dropped_entries),
                attempt=attempts,
            )
            logger.info(
                "plan_phase.recovery_tier5_succeeded",
                escalated_model=outcome.escalated_model,
            )
            return validated
        except (
            PlanParseError,
            _PYD_VAL_ERROR,
            PathValidationError,
            Exception,
        ) as exc:  # noqa: BLE001
            logger.warning(
                "plan_phase.recovery_tier5_failed", err=str(exc)
            )
            # Fall through to Tier 6/7 (caller hard-fails).

    # v0.42.1 F1b (ADR-0047): all recovery tiers are exhausted — this is the
    # Tier-7 hard-fail. Route the degrade through the resolver as an EXPLICIT,
    # auditable breadcrumb before the caller re-raises (observability only; the
    # hard-fail still propagates). Lives behind the recovery module's thin async
    # wrapper so the enforced ``record_phase_degrade`` setter stays the path.
    try:
        from orchestrator.plan_phase_recovery import (  # noqa: PLC0415
            record_phase_degrade as _record_recovery_degrade,
        )

        await _record_recovery_degrade(orch, last_exc)
    except Exception:  # noqa: BLE001 - never mask the hard-fail
        pass

    # Tier 6 already fired (the recovery_hint was logged above); the
    # caller (the architect-retry loop) hard-fails the plan-phase by
    # re-raising. We attach the forensic summary onto the last
    # exception so the operator sees both the original failure and
    # the recovery context.
    if last_exc is not None:
        # Augment the exception's args with the forensic summary so
        # the eventual ``raise`` carries it forward.
        try:
            last_exc.args = (
                f"{last_exc.args[0] if last_exc.args else ''}\n\n"
                f"{outcome.forensic_summary}",
            )
        except Exception:  # noqa: BLE001
            pass
        # v0.32.0 (Phase 5, Gap G): stash the structured recovery hint
        # on the exception so a future upstream catcher can render the
        # actionable surface (e.g. an autodev plan top-level handler).
        try:
            setattr(last_exc, "recovery_hint", outcome.recovery_hint)
        except Exception:  # noqa: BLE001 - never let attribute pinning mask the raise
            pass
    return None


# Module-level alias for the pydantic ValidationError to avoid
# re-importing it inside _run_phase1_4_recovery (the import lives
# inside run_plan_phase). Resolved lazily on first access.
def _resolve_pyd_validation_error() -> type[BaseException]:
    from pydantic import ValidationError as _PV  # noqa: PLC0415

    return _PV


_PYD_VAL_ERROR: type[BaseException] = _resolve_pyd_validation_error()


async def _delegate(
    orch: "Orchestrator",
    role: str,
    envelope: DelegationEnvelope,
    *,
    max_turns_override: int | None = None,
    system_prompt_substitutions: dict[str, str] | None = None,
) -> AgentResult:
    """Build an :class:`AgentInvocation` from the envelope + registry and call adapter.

    Guardrail hooks are called around the adapter execution:
    - ``pre_invocation`` before the adapter call (may raise GuardrailExceededError)
    - ``post_invocation`` after the adapter call (may raise GuardrailExceededError)
    - ``loop_detector.observe`` after post_invocation

    v0.26.0: InlineAdapter's suspend/resume special-cases (response-file
    shortcut on the resume path, ``write_suspend_state`` on the
    ``DelegationPendingSignal`` exit path) were removed. Every adapter
    is now a subprocess adapter.

    v0.32.0 Phase 1.1: ``system_prompt_substitutions`` lets the
    architect-retry loop interpolate ``{rejection_history}`` (and any
    future placeholders) into the role's system prompt at call time.
    Each ``{key}`` in ``spec.prompt`` is replaced with its mapped
    value; missing placeholders are replaced with the empty string so
    a never-substituted ``{rejection_history}`` doesn't leak into the
    model's context as a literal token.
    """
    spec = orch.registry.get(role)
    if spec is None:
        raise AutodevError(f"role {role!r} not in registry")
    system_prompt = spec.prompt.strip()
    # v0.32.0 Phase 1.1: interpolate ``{key}`` placeholders. Done with
    # str.replace rather than str.format so prompt bodies that
    # legitimately contain unrelated curly braces (JSON examples, code
    # snippets, format specs) don't trip ``KeyError``.
    substitutions = dict(system_prompt_substitutions or {})
    # Always strip the ``{rejection_history}`` placeholder when no
    # substitution was provided — first-attempt architect calls leave
    # the spot blank rather than emitting the literal placeholder.
    substitutions.setdefault("rejection_history", "")
    for key, value in substitutions.items():
        system_prompt = system_prompt.replace("{" + key + "}", value)
    parts: list[str] = [system_prompt]
    block = envelope.render_as_task_message()
    parts.append("\n\n---\n")
    parts.append(block)
    # v0.35.0 C1 prerequisite: prefer the IDs-returning variant so the
    # success path can credit the entries that contributed. Fall back
    # to the legacy str-returning ``inject_block`` for test fakes that
    # haven't been updated to expose the sister method.
    inject_with_ids = getattr(orch.knowledge, "inject_block_with_ids", None)
    if inject_with_ids is not None:
        lessons, injected_ids = await inject_with_ids(
            role, task_id=envelope.task_id
        )
    else:
        lessons = await orch.knowledge.inject_block(role, task_id=envelope.task_id)
        injected_ids = []
    if lessons:
        parts.append("\n\n")
        parts.append(lessons)
    # v0.35.0 C1 prerequisite: record the per-(task, role) correlation
    # so plan-phase tasks that later complete successfully also credit
    # the lessons that contributed to their plan-time prompt. Defensive
    # access so orchestrator stubs in unit tests are not forced to
    # carry the slot.
    if injected_ids and envelope.task_id:
        correlation = getattr(orch, "_injected_lessons_by_task", None)
        if correlation is not None:
            corr_key = (envelope.task_id, role)
            existing = correlation.get(corr_key, [])
            correlation[corr_key] = existing + [
                i for i in injected_ids if i not in existing
            ]

    # Resolve per-role effort: explicit override > architect floor > matrix > None.
    # In plan phase the plan doesn't exist yet for the architect call (and on
    # the architect retry path); ``resolve_role_effort`` returns the architect
    # floor based on ``user_complexity`` regardless.
    agent_cfg = orch.cfg.agents.get(role)
    plan_complexity: str | None = None
    if orch.plan_manager is not None:
        try:
            existing_plan = await orch.plan_manager.load()
        except Exception:  # noqa: BLE001
            existing_plan = None
        if existing_plan is not None:
            plan_complexity = existing_plan.complexity
    effort = resolve_role_effort(
        role, agent_cfg, plan_complexity, orch.cfg.user_complexity
    )

    # v0.23.0 C5: explorer max_turns 2x bump on huge repos. The explorer's
    # job is to enumerate the codebase before the architect plans; on huge
    # repos (Unity: 358K files) the default 3 turns is insufficient — the
    # 2026-05-09 run hit ``error_max_turns`` at turn 3 with 218K cached
    # tokens still in flight. Other roles don't benefit from the bump
    # (their work isn't exploratory) so this targets explorer only.
    resolved_max_turns = max_turns_override or spec.max_turns or 1
    if (
        role == "explorer"
        and max_turns_override is None
        and getattr(getattr(orch, "_repo_capacity", None), "is_huge", False)
    ):
        resolved_max_turns = int(round(resolved_max_turns * 2.0))

    inv = AgentInvocation(
        role=role,
        prompt="\n".join(parts),
        cwd=orch.cwd,
        model=spec.model,
        allowed_tools=list(spec.tools) if spec.tools else None,
        max_turns=resolved_max_turns,
        effort=effort,
    )

    orch.guardrails.pre_invocation(envelope.task_id, inv)
    result = await orch.adapter.execute(inv)
    orch.guardrails.post_invocation(envelope.task_id, result)
    if result.success and result.text:
        orch.loop_detector.observe(envelope.task_id, role, result.text)
    return result


__all__ = [
    "PlanParseError",
    "parse_plan_markdown",
    "run_plan_phase",
]
