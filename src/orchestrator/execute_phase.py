"""Execute-phase loop and conflict-escalation helpers.

For each pending task (or a specific task when ``task_id`` is given):

  1. Build a :class:`DelegationEnvelope` from the task.
  2. developer -> :class:`CoderEvidence`. Retry on adapter failure up to
     ``qa_retry_limit``; on exhaustion, escalate.
  3. test_engineer -> :class:`TestEvidence`. Any failure retries test_engineer.
  4. auto-gates (syntax/lint/build/run_tests/secretscan). ``TODO(phase-8)``:
     we pretend gates always pass and advance to ``auto_gated``.
  5. reviewer -> :class:`ReviewEvidence`. NEEDS_CHANGES / REJECTED counts
     as a retry back to developer with the issue list injected as context.
  6. ``TODO(phase-7)``: :class:`ImplementationTournament`. Phase 4: skip.
  7. Mark task complete.

On retry exhaustion, call ``critic_sounding_board`` once, flag the task as
escalated, mark it blocked, and stop the loop.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from adapters.git_utils import _git_rev_parse_head, extract_files_from_diff
from adapters.inline import InlineAdapter
from adapters.inline_types import DelegationPendingSignal
from adapters.types import AgentInvocation, AgentResult
from errors import AutodevError, GuardrailExceededError
from autologging import get_logger
from orchestrator.delegation_envelope import DelegationEnvelope
from orchestrator.inline_state import write_suspend_state
from orchestrator.worktree import WorktreeError, WorktreeManager
from state.evidence import write_evidence, write_patch
from qa import (
    GateResult,
    detect_language,
    run_build_check,
    run_lint,
    run_hallucination_guard,
    run_secretscan,
    run_syntax_check,
    run_tests,
)
from state.paths import autodev_root
from state.schemas import (
    CoderEvidence,
    CriticEvidence,
    Phase,
    ReviewEvidence,
    Task,
    TestEvidence,
)
from tournament.effort import resolve_role_effort
from tournament.task_overrides import (
    resolve_task_max_turns,
    resolve_task_timeout_s,
)


if TYPE_CHECKING:
    from orchestrator import Orchestrator


logger = get_logger(__name__)


# Default subprocess timeout for the developer adapter when ``Task.complexity``
# is ``None`` (legacy / pre-v0.8.0 plans). 900s = 15 minutes — picked to give
# untagged tasks comfortable runway without slipping into the 1800s reserved
# for the architect-tagged ``complex`` bucket. Reused by the per-task
# resolver's ``spec_default`` argument for symmetry; the resolver itself
# returns ``None`` and ``delegate`` falls back to this constant.
_DEFAULT_DEVELOPER_TIMEOUT_S = 900


# v0.9.0: terminal task statuses that count toward "phase done" for
# phase-review trigger detection. All three pause forward progress for
# the task in question; ``complete`` and ``skipped`` are intentional
# terminal states; ``blocked`` is escalation. The phase-review tournament
# fires once the LAST task in the phase reaches one of these.
_TERMINAL_TASK_STATUSES: tuple[str, ...] = ("complete", "blocked", "skipped")


class TaskEscalatedError(AutodevError):
    """Raised (and logged) when a task is escalated to critic_sounding_board.

    The execute loop catches this internally — it is surfaced to the CLI
    only when the user explicitly targets a task that ends up escalated.
    """


# v0.11.0: conflict-escalation helpers.

ConflictAction = Literal["rebase-and-retry", "abandon-task", "rewrite"]


@dataclass
class ConflictResolution:
    """Parsed result from ``critic_sounding_board`` in CONFLICT ESCALATION MODE.

    Attributes:
        action: One of ``rebase-and-retry`` / ``abandon-task`` /
            ``rewrite``. Defaults to ``abandon-task`` when the parser
            cannot extract a valid directive (defensive — the worker
            then blocks the task with a parser-fallback reason).
        rewrite_guidance: Free-form text from the critic intended to
            steer a re-invoked developer pass. Only meaningful when
            ``action == "rewrite"``. Empty for the other two branches.
    """

    action: ConflictAction = "abandon-task"
    rewrite_guidance: str = ""


# Regex matching the trailing ``RESOLUTION:`` directive on its own line.
# The directive may be followed by trailing whitespace / blank lines.
_CONFLICT_DIRECTIVE_RE = re.compile(
    r"^RESOLUTION:\s*(rebase-and-retry|abandon-task|rewrite)\s*$",
    re.MULTILINE,
)


def _parse_conflict_resolution(critic_response: str) -> ConflictResolution:
    """Extract a :class:`ConflictResolution` from the critic's text.

    Looks for a ``RESOLUTION: <action>`` line, anchored to its own line.
    On parse failure (no directive found, multiple conflicting
    directives, etc.) returns ``ConflictResolution(action="abandon-task")``
    so the caller can safely block the task.

    For ``rewrite``, captures everything BEFORE the matched directive
    line as ``rewrite_guidance`` (stripped of trailing whitespace). The
    guidance is what the orchestrator passes back to the developer
    when re-invoking.
    """
    if not critic_response:
        return ConflictResolution()

    matches = list(_CONFLICT_DIRECTIVE_RE.finditer(critic_response))
    if not matches:
        return ConflictResolution()
    # Take the LAST directive — the prompt instructs ending with one
    # directive, so the last match is the authoritative answer.
    last = matches[-1]
    action_raw = last.group(1)
    if action_raw not in ("rebase-and-retry", "abandon-task", "rewrite"):
        return ConflictResolution()
    action: ConflictAction = cast("ConflictAction", action_raw)

    guidance = ""
    if action == "rewrite":
        # Everything before the directive's start position is guidance.
        guidance = critic_response[: last.start()].strip()
    return ConflictResolution(action=action, rewrite_guidance=guidance)


async def _escalate_conflict_to_critic(
    orch: "Orchestrator",
    task: Task,
    worktree: Path,
    conflict_diff: str,
    already_applied_diff: str = "",
    conflict_files: list[str] | None = None,
) -> ConflictResolution:
    """Invoke ``critic_sounding_board`` in CONFLICT ESCALATION MODE.

    Builds a :class:`DelegationEnvelope` carrying a ``CONFLICT_CONTEXT:``
    block (failing task id, conflict file paths, both diffs) so the
    prompt's gated CONFLICT ESCALATION MODE section activates. Parses
    the response via :func:`_parse_conflict_resolution`.

    Returns a :class:`ConflictResolution`. The caller (typically the
    worker after ``apply_patch_to_main`` raises) branches on
    ``resolution.action``:

    * ``rebase-and-retry`` → retry apply with ``three_way=True``.
    * ``abandon-task`` → mark the task blocked.
    * ``rewrite`` → re-invoke the developer with
      ``resolution.rewrite_guidance`` injected as context.
    """
    files_block = "\n".join(
        f"  - {f}" for f in (conflict_files or [str(worktree)])
    )
    conflict_context = (
        "CONFLICT_CONTEXT:\n"
        f"failing_task_id: {task.id}\n"
        f"conflict_files:\n{files_block}\n"
        "already_applied_diff: |\n"
        f"{_indent_block(already_applied_diff)}\n"
        "attempted_diff: |\n"
        f"{_indent_block(conflict_diff)}\n"
    )

    env = DelegationEnvelope(
        task_id=task.id,
        target_agent="critic_sounding_board",
        action="critique",
        acceptance=(
            "End your response with exactly one RESOLUTION: directive "
            "(rebase-and-retry / abandon-task / rewrite)."
        ),
        context={
            "task_id": task.id,
            "conflict_context_marker": True,
        },
    )
    result = await delegate(
        orch,
        "critic_sounding_board",
        env,
        extra_context=conflict_context,
    )
    return _parse_conflict_resolution(result.text or "")


# v0.15.0: stuck-recovery escalation parser + helper. Mirrors the conflict
# escalation pattern above so the structural code shape is consistent
# across both gated critic-escalation modes.

StuckAction = Literal["refine", "pivot", "soft-blocker"]


@dataclass
class StuckResolution:
    """Parsed result from ``critic_sounding_board`` in STUCK RECOVERY MODE.

    Attributes:
        action: One of ``refine`` / ``pivot`` / ``soft-blocker``.
            Defaults to ``refine`` (the least-disruptive fallback —
            matches the documented prompt fallback).
        guidance: Free-form text from the critic intended for the next
            developer attempt (or, in the soft-blocker case, the
            description of what the human needs to decide). Empty when
            the parser could not extract guidance.
    """

    action: StuckAction = "refine"
    guidance: str = ""


# Regex matching the trailing ``RESOLUTION:`` directive on its own line.
# Mirrors :data:`_CONFLICT_DIRECTIVE_RE` shape.
_STUCK_DIRECTIVE_RE = re.compile(
    r"^RESOLUTION:\s*(refine|pivot|soft-blocker)\s*$",
    re.MULTILINE,
)


def _parse_stuck_resolution(critic_response: str) -> StuckResolution:
    """Extract a :class:`StuckResolution` from the critic's text.

    Looks for a ``RESOLUTION: <action>`` line, anchored to its own line.
    On parse failure (no directive found, multiple-but-final-is-invalid,
    etc.) returns ``StuckResolution(action="refine")`` so the caller
    safely falls through to the least-disruptive next step.

    Captures everything BEFORE the matched directive line as ``guidance``
    (stripped of trailing whitespace). The guidance is what the
    orchestrator passes back to the developer when re-invoking (refine /
    pivot) or surfaces in the blocked_reason text (soft-blocker).
    """
    if not critic_response:
        return StuckResolution()

    matches = list(_STUCK_DIRECTIVE_RE.finditer(critic_response))
    if not matches:
        return StuckResolution()
    last = matches[-1]
    action_raw = last.group(1)
    if action_raw not in ("refine", "pivot", "soft-blocker"):
        return StuckResolution()
    action: StuckAction = cast("StuckAction", action_raw)

    guidance = critic_response[: last.start()].strip()
    return StuckResolution(action=action, guidance=guidance)


async def _escalate_stuck_to_critic(
    orch: "Orchestrator",
    task: Task,
    *,
    stuck_state: object,
    ladder_step: str,
    recent_evidence: str = "",
    prior_attempts: list[str] | None = None,
) -> StuckResolution:
    """Invoke ``critic_sounding_board`` in STUCK RECOVERY MODE.

    Builds a :class:`DelegationEnvelope` carrying a ``STUCK_CONTEXT:``
    block (failing task id, current discard / pivot counts, ladder
    step, prior attempts, freshest evidence) so the prompt's gated
    STUCK RECOVERY MODE section activates. Parses the response via
    :func:`_parse_stuck_resolution`.

    Args:
        orch: Orchestrator instance.
        task: Task whose retries have stalled.
        stuck_state: :class:`orchestrator.escalation_ladder.StuckState`
            for the failing task. Typed ``object`` here to avoid a
            cycle with the ladder module at import time.
        ladder_step: One of ``"REFINE" | "PIVOT" | "SOFT_BLOCKER"`` —
            the ladder's recommendation. Surfaced in the prompt as a
            hint to the critic about which response mode to take.
        recent_evidence: Freshest excerpt of failing-test / reviewer /
            error output. Empty string is allowed.
        prior_attempts: Optional list of one-line summaries of the
            most recent attempts.

    Returns a :class:`StuckResolution`. The caller (typically
    :func:`_try_retry_or_escalate`) branches on ``resolution.action``:

    * ``refine`` → re-invoke the developer with ``resolution.guidance``
      injected as context.
    * ``pivot`` → re-invoke the developer with the radical-direction
      guidance as context (and the orchestrator increments
      ``pivot_count``).
    * ``soft-blocker`` → mark the task blocked with the guidance text
      as the ``blocked_reason``; emit a ``soft_blocker_handoff``
      ledger op for forensics.
    """
    # Indented YAML-ish prior_attempts block; empty when no attempts.
    if prior_attempts:
        attempts_block = "\n".join(f"  - {a}" for a in prior_attempts)
    else:
        attempts_block = "  - (no prior attempts recorded)"

    # ``stuck_state`` is typed ``object`` to avoid a cyclic import at
    # module-load time. We accept anything with the attributes we need.
    discard_count = int(getattr(stuck_state, "discard_count", 0))
    pivot_count = int(getattr(stuck_state, "pivot_count", 0))
    last_event = str(getattr(stuck_state, "last_event", "") or "")

    stuck_context = (
        "STUCK_CONTEXT:\n"
        f"failing_task_id: {task.id}\n"
        f"discard_count: {discard_count}\n"
        f"pivot_count: {pivot_count}\n"
        f"last_event: {last_event}\n"
        f"ladder_step: {ladder_step}\n"
        f"prior_attempts:\n{attempts_block}\n"
        "recent_evidence: |\n"
        f"{_indent_block(recent_evidence)}\n"
    )

    env = DelegationEnvelope(
        task_id=task.id,
        target_agent="critic_sounding_board",
        action="critique",
        acceptance=(
            "End your response with exactly one RESOLUTION: directive "
            "(refine / pivot / soft-blocker)."
        ),
        context={
            "task_id": task.id,
            "stuck_context_marker": True,
            "ladder_step": ladder_step,
        },
    )
    result = await delegate(
        orch,
        "critic_sounding_board",
        env,
        extra_context=stuck_context,
    )
    return _parse_stuck_resolution(result.text or "")


def _indent_block(text: str, prefix: str = "  ") -> str:
    """Indent every line of ``text`` by ``prefix`` for the YAML-ish block.

    Returns the empty string verbatim so the CONFLICT_CONTEXT: block is
    well-formed even when one of the diffs is missing.
    """
    if not text:
        return ""
    return "\n".join(prefix + line for line in text.splitlines())


# Maximum number of "rewrite" rounds before forcing abandon. Caps the
# critic-developer ping-pong so a misbehaving critic cannot loop
# indefinitely.
_CONFLICT_REWRITE_RETRY_CAP = 2


async def _apply_with_conflict_escalation(
    orch: "Orchestrator",
    task: Task,
    worktree: Path,
    worktree_mgr: WorktreeManager,
) -> bool:
    """Apply the worktree diff to main with critic-escalation on conflict.

    Returns ``True`` if the diff was applied (cleanly OR via 3-way
    merge or after a rewrite round), ``False`` if the task was
    abandoned. On ``False`` the caller transitions the task to
    ``blocked`` (caller-side update_task_status, this helper records
    the blocked reason via the worker contract).

    Branches on the critic's RESOLUTION directive:

    * ``rebase-and-retry`` → re-attempt apply with ``three_way=True``.
      If the 3-way also fails, escalate again (capped by retry cap).
    * ``abandon-task`` → block + cascade-block descendants, return False.
    * ``rewrite`` → re-invoke the developer with the merge guidance
      injected as ``extra_context`` and retry the apply.

    The cap of two rewrite rounds prevents critic-developer ping-pong
    on pathological cases.
    """
    rewrite_rounds = 0
    while True:
        try:
            await worktree_mgr.apply_patch_to_main(worktree, base_ref="HEAD")
            return True
        except WorktreeError as exc:
            logger.warning(
                "execute_phase.apply_patch_conflict",
                task_id=task.id,
                err=str(exc),
            )

            try:
                conflict_diff = await worktree_mgr.get_diff_vs_base(worktree)
            except WorktreeError:
                conflict_diff = ""
            resolution = await _escalate_conflict_to_critic(
                orch,
                task,
                worktree,
                conflict_diff=conflict_diff,
                already_applied_diff="",
                conflict_files=list(task.files),
            )

            if resolution.action == "rebase-and-retry":
                # Try 3-way apply. If THIS also fails, fall through to
                # abandon (no infinite loop on persistent conflicts).
                try:
                    await worktree_mgr.apply_patch_to_main(
                        worktree, base_ref="HEAD", three_way=True
                    )
                    logger.info(
                        "execute_phase.conflict_resolved_3way",
                        task_id=task.id,
                    )
                    return True
                except WorktreeError as exc2:
                    logger.warning(
                        "execute_phase.conflict_3way_failed",
                        task_id=task.id,
                        err=str(exc2),
                    )
                    await orch.plan_manager.update_task_status(
                        task.id,
                        "blocked",
                        meta={
                            "blocked_reason": f"conflict_escalation:3way_failed: {exc2}"
                        },
                    )
                    return False

            if resolution.action == "abandon-task":
                await orch.plan_manager.update_task_status(
                    task.id,
                    "blocked",
                    meta={
                        "blocked_reason": "conflict_escalation:abandon"
                    },
                )
                return False

            # rewrite: re-invoke developer with guidance, capped retries.
            if rewrite_rounds >= _CONFLICT_REWRITE_RETRY_CAP:
                logger.warning(
                    "execute_phase.conflict_rewrite_cap_exceeded",
                    task_id=task.id,
                    cap=_CONFLICT_REWRITE_RETRY_CAP,
                )
                await orch.plan_manager.update_task_status(
                    task.id,
                    "blocked",
                    meta={
                        "blocked_reason": "conflict_escalation:rewrite_cap_exceeded"
                    },
                )
                return False

            rewrite_rounds += 1
            developer_env = _developer_envelope(
                task, extra_issues=[resolution.rewrite_guidance]
            )
            try:
                await delegate(
                    orch,
                    "developer",
                    developer_env,
                    extra_context=resolution.rewrite_guidance,
                    retry_count=task.retry_count,
                    last_issues=[resolution.rewrite_guidance],
                    task=task,
                    cwd_override=worktree,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.conflict_rewrite_developer_failed",
                    task_id=task.id,
                    err=str(exc),
                )
                await orch.plan_manager.update_task_status(
                    task.id,
                    "blocked",
                    meta={
                        "blocked_reason": f"conflict_escalation:rewrite_developer_failed: {exc}"
                    },
                )
                return False
            # Loop and retry apply with the developer's new diff.


async def run_execute_phase(
    orch: "Orchestrator", task_id: str | None = None
) -> list[Task]:
    """Run the execute loop. Returns the list of tasks processed (in order).

    v0.11.0: replaces the serial ``next_pending_task`` walk with a
    DAG-aware worker pool. Within a phase, independent tasks execute
    concurrently up to ``cfg.tournaments.execute_max_parallel_tasks``
    (auto-resolved via the v0.10.0 resource probe when ``None``). Tasks
    with overlapping ``files`` are serialized; failed tasks
    cascade-block their dependents via
    :meth:`PlanManager.mark_blocked_descendants`.

    v0.9.0: when the loop processes the last task in a phase (all tasks
    terminal), the per-phase code-review tournament fires. The tournament
    decides whether to accept the phase as-is (winner=A) or inject
    corrective sub-tasks (winner=B/AB) — corrective tasks land at the end
    of the same phase and execute BEFORE the next phase begins.

    Single-task path (``task_id`` set) bypasses the DAG dispatcher and
    runs the legacy serial path, with a NEW per-task worktree if the
    repo is git-initialized (otherwise falls back to running in
    ``orch.cwd`` directly).
    """
    processed: list[Task] = []

    if task_id is not None:
        task = await orch.plan_manager.get_task(task_id)
        if task is None:
            raise AutodevError(f"task_id={task_id!r} not found in plan")
        if task.status in ("complete", "skipped"):
            logger.info("execute_phase.skip", task_id=task_id, status=task.status)
            return processed
        # Single-task path: still record baseline_commit on first entry to
        # the phase so a later (whole-loop) run picks up the right
        # baseline. This is idempotent (the meta-update only runs when
        # baseline_commit is unset).
        await _maybe_record_phase_entry(orch, task.phase_id)
        processed.append(await _execute_one(orch, task))
        # Single-task path also triggers review when the targeted task
        # was the last terminal one in its phase.
        await _maybe_run_phase_review(orch, task.phase_id)
        return processed

    # v0.11.0: DAG-aware worker pool over all phases.
    plan = await orch.plan_manager.load()
    if plan is None:
        return processed

    # Resolve worker-pool cap once per run.
    parallelism = _resolve_execute_parallelism(orch)

    # Build a worktree manager rooted under the autodev root. Skip
    # worktree-isolation when the repo is not git-initialized — tests
    # commonly use bare tmp dirs and the legacy serial path applies.
    worktree_mgr: WorktreeManager | None = None
    if _is_git_repo(orch.cwd):
        worktree_mgr = WorktreeManager(
            main_repo=orch.cwd,
            tournament_dir=autodev_root(orch.cwd) / "execute_worktrees",
        )

    try:
        # v0.14.0: validate edit_scope first — a plan that declares a
        # narrowed scope but lists out-of-scope task files is a fail-fast
        # condition. We block every pending task in offending phases (so
        # the run terminates cleanly) and emit a structured warning that
        # includes the offending task id, file, and resolved scope. When
        # ``Plan.edit_scope`` is empty (legacy / no scope declared), the
        # validator is a no-op and the loop proceeds unchanged.
        from orchestrator.dag import (
            DagValidationError,
            EditScopeViolation,
            validate_edit_scope,
            validate_phase_dag,
        )

        try:
            # v0.17.0 S5: pass the orchestrator's tracked-files cache so
            # glob entries in ``Task.files`` are expanded before scope
            # validation. Empty set / missing cache preserves legacy
            # literal-string behavior. ``getattr`` tolerates legacy
            # OrchStub fixtures that pre-date the cache.
            validate_edit_scope(
                plan, tracked_files=getattr(orch, "tracked_files", None)
            )
        except EditScopeViolation as exc:
            logger.warning(
                "execute_phase.edit_scope_violation",
                err=str(exc),
            )
            # Block every pending task in every phase: an edit_scope
            # violation typically points at a structural plan error
            # (architect declared too-narrow scope, or a task slipped
            # in with the wrong files), and partial execution past the
            # violation is unsafe.
            for phase in plan.phases:
                for t in phase.tasks:
                    if t.status == "pending":
                        try:
                            await orch.plan_manager.update_task_status(
                                t.id,
                                "blocked",
                                meta={
                                    "blocked_reason": f"edit_scope_violation: {exc}"
                                },
                            )
                        except Exception:  # noqa: BLE001
                            pass
            return processed

        for phase in plan.phases:
            try:
                validate_phase_dag(phase)
            except DagValidationError as exc:
                logger.warning(
                    "execute_phase.dag_invalid",
                    phase_id=phase.id,
                    err=str(exc),
                )
                # Block every pending task in the offending phase so the
                # run terminates cleanly.
                for t in phase.tasks:
                    if t.status == "pending":
                        try:
                            await orch.plan_manager.update_task_status(
                                t.id,
                                "blocked",
                                meta={
                                    "blocked_reason": f"dag_invalid: {exc}"
                                },
                            )
                        except Exception:  # noqa: BLE001
                            pass

        for phase in plan.phases:
            await _maybe_record_phase_entry(orch, phase.id)
            # Run the worker pool for this phase, then phase-review.
            # If phase-review injects corrective tasks (review_status
            # transitions to ``corrective_required``), loop again so
            # those tasks execute within this phase before advancing
            # to the next. Cap at a small number of iterations as a
            # defensive net against pathological architect_b output.
            for _round in range(3):
                phase_processed = await _execute_phase_dag(
                    orch, phase.id, worktree_mgr, parallelism
                )
                processed.extend(phase_processed)
                await _maybe_run_phase_review(orch, phase.id)
                # Did phase-review accept? If so we're done with this
                # phase. Otherwise (corrective_required) the next loop
                # iteration will run any newly-injected tasks.
                latest_plan = await orch.plan_manager.load()
                if latest_plan is None:
                    break
                latest_phase = next(
                    (p for p in latest_plan.phases if p.id == phase.id), None
                )
                if latest_phase is None:
                    break
                if latest_phase.review_status in ("accepted", "skipped"):
                    break
                # Any new pending tasks? If not, stop looping.
                if not any(t.status == "pending" for t in latest_phase.tasks):
                    break
    finally:
        if worktree_mgr is not None:
            try:
                await worktree_mgr.cleanup_all()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.cleanup_all_failed", err=str(exc)
                )
    return processed


def _resolve_execute_parallelism(orch: "Orchestrator") -> int:
    """Resolve the per-task worker pool cap via runtime.resource_probe.

    Forwards ``cfg.tournaments.execute_max_parallel_tasks`` (None =
    auto-resolve) into :func:`runtime.resource_probe.resolve_parallelism`
    with ``role_mix='execute'``. ``num_judges=16`` (the absolute
    ceiling) when no explicit cap is configured — the dispatcher polls
    greedily, so this just sets the upper bound.
    """
    from runtime.resource_probe import probe_host, resolve_parallelism

    configured = orch.cfg.tournaments.execute_max_parallel_tasks
    capacity = probe_host()
    return resolve_parallelism(
        configured=configured,
        capacity=capacity,
        role_mix="execute",
        num_judges=configured if configured is not None else 16,
    )


def _is_git_repo(cwd: Path) -> bool:
    """Return True iff ``cwd`` looks like a git repo (has a ``.git`` entry).

    Worktree-isolation requires git, so the dispatcher falls back to
    the legacy in-place execution path for non-git fixtures.
    """
    try:
        gp = cwd / ".git"
        return gp.exists()
    except OSError:
        return False


async def _all_phase_tasks_terminal_async(
    orch: "Orchestrator", phase_id: str
) -> bool:
    """Read the latest plan and return True iff every task in the phase
    is in a terminal state. Distinct from the synchronous helper
    :func:`_all_phase_tasks_terminal` which takes a Phase object."""
    plan = await orch.plan_manager.load()
    if plan is None:
        return False
    phase = next((p for p in plan.phases if p.id == phase_id), None)
    if phase is None or not phase.tasks:
        return False
    return _all_phase_tasks_terminal(phase)


async def _execute_phase_dag(
    orch: "Orchestrator",
    phase_id: str,
    worktree_mgr: WorktreeManager | None,
    parallelism: int,
) -> list[Task]:
    """Run the worker pool for one phase. Returns processed tasks.

    Polls :meth:`PlanManager.next_pending_tasks` for runnable tasks
    (deps met, no file overlap with in-flight) and spawns asyncio
    workers up to ``parallelism``. Awaits the first to complete via
    :func:`asyncio.wait` with ``FIRST_COMPLETED``, drains, and resumes
    polling. Failures route into ``mark_blocked_descendants`` to
    cascade-block dependents.
    """
    import asyncio

    in_flight: dict[str, asyncio.Task[Task]] = {}
    processed: list[Task] = []

    while True:
        # Termination: phase is fully terminal AND no workers running.
        if not in_flight and await _all_phase_tasks_terminal_async(orch, phase_id):
            return processed

        # Try to spawn new workers up to the cap.
        if len(in_flight) < parallelism:
            slots = parallelism - len(in_flight)
            excluded = await orch.plan_manager.in_flight_files()
            tasks_to_run = await orch.plan_manager.next_pending_tasks(
                limit=slots, exclude_files=excluded
            )
            # Filter to tasks that belong to THIS phase (others wait
            # for their phase's turn — phase-major scheduling).
            tasks_to_run = [t for t in tasks_to_run if t.phase_id == phase_id]
            for t in tasks_to_run:
                if t.id in in_flight:
                    continue
                await orch.plan_manager.mark_in_flight(t.id)
                in_flight[t.id] = asyncio.create_task(
                    _execute_one_worker(orch, t, worktree_mgr)
                )

        if not in_flight:
            # Nothing to spawn AND not all terminal — likely waiting on
            # an in-flight task in a different phase; brief sleep then
            # re-poll. Bounded short delay keeps unit tests snappy.
            # If the ENTIRE phase is in_progress (no pending, no
            # in_flight in our set), another runner / external mutation
            # is in play — break out so we don't spin forever.
            if await _all_phase_tasks_terminal_async(orch, phase_id):
                return processed
            # Defensive: also break if the phase has ZERO pending tasks
            # at this point — there's nothing to wait for.
            phase_has_pending = False
            plan = await orch.plan_manager.load()
            if plan is not None:
                phase_obj = next(
                    (p for p in plan.phases if p.id == phase_id), None
                )
                if phase_obj is not None:
                    phase_has_pending = any(
                        t.status == "pending" for t in phase_obj.tasks
                    )
            if not phase_has_pending:
                return processed
            await asyncio.sleep(0.05)
            continue

        # Wait for any worker to finish.
        done, _pending = await asyncio.wait(
            list(in_flight.values()), return_when=asyncio.FIRST_COMPLETED
        )
        for d in done:
            # Reverse-lookup the task id from the asyncio.Task instance.
            finished_id = next(
                (tid for tid, h in in_flight.items() if h is d), None
            )
            if finished_id is None:
                continue
            del in_flight[finished_id]
            await orch.plan_manager.clear_in_flight(finished_id)
            try:
                completed_task = d.result()
                processed.append(completed_task)
            except DelegationPendingSignal:
                # Inline-adapter suspend: propagate to the caller so
                # ``write_suspend_state`` runs and the CLI exits cleanly.
                # The plan_manager already cleared in_flight for this id.
                raise
            except Exception as exc:  # noqa: BLE001
                # Worker raised — cascade-block descendants. The worker
                # itself should have caught and routed to update_task_status,
                # so this is a defensive net.
                logger.warning(
                    "execute_phase.worker_unhandled_exception",
                    task_id=finished_id,
                    err=str(exc),
                )
                try:
                    await orch.plan_manager.mark_blocked_descendants(
                        phase_id, finished_id, str(exc)
                    )
                except Exception as exc2:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.mark_blocked_descendants_failed",
                        phase_id=phase_id,
                        task_id=finished_id,
                        err=str(exc2),
                    )


async def _execute_one_worker(
    orch: "Orchestrator",
    task: Task,
    worktree_mgr: WorktreeManager | None,
) -> Task:
    """Worker entry: run :func:`_execute_one` and route all exceptions.

    Workers must NEVER raise plan-fatal exceptions back to the
    dispatcher — instead they catch any exception, transition the task
    to ``blocked`` with a structured reason, and call
    ``mark_blocked_descendants`` so dependents are cascade-blocked.
    The dispatcher reads the worker's return value (the final Task)
    and only sees "done, status was X".

    EXCEPTION: :class:`DelegationPendingSignal` (raised by the inline
    adapter to suspend the run pending external response) MUST
    propagate to the dispatcher so the caller can persist suspend
    state. We re-raise it untouched.
    """
    try:
        return await _execute_one(orch, task, worktree_mgr)
    except DelegationPendingSignal:
        # Inline-adapter suspend signal — let the dispatcher / caller
        # handle it. The plan_manager already recorded the task as
        # in_progress; resume picks up where we left off.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.worker_caught_exception",
            task_id=task.id,
            err=str(exc),
        )
        try:
            blocked = await orch.plan_manager.update_task_status(
                task.id,
                "blocked",
                meta={"blocked_reason": f"worker_exception: {exc}"},
            )
        except Exception as exc2:  # noqa: BLE001
            logger.warning(
                "execute_phase.worker_block_failed",
                task_id=task.id,
                err=str(exc2),
            )
            return task
        try:
            await orch.plan_manager.mark_blocked_descendants(
                task.phase_id, task.id, str(exc)
            )
        except Exception as exc2:  # noqa: BLE001
            logger.warning(
                "execute_phase.cascade_block_failed",
                task_id=task.id,
                err=str(exc2),
            )
        return blocked


async def _maybe_record_phase_entry(orch: "Orchestrator", phase_id: str) -> None:
    """Record ``Phase.baseline_commit`` once at first entry to the phase.

    Idempotent: if the phase already has a ``baseline_commit``, this is a
    no-op. Failure to read HEAD (no git repo, etc.) is logged and skipped
    rather than raising — phase review will degrade gracefully to an
    empty-diff bundle.
    """
    plan = await orch.plan_manager.load()
    if plan is None:
        return
    phase = next((p for p in plan.phases if p.id == phase_id), None)
    if phase is None or phase.baseline_commit is not None:
        return
    sha = _git_rev_parse_head(orch.cwd)
    if sha is None:
        logger.info(
            "execute_phase.baseline_commit_unavailable", phase_id=phase_id
        )
        return
    try:
        await orch.plan_manager.update_phase_meta(phase_id, baseline_commit=sha)
    except Exception as exc:  # noqa: BLE001 — never let phase metadata
        logger.warning(
            "execute_phase.update_phase_meta_failed",
            phase_id=phase_id,
            err=str(exc),
        )


def _all_phase_tasks_terminal(phase: Phase) -> bool:
    """Return ``True`` iff every task in the phase is in a terminal state.

    Terminal = complete | blocked | skipped. The phase-review tournament
    fires only when ALL tasks are terminal — partial progress doesn't
    trigger review.
    """
    if not phase.tasks:
        return False
    return all(t.status in _TERMINAL_TASK_STATUSES for t in phase.tasks)


async def _maybe_run_phase_review(
    orch: "Orchestrator", phase_id: str
) -> None:
    """Trigger the phase-review tournament if the phase is fully terminal.

    Critical loop guard: phases with ``review_status`` already in
    ``{"accepted", "corrective_required", "skipped"}`` are skipped. This
    prevents corrective tasks (which themselves land terminal) from
    re-firing the tournament. Once corrective tasks reach terminal, this
    function transitions ``"corrective_required"`` directly to
    ``"accepted"`` without a second tournament.
    """
    if not orch.cfg.tournaments.phase_review.enabled:
        return

    # v0.11.0 race guard: if any worker for this phase is still in
    # flight, defer firing. The dispatcher will call this again after
    # each worker drains. Without this guard, two workers completing
    # simultaneously can both observe "all terminal" and double-fire.
    if (await orch.plan_manager.phase_in_flight_count(phase_id)) > 0:
        return

    plan = await orch.plan_manager.load()
    if plan is None:
        return
    phase = next((p for p in plan.phases if p.id == phase_id), None)
    if phase is None:
        return

    # Critical loop guard: reviewed phases are not re-reviewed.
    if phase.review_status == "accepted":
        return
    if phase.review_status == "skipped":
        return
    if phase.review_status == "corrective_required":
        # Corrective tasks have landed terminal — accept and move on.
        if _all_phase_tasks_terminal(phase):
            try:
                await orch.plan_manager.update_phase_meta(
                    phase_id, review_status="accepted"
                )
                logger.info(
                    "execute_phase.corrective_completed_accepted",
                    phase_id=phase_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.corrective_accept_failed",
                    phase_id=phase_id,
                    err=str(exc),
                )
        return

    # Only fire on full-terminal observation.
    if not _all_phase_tasks_terminal(phase):
        return

    await _run_phase_review(orch, phase)


async def _run_phase_review(orch: "Orchestrator", phase: Phase) -> None:
    """Run the phase-review tournament and apply its outcome.

    A-winner → ``review_status="accepted"``.
    B/AB-winner → parse direction → append corrective tasks. The tasks land
    at the end of the phase, where ``next_pending_task()`` will pick them
    up before advancing to the next phase.
    Exception → ``review_status="skipped"``, log warning, do not block
    forward progress.
    """
    from orchestrator.corrective_parser import parse_corrective_direction
    from orchestrator.phase_review_runner import (
        _phase_complexity_rollup,
        run_phase_review_tournament,
    )

    # Mark in-progress so a concurrent observer can see we're working.
    try:
        await orch.plan_manager.update_phase_meta(
            phase.id, review_status="in_progress"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.review_status_in_progress_failed",
            phase_id=phase.id,
            err=str(exc),
        )

    baseline_commit = phase.baseline_commit or ""
    tip_commit = _git_rev_parse_head(orch.cwd) or ""
    spec_md = ""
    spec_path = autodev_root(orch.cwd) / "spec.md"
    if spec_path.exists():
        try:
            spec_md = spec_path.read_text(encoding="utf-8")
        except OSError:
            spec_md = ""

    try:
        outcome = await run_phase_review_tournament(
            orch, phase, baseline_commit, tip_commit, spec_md
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.phase_review_error",
            phase_id=phase.id,
            err=str(exc),
        )
        try:
            await orch.plan_manager.update_phase_meta(
                phase.id, review_status="skipped"
            )
        except Exception as exc2:  # noqa: BLE001
            logger.warning(
                "execute_phase.review_status_skipped_failed",
                phase_id=phase.id,
                err=str(exc2),
            )
        return

    if outcome.accept_phase:
        try:
            await orch.plan_manager.update_phase_meta(
                phase.id, review_status="accepted"
            )
            logger.info(
                "execute_phase.phase_review_accepted",
                phase_id=phase.id,
                winner=outcome.winner,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.review_status_accepted_failed",
                phase_id=phase.id,
                err=str(exc),
            )
        return

    # Non-A winner: parse the direction and inject corrective sub-tasks.
    if not outcome.corrective_direction:
        logger.warning(
            "execute_phase.phase_review_no_direction",
            phase_id=phase.id,
            winner=outcome.winner,
        )
        try:
            await orch.plan_manager.update_phase_meta(
                phase.id, review_status="skipped"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.review_status_skipped_failed",
                phase_id=phase.id,
                err=str(exc),
            )
        return

    rollup = _phase_complexity_rollup(phase)
    corrective_tasks = parse_corrective_direction(
        outcome.corrective_direction,
        phase_id=phase.id,
        base_task_count=len(phase.tasks),
        phase_complexity=rollup,
    )
    if not corrective_tasks:
        logger.warning(
            "execute_phase.phase_review_no_corrective_parsed",
            phase_id=phase.id,
        )
        try:
            await orch.plan_manager.update_phase_meta(
                phase.id, review_status="skipped"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.review_status_skipped_failed",
                phase_id=phase.id,
                err=str(exc),
            )
        return

    try:
        await orch.plan_manager.append_corrective_tasks(
            phase.id, corrective_tasks
        )
        logger.info(
            "execute_phase.phase_review_corrective_injected",
            phase_id=phase.id,
            winner=outcome.winner,
            count=len(corrective_tasks),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.append_corrective_failed",
            phase_id=phase.id,
            err=str(exc),
        )


async def _execute_one(
    orch: "Orchestrator",
    task: Task,
    worktree_mgr: "WorktreeManager | None" = None,
) -> Task:
    """Run the developer -> reviewer -> tests loop for one task. Returns the final task.

    v0.11.0: when ``worktree_mgr`` is supplied, the task runs in an
    isolated git worktree (``tournament_dir/tasks/<task_id>``) and the
    final diff is applied to main via ``apply_patch_to_main`` after the
    task completes. The worktree is removed in a ``finally`` clause so
    a worker exit (success or failure) always cleans up.

    When ``worktree_mgr`` is ``None`` the task runs in ``orch.cwd``
    directly — the legacy serial path used by the single-task CLI
    (``execute --task-id``) and by tests that don't initialize git.
    """
    retry_limit = orch.cfg.qa_retry_limit

    # Step 0: short-circuit non-agent-executable tasks (v0.6.1).
    # Tasks with a non-empty ``requires`` list are skipped programmatically —
    # the orchestrator never invokes any adapter for them. Mirrors the
    # ``status in ('complete', 'skipped')`` short-circuit in
    # :func:`run_execute_phase` for tasks that are already terminal, but
    # catches the *first-time* skip path before any FSM transition.
    if task.requires:
        blocked_reason = f"requires={list(task.requires)}"
        skipped_task = await orch.plan_manager.update_task_status(
            task.id,
            "skipped",
            meta={"blocked_reason": blocked_reason},
        )
        logger.info(
            "execute_phase.skip_requires",
            task_id=task.id,
            requires=list(task.requires),
        )
        return skipped_task

    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    # v0.11.0: per-task worktree isolation. ``worktree`` is the cwd
    # passed into delegate() for every agent invocation in this task —
    # the developer writes there, QA gates run there, and the diff is
    # applied to main after completion. ``None`` keeps the legacy path
    # where everything happens in orch.cwd.
    worktree: Path | None = None
    if worktree_mgr is not None:
        # v0.17.0 S6: optional sparse-checkout. When the config flag is on,
        # narrow the worktree to ``phase.edit_scope or plan.edit_scope``
        # so huge repos don't materialize gigabytes of unrelated files
        # into every per-task worktree. Falls back to a full checkout
        # when (a) the flag is off, (b) no scope is declared, or (c)
        # git is older than 2.25 (pre-flighted in WorktreeManager.create).
        sparse_paths: list[str] | None = None
        if orch.cfg.worktree_sparse_checkout_enabled:
            try:
                _plan_for_scope = await orch.plan_manager.load()
            except Exception:  # noqa: BLE001
                _plan_for_scope = None
            if _plan_for_scope is not None:
                _resolved: list[str] = []
                for _ph in _plan_for_scope.phases:
                    if _ph.id == task.phase_id:
                        _resolved = (
                            list(_ph.edit_scope)
                            if _ph.edit_scope is not None
                            else list(_plan_for_scope.edit_scope)
                        )
                        break
                else:
                    _resolved = list(_plan_for_scope.edit_scope)
                if _resolved:
                    sparse_paths = _resolved
        try:
            worktree = await worktree_mgr.create_per_task(
                task.id, sparse_paths=sparse_paths
            )
        except WorktreeError as exc:
            logger.warning(
                "execute_phase.worktree_create_failed",
                task_id=task.id,
                err=str(exc),
            )
            worktree = None

    orch.guardrails.start_task(task.id)
    try:
        # Retry loop — one iteration = one developer-then-gates cycle.
        last_issues: list[str] = []
        while True:
            try:
                developer_env = _developer_envelope(task, extra_issues=last_issues)
                developer_result = await delegate(
                    orch,
                    "developer",
                    developer_env,
                    retry_count=task.retry_count,
                    last_issues=last_issues,
                    task=task,
                    cwd_override=worktree,
                )
            except GuardrailExceededError as exc:
                logger.warning(
                    "execute_phase.guardrail_exceeded",
                    task_id=task.id,
                    reason=str(exc),
                )
                task = await orch.plan_manager.update_task_status(
                    task.id,
                    "blocked",
                    meta={"blocked_reason": f"guardrail_exceeded: {exc}"},
                )
                return task

            coder_ev = CoderEvidence(
                task_id=task.id,
                diff=developer_result.diff,
                files_changed=[str(p) for p in developer_result.files_changed],
                output_text=developer_result.text,
                duration_s=developer_result.duration_s,
                success=developer_result.success,
            )
            await write_evidence(orch.cwd, task.id, coder_ev)
            if developer_result.diff:
                await write_patch(orch.cwd, task.id, developer_result.diff)

            if not developer_result.success:
                logger.warning(
                    "execute_phase.developer_failed",
                    task_id=task.id,
                    err=developer_result.error,
                )
                task = await _try_retry_or_escalate(
                    orch, task, retry_limit, reason="coder adapter failure"
                )
                if task.escalated:
                    return task
                last_issues = [developer_result.error or "adapter failure"]
                continue

            task = await orch.plan_manager.update_task_status(task.id, "coded")

            # Step 3: auto-gates (syntax/lint/build/test_runner/secretscan).
            # v0.13.0: pass developer_result so secretscan can scope to the
            # diff (skip pre-existing repo state).
            gate_failure = await _run_qa_gates(
                orch, task, developer_result=developer_result
            )
            if gate_failure is not None:
                logger.warning(
                    "execute_phase.qa_gate_failed",
                    task_id=task.id,
                    details=gate_failure,
                )
                task = await _try_retry_or_escalate(
                    orch, task, retry_limit, reason=gate_failure
                )
                if task.escalated:
                    return task
                last_issues = [gate_failure]
                continue
            task = await orch.plan_manager.update_task_status(task.id, "auto_gated")

            # Step 4: reviewer.
            try:
                review_env = _review_envelope(task, coder_ev.diff or "")
                review_result = await delegate(
                    orch,
                    "reviewer",
                    review_env,
                    retry_count=task.retry_count,
                    last_issues=last_issues,
                    cwd_override=worktree,
                )
            except GuardrailExceededError as exc:
                logger.warning(
                    "execute_phase.guardrail_exceeded",
                    task_id=task.id,
                    reason=str(exc),
                )
                task = await orch.plan_manager.update_task_status(
                    task.id,
                    "blocked",
                    meta={"blocked_reason": f"guardrail_exceeded: {exc}"},
                )
                return task

            verdict, issues = _parse_review_verdict(review_result.text)
            review_ev = ReviewEvidence(
                task_id=task.id,
                verdict=cast("Literal['APPROVED', 'NEEDS_CHANGES', 'REJECTED']", verdict),
                issues=issues,
                output_text=review_result.text,
            )
            await write_evidence(orch.cwd, task.id, review_ev)
            if verdict in ("NEEDS_CHANGES", "REJECTED"):
                logger.info(
                    "execute_phase.review_needs_changes",
                    task_id=task.id,
                    verdict=verdict,
                    issues=issues,
                )
                task = await _try_retry_or_escalate(
                    orch, task, retry_limit, reason=f"reviewer {verdict}"
                )
                if task.escalated:
                    return task
                last_issues = issues or [f"reviewer {verdict}"]
                continue
            task = await orch.plan_manager.update_task_status(task.id, "reviewed")

            # Step 5: test_engineer generates and runs tests.
            try:
                test_env = _test_envelope(task, coder_ev.diff or "")
                test_result = await delegate(
                    orch,
                    "test_engineer",
                    test_env,
                    retry_count=task.retry_count,
                    last_issues=last_issues,
                    cwd_override=worktree,
                )
            except GuardrailExceededError as exc:
                logger.warning(
                    "execute_phase.guardrail_exceeded",
                    task_id=task.id,
                    reason=str(exc),
                )
                task = await orch.plan_manager.update_task_status(
                    task.id,
                    "blocked",
                    meta={"blocked_reason": f"guardrail_exceeded: {exc}"},
                )
                return task

            passed, failed, total = _parse_test_counts(test_result.text)
            test_ev = TestEvidence(
                task_id=task.id,
                passed=passed,
                failed=failed,
                total=total,
                output_text=test_result.text,
            )
            await write_evidence(orch.cwd, task.id, test_ev)
            if failed > 0 or not test_result.success:
                logger.info(
                    "execute_phase.tests_failed",
                    task_id=task.id,
                    failed=failed,
                    total=total,
                )
                task = await _try_retry_or_escalate(
                    orch, task, retry_limit, reason="tests failed"
                )
                if task.escalated:
                    return task
                last_issues = [
                    f"{failed}/{total} tests failed",
                    test_result.text[:500],
                ]
                continue
            task = await orch.plan_manager.update_task_status(task.id, "tested")

            # Step 6: impl tournament.
            if orch.cfg.tournaments.impl.enabled and not orch.disable_impl_tournament:
                from orchestrator.impl_tournament_runner import (
                    run_impl_tournament,
                )
                from tournament import ImplBundle as _ImplBundle

                _initial_bundle = _ImplBundle(
                    task_id=task.id,
                    task_description=task.description,
                    diff=coder_ev.diff or "",
                    files_changed=coder_ev.files_changed,
                    tests_passed=passed,
                    tests_failed=failed,
                    tests_total=total,
                    test_output_excerpt=test_result.text[:1000],
                )
                try:
                    await run_impl_tournament(orch, task, _initial_bundle)
                except Exception as _exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.impl_tournament_error",
                        task_id=task.id,
                        err=str(_exc),
                    )
            task = await orch.plan_manager.update_task_status(task.id, "tournamented")

            # Step 7: extract and record lessons from agent outputs.
            await _record_lessons(orch, task.id, developer_result.text, "developer")
            await _record_lessons(orch, task.id, review_result.text, "reviewer")
            await _record_lessons(orch, task.id, test_result.text, "test_engineer")

            # v0.11.0: when running in a per-task worktree, apply the
            # diff to the main repo BEFORE marking complete. This is
            # the convergence step that makes parallel execution safe:
            # only after a successful apply do dependent tasks see the
            # change. Apply failures route into critic-escalation:
            # critic_sounding_board is invoked in CONFLICT ESCALATION
            # MODE and its RESOLUTION directive determines the next
            # step (rebase-and-retry / abandon-task / rewrite).
            if worktree_mgr is not None and worktree is not None:
                applied = await _apply_with_conflict_escalation(
                    orch, task, worktree, worktree_mgr
                )
                if not applied:
                    return await orch.plan_manager.get_task(task.id) or task

            # Step 8: complete.
            task = await orch.plan_manager.update_task_status(
                task.id,
                "complete",
                meta={"evidence_bundle": f".autodev/evidence/{task.id}-coder.json"},
            )
            # v0.15.0: zero the stuck-state counters on success so the
            # ladder accounting tracks the *current* episode of stuck-ness,
            # not historical retries that successfully recovered.
            try:
                await orch.plan_manager.reset_stuck_state(task.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.reset_stuck_state_failed",
                    task_id=task.id,
                    err=str(exc),
                )
            logger.info("execute_phase.task_complete", task_id=task.id)
            return task
    finally:
        orch.guardrails.end_task(task.id)
        # v0.11.0: always clean up the per-task worktree, even on
        # exception. ``remove_per_task`` swallows missing-worktree races.
        if worktree_mgr is not None and worktree is not None:
            try:
                await worktree_mgr.remove_per_task(task.id, force=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.worktree_remove_failed",
                    task_id=task.id,
                    err=str(exc),
                )


async def _try_retry_or_escalate(
    orch: "Orchestrator",
    task: Task,
    retry_limit: int,
    reason: str,
) -> Task:
    """Bump retry count or escalate when the limit is reached.

    Returns the updated task. If ``task.escalated`` becomes True on return,
    the caller should stop the loop.

    v0.15.0: in addition to the legacy retry-then-escalate logic, this
    helper now consults the per-task :class:`StuckState` against
    :func:`orchestrator.escalation_ladder.next_step`. When the ladder
    returns ``"continue"`` (the dominant path in normal runs), behavior
    is identical to v0.14.0 — backward compat. Otherwise the helper
    dispatches to :func:`_escalate_stuck_to_critic` and applies the
    resolution:

    * ``"REFINE"`` → critic suggests a small adjustment; on
      ``RESOLUTION: refine`` the task is restarted in_progress (the
      caller's loop picks up the developer with the refined guidance
      injected via ``last_issues`` on the next iteration). The
      lessons-emit path records a ``course_correction`` event.
    * ``"PIVOT"`` → critic suggests a radical redirect; on
      ``RESOLUTION: pivot`` the pivot counter is bumped and the task
      is restarted with the radical guidance.
    * ``"SOFT_BLOCKER"`` → critic confirms a human-required decision;
      the task is marked ``escalated`` + ``blocked`` with the guidance
      surfaced as ``blocked_reason``. Lessons-emit records a
      ``soft_blocker`` event.

    All branches first bump :meth:`PlanManager.increment_discard` so the
    ladder accounting reflects the freshest signal.
    """
    from orchestrator.escalation_ladder import next_step
    from state.knowledge import TournamentEvent

    # v0.15.0: bump the stuck-state discard counter under lock.
    stuck_state = await orch.plan_manager.increment_discard(task.id)
    step = next_step(stuck_state)

    if step != "continue":
        # Ladder dispatch path. Build a minimal prior_attempts list from
        # the legacy retry_count for forensics.
        prior_attempts = [f"retry_count={task.retry_count}, reason={reason}"]

        # v0.18.0: WEB_SEARCH ladder step — when enabled by config, fetch
        # top-3 search results and splice them as a ``WEB_CONTEXT:`` block
        # into the next critic prompt. After the search dispatches, fall
        # through to the critic invocation with the spliced context. The
        # search adapter, web_search_invoked ledger op, and search_count
        # accounting all already shipped in v0.17.0; this is the deferred
        # wiring at the executor's dispatch site.
        web_context_block: str = ""
        if step == "WEB_SEARCH" and getattr(
            orch.cfg, "web_search_enabled", False
        ):
            try:
                from adapters.web_search import web_search

                # Build a minimal query from the failure reason + task.
                query = f"{task.title}: {reason}"[:200]
                results = await web_search(query, max_results=3)
                # Format results into the WEB_CONTEXT: block.
                if results:
                    rows = []
                    for r in results:
                        rows.append(f"  - {r.title}: {r.url}")
                        if r.snippet:
                            rows.append(f"    {r.snippet[:200]}")
                    web_context_block = (
                        "WEB_CONTEXT:\n" + "\n".join(rows) + "\n"
                    )
                # Bump search_count under lock + ledger op.
                await orch.plan_manager.increment_search(task.id)
                try:
                    await orch.plan_manager.ledger_append(
                        op="web_search_invoked",
                        payload={
                            "task_id": task.id,
                            "query": query,
                            "results_count": len(results),
                            "search_count_after": stuck_state.search_count + 1,
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.ledger_append_failed",
                        op="web_search_invoked",
                        err=str(exc),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.web_search_failed",
                    task_id=task.id,
                    err=str(exc),
                )

        try:
            resolution = await _escalate_stuck_to_critic(
                orch,
                task,
                stuck_state=stuck_state,
                ladder_step=step,
                recent_evidence=(
                    web_context_block + reason if web_context_block else reason
                ),
                prior_attempts=prior_attempts,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.stuck_escalation_failed",
                task_id=task.id,
                step=step,
                err=str(exc),
            )
            # Fall through to legacy path on any escalation failure.
            resolution = None

        if resolution is not None:
            # Branch on the critic's chosen action.
            if resolution.action == "soft-blocker":
                await orch.plan_manager.mark_escalated(task.id)
                guidance_text = resolution.guidance or "human decision required"
                updated = await orch.plan_manager.update_task_status(
                    task.id,
                    "blocked",
                    meta={
                        "blocked_reason": (
                            f"soft-blocker: {guidance_text}"
                        )
                    },
                )
                # Audit-only ledger op + cross-run lesson.
                try:
                    await orch.plan_manager.ledger_append(
                        op="soft_blocker_handoff",
                        payload={
                            "task_id": task.id,
                            "reason": reason,
                            "critic_response_excerpt": guidance_text[:500],
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.ledger_append_failed",
                        op="soft_blocker_handoff",
                        err=str(exc),
                    )
                try:
                    await orch.knowledge.record_tournament_event(
                        TournamentEvent(
                            event_type="soft_blocker",
                            family="execute-phase",
                            hypothesis=(
                                f"task {task.id} required human decision "
                                f"after {stuck_state.discard_count} discards "
                                f"and {stuck_state.pivot_count} pivots"
                            ),
                            evidence=guidance_text[:500],
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.knowledge_record_failed",
                        event="soft_blocker",
                        err=str(exc),
                    )
                return updated

            if resolution.action == "pivot":
                # Bump pivot counter under lock + audit ledger op.
                await orch.plan_manager.increment_pivot(task.id)
                try:
                    await orch.plan_manager.ledger_append(
                        op="stuck_pivot",
                        payload={
                            "task_id": task.id,
                            "reason": reason,
                            "critic_response_excerpt": resolution.guidance[:500],
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.ledger_append_failed",
                        op="stuck_pivot",
                        err=str(exc),
                    )
                try:
                    await orch.knowledge.record_tournament_event(
                        TournamentEvent(
                            event_type="course_correction",
                            family="execute-phase",
                            hypothesis=(
                                f"task {task.id} pivoted at "
                                f"discard_count={stuck_state.discard_count}"
                            ),
                            evidence=resolution.guidance[:500],
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.knowledge_record_failed",
                        event="pivot",
                        err=str(exc),
                    )
                # Restart the task so the executor's outer loop re-picks
                # it with the pivot guidance injected via ``last_issues``.
                if task.status != "in_progress":
                    task = await orch.plan_manager.update_task_status(
                        task.id, "in_progress"
                    )
                return await orch.plan_manager.get_task(task.id) or task

            # ``refine`` (also the defensive default).
            try:
                await orch.plan_manager.ledger_append(
                    op="stuck_refine",
                    payload={
                        "task_id": task.id,
                        "reason": reason,
                        "critic_response_excerpt": resolution.guidance[:500],
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.ledger_append_failed",
                    op="stuck_refine",
                    err=str(exc),
                )
            try:
                await orch.knowledge.record_tournament_event(
                    TournamentEvent(
                        event_type="course_correction",
                        family="execute-phase",
                        hypothesis=(
                            f"task {task.id} refined at "
                            f"discard_count={stuck_state.discard_count}"
                        ),
                        evidence=resolution.guidance[:500],
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.knowledge_record_failed",
                    event="refine",
                    err=str(exc),
                )
            if task.status != "in_progress":
                task = await orch.plan_manager.update_task_status(
                    task.id, "in_progress"
                )
            return await orch.plan_manager.get_task(task.id) or task

    # Legacy ("continue") path: behavior identical to v0.14.0.
    new_count = await orch.plan_manager.mark_task_retry(task.id)
    if new_count >= retry_limit:
        logger.warning(
            "execute_phase.retry_exhausted",
            task_id=task.id,
            retry=new_count,
            reason=reason,
        )
        sb_env = DelegationEnvelope(
            task_id=task.id,
            target_agent="critic_sounding_board",
            action="critique",
            acceptance="Diagnose why this task keeps failing and suggest next steps.",
            context={
                "task_id": task.id,
                "reason": reason,
                "retry_count": new_count,
            },
        )
        sb_result = await delegate(orch, "critic_sounding_board", sb_env)
        await write_evidence(
            orch.cwd,
            task.id,
            CriticEvidence(
                task_id=task.id,
                verdict="NEEDS_REVISION",
                issues=[reason],
                output_text=sb_result.text,
            ),
        )
        # Cross-run lesson: legacy retry-exhaustion escalation.
        try:
            await orch.knowledge.record_tournament_event(
                TournamentEvent(
                    event_type="escalation",
                    family="execute-phase",
                    hypothesis=(
                        f"task {task.id} exceeded retry_limit={retry_limit}"
                    ),
                    evidence=f"reason={reason} retry_count={new_count}",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.knowledge_record_failed",
                event="escalation",
                err=str(exc),
            )
        await orch.plan_manager.mark_escalated(task.id)
        updated = await orch.plan_manager.update_task_status(
            task.id,
            "blocked",
            meta={"blocked_reason": f"escalated: {reason}"},
        )
        return updated

    # Retry: transition blocked/etc -> in_progress as appropriate.
    if task.status != "in_progress":
        task = await orch.plan_manager.update_task_status(task.id, "in_progress")
    fresh = await orch.plan_manager.get_task(task.id)
    return fresh or task


async def delegate(
    orch: "Orchestrator",
    role: str,
    envelope: DelegationEnvelope,
    extra_context: str = "",
    retry_count: int = 0,
    last_issues: list[str] | None = None,
    task: Task | None = None,
    cwd_override: Path | None = None,
) -> AgentResult:
    """Build an :class:`AgentInvocation` from the envelope and call the adapter.

    Guardrail hooks are called around the adapter execution:
    - ``pre_invocation`` before the adapter call (may raise GuardrailExceededError)
    - ``post_invocation`` after the adapter call (may raise GuardrailExceededError)
    - ``loop_detector.observe`` after post_invocation

    For :class:`~adapters.inline.InlineAdapter`:
    - If a response file already exists (resume path), collect and return it.
    - Otherwise inject ``task_id`` into ``inv.metadata`` so the adapter can
      name the delegation file, then re-raise :class:`DelegationPendingSignal`
      after writing suspend state.

    v0.8.0: when ``task`` is provided, the per-task complexity resolvers
    override ``max_turns`` and ``timeout_s`` on the constructed invocation.
    The developer call passes ``task`` so its budget scales with the
    architect-tagged complexity bucket; reviewer/test_engineer/critic_t
    calls leave it unset and inherit the spec defaults (which are tuned
    for shorter, single-purpose passes).
    """
    spec = orch.registry.get(role)
    if spec is None:
        raise AutodevError(f"role {role!r} not in registry")
    parts: list[str] = [spec.prompt.strip()]
    parts.append("\n\n---\n")
    parts.append(envelope.render_as_task_message())
    if extra_context:
        parts.append("\n\n")
        parts.append(extra_context)
    lessons = await orch.knowledge.inject_block(role, task_id=envelope.task_id)
    if lessons:
        parts.append("\n\n")
        parts.append(lessons)

    # v0.15.0: PRM trajectory pattern detection. Before this dispatch,
    # consult the trajectory store for any patterns observed since the
    # last call. If a CourseCorrection is pending for the task AND has
    # not been emitted yet, splice the markdown block into the prompt
    # and mark it emitted (cap one correction per fingerprint per task).
    trajectory_store = getattr(orch, "trajectory_store", None)
    if trajectory_store is not None and envelope.task_id:
        try:
            patterns = trajectory_store.analyze(envelope.task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.prm_analyze_failed",
                task_id=envelope.task_id,
                err=str(exc),
            )
            patterns = []
        if patterns:
            from orchestrator.prm import CourseCorrection

            # Highest-severity pattern first (analyze() pre-sorts).
            top = patterns[0]
            cc = CourseCorrection.from_pattern(top)
            fingerprint = cc.fingerprint()
            if not trajectory_store.has_emitted(envelope.task_id, fingerprint):
                parts.append("\n\n")
                parts.append(cc.format_for_prompt())
                trajectory_store.mark_emitted(envelope.task_id, fingerprint)
                # Audit-only ledger op + cross-run lesson.
                if hasattr(orch, "plan_manager") and orch.plan_manager is not None:
                    try:
                        await orch.plan_manager.ledger_append(
                            op="course_correction_emitted",
                            payload={
                                "task_id": envelope.task_id,
                                "taxonomy": cc.taxonomy,
                                "pattern": cc.pattern,
                                "suggestion": cc.suggestion[:500],
                            },
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "execute_phase.ledger_append_failed",
                            op="course_correction_emitted",
                            err=str(exc),
                        )
                try:
                    from state.knowledge import TournamentEvent

                    await orch.knowledge.record_tournament_event(
                        TournamentEvent(
                            event_type="course_correction",
                            family="prm",
                            hypothesis=(
                                f"PRM detected {cc.pattern} on task "
                                f"{envelope.task_id}"
                            ),
                            evidence=cc.suggestion[:500],
                            next_action_hint=(
                                f"taxonomy={cc.taxonomy}; review trajectory "
                                "before next dispatch"
                            ),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.knowledge_record_failed",
                        event="course_correction",
                        err=str(exc),
                    )

    # v0.14.0: inject EDIT SCOPE addendum for the developer role when the
    # plan/phase declares a non-empty scope. Resolution mirrors
    # :func:`orchestrator.dag.validate_edit_scope`: phase-level override
    # (when non-None) wins over plan-level. The developer sees the
    # narrowest applicable boundary as a single line in their system
    # prompt addendum so they can self-police edits before writing.
    if role == "developer" and orch.plan_manager is not None and task is not None:
        try:
            existing_plan_for_scope = await orch.plan_manager.load()
        except Exception:  # noqa: BLE001
            existing_plan_for_scope = None
        if existing_plan_for_scope is not None:
            resolved_scope: list[str] = []
            for ph in existing_plan_for_scope.phases:
                if ph.id == task.phase_id:
                    if ph.edit_scope is not None:
                        resolved_scope = list(ph.edit_scope)
                    else:
                        resolved_scope = list(existing_plan_for_scope.edit_scope)
                    break
            else:
                # task.phase_id not matched (defensive — shouldn't happen
                # in production); fall back to plan-level scope.
                resolved_scope = list(existing_plan_for_scope.edit_scope)
            if resolved_scope:
                parts.append("\n\n")
                parts.append(f"EDIT SCOPE: {', '.join(resolved_scope)}")

    # Resolve per-role effort using the parsed plan complexity. Once the
    # execute phase begins, the architect has run and ``Plan.complexity`` is
    # set (or None on legacy/pre-upgrade plans, which gracefully falls back
    # to the user-global default).
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

    # v0.8.0: per-task complexity overrides for ``max_turns`` and
    # ``timeout_s``. Resolvers return ``None`` when no override applies (or
    # when ``task`` is unset for non-developer roles), and the spec / module
    # constant defaults take over.
    spec_max_turns = spec.max_turns or 1
    if task is not None:
        # v0.13.0: thread the orchestrator's repo-size snapshot through so
        # tasks on Unity-class repos get the doubled per-complexity budget.
        # ``_repo_capacity`` may be None on the orchestrator stub used in
        # some unit tests (back-compat). Fall back to legacy behavior in
        # that case.
        repo_capacity = getattr(orch, "_repo_capacity", None)
        max_turns = (
            resolve_task_max_turns(task, spec.max_turns, capacity=repo_capacity)
            or spec.max_turns
            or 1
        )
        timeout_s = resolve_task_timeout_s(task, _DEFAULT_DEVELOPER_TIMEOUT_S)
        if timeout_s is None:
            timeout_s = _DEFAULT_DEVELOPER_TIMEOUT_S
    else:
        max_turns = spec_max_turns
        timeout_s = None  # adapter applies its own default (600s in claude_code)

    # v0.11.0: per-task worktree isolation — when a worker passes a
    # cwd_override (its worktree path), agent execution happens there
    # rather than in orch.cwd (the main repo). All other accounting
    # (evidence, ledger, guardrails) still keys off orch.cwd.
    effective_cwd = cwd_override if cwd_override is not None else orch.cwd

    inv = AgentInvocation(
        role=role,
        prompt="\n".join(parts),
        cwd=effective_cwd,
        model=spec.model,
        allowed_tools=list(spec.tools) if spec.tools else None,
        max_turns=max_turns,
        effort=effort,
        timeout_s=timeout_s,
    )

    # Inline adapter: check for existing response (resume shortcut) or inject
    # task_id into metadata so the adapter can name the delegation file.
    if isinstance(orch.adapter, InlineAdapter):
        if orch.adapter.has_pending_response(envelope.task_id, role):
            result = orch.adapter.collect_response(envelope.task_id, role)
            orch.guardrails.post_invocation(envelope.task_id, result)
            if result.success and result.text:
                orch.loop_detector.observe(envelope.task_id, role, result.text)
            return result
        inv = inv.model_copy(
            update={"metadata": {**inv.metadata, "task_id": envelope.task_id}}
        )

    orch.guardrails.pre_invocation(envelope.task_id, inv)
    import time as _time

    _t0 = _time.time()
    try:
        result = await orch.adapter.execute(inv)
    except DelegationPendingSignal as sig:
        write_suspend_state(
            cwd=orch.cwd,
            session_id=orch.session_id,
            pending_task_id=envelope.task_id,
            pending_role=role,
            delegation_path=sig.delegation_path,
            response_path=orch.adapter.response_path(envelope.task_id, role),  # type: ignore[attr-defined]
            orchestrator_step=role,
            retry_count=retry_count,
            last_issues=last_issues or [],
        )
        raise
    orch.guardrails.post_invocation(envelope.task_id, result)
    if result.success and result.text:
        orch.loop_detector.observe(envelope.task_id, role, result.text)

    # v0.15.0: record this dispatch into the PRM trajectory store so the
    # NEXT delegate call can run pattern detection against an updated
    # event log. Pure observation — no decisions are made here; the
    # injection logic at the top of this function consumes any pending
    # CourseCorrection from prior analyze() runs.
    if trajectory_store is not None and envelope.task_id:
        try:
            from orchestrator.prm import TrajectoryEvent

            trajectory_store.record(
                envelope.task_id,
                TrajectoryEvent(
                    timestamp=_t0,
                    role=role,
                    action=envelope.action or "dispatch",
                    target_files=tuple(envelope.files or []),
                    success=bool(result.success),
                    duration_s=max(0.0, _time.time() - _t0),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.prm_record_failed",
                task_id=envelope.task_id,
                err=str(exc),
            )

    return result


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------


def _developer_envelope(task: Task, extra_issues: list[str]) -> DelegationEnvelope:
    acceptance = " | ".join(a.description for a in task.acceptance) or None
    context: dict = {"task_title": task.title, "task_description": task.description}
    # v0.8.0: surface the architect-tagged complexity bucket to the developer
    # so it can pace itself (a ``complex`` task gets 40 turns and should not
    # wrap up after 5 — the prompt-level hint reinforces the budget the
    # adapter-level ``max_turns`` enforces). Defaults to ``"medium"`` when
    # the architect didn't tag a bucket — matches the orchestrator's spec
    # fallback shape and avoids surfacing the raw ``None`` value.
    context["complexity"] = task.complexity or "medium"
    if extra_issues:
        context["prior_issues"] = extra_issues
    return DelegationEnvelope(
        task_id=task.id,
        target_agent="developer",
        action="implement",
        files=list(task.files),
        acceptance=acceptance,
        context=context,
    )


def _review_envelope(task: Task, diff: str) -> DelegationEnvelope:
    return DelegationEnvelope(
        task_id=task.id,
        target_agent="reviewer",
        action="review",
        files=list(task.files),
        acceptance=(
            "Respond with one of APPROVED / NEEDS_CHANGES / REJECTED on the "
            "first line. Follow with bullet-point issues if not APPROVED."
        ),
        context={
            "task_title": task.title,
            "task_description": task.description,
            "diff": diff[:8000],
        },
    )


def _test_envelope(task: Task, diff: str) -> DelegationEnvelope:
    return DelegationEnvelope(
        task_id=task.id,
        target_agent="test_engineer",
        action="test",
        files=list(task.files),
        acceptance=(
            "Run tests and return a line of the form 'RESULTS: passed=N "
            "failed=M total=T'. Include failure output if any test failed."
        ),
        context={
            "task_title": task.title,
            "diff": diff[:8000],
        },
    )


# ---------------------------------------------------------------------------
# Output parsers
# ---------------------------------------------------------------------------


def _parse_review_verdict(text: str) -> tuple[str, list[str]]:
    if not text:
        return "NEEDS_CHANGES", ["empty reviewer response"]
    verdict = "APPROVED"
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        upper = s.upper()
        if "REJECTED" in upper:
            verdict = "REJECTED"
        elif "NEEDS_CHANGES" in upper or "NEEDS CHANGES" in upper:
            verdict = "NEEDS_CHANGES"
        elif "APPROVED" in upper:
            verdict = "APPROVED"
        else:
            continue
        break
    issues: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- ") or s.startswith("* "):
            issues.append(s[2:].strip())
    return verdict, issues


def _parse_test_counts(text: str) -> tuple[int, int, int]:
    """Parse ``RESULTS: passed=N failed=M total=T`` from test_engineer output.

    Very forgiving — missing values default to 0. If no RESULTS line is
    present, return (0, 0, 0) and let the orchestrator treat it as failure
    only if ``result.success`` is also False.
    """
    import re

    m = re.search(
        r"passed\s*=\s*(\d+)\s+failed\s*=\s*(\d+)\s+total\s*=\s*(\d+)",
        text,
        re.IGNORECASE,
    )
    if m is None:
        return 0, 0, 0
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _files_changed_for_secretscan(
    developer_result: AgentResult | None,
) -> list[Path] | None:
    """Extract repo-relative paths from a developer ``AgentResult`` for the
    v0.13.0 diff-scoped secretscan.

    Returns:
        * ``None`` when the result is missing or has no diff text — the
          caller falls back to the legacy full-walk behavior.
        * ``[]`` when the diff is non-empty but contains no parseable
          ``+++ b/`` headers (e.g. error output) — caller may choose to
          skip the gate entirely (empty list scans nothing).
        * ``list[Path]`` of repo-relative paths in first-seen, deduped
          order.
    """
    if developer_result is None or not developer_result.diff:
        return None
    return [Path(p) for p in extract_files_from_diff(developer_result.diff)]


async def _run_qa_gates(
    orch: "Orchestrator",
    task: "Task",
    developer_result: AgentResult | None = None,
) -> str | None:
    """Run enabled QA gates. Returns the first failure detail string, or None if all pass.

    v0.13.0: ``developer_result`` is the optional output of the most recent
    developer delegation. When supplied, the secretscan gate is invoked
    with ``paths=`` extracted from its diff so only the executor's just-
    introduced changes are scanned. None preserves legacy whole-tree walk.
    """
    from plugins.registry import QAContext

    cfg = orch.cfg.qa_gates
    cwd = orch.cwd
    language = detect_language(cwd)

    secretscan_paths = _files_changed_for_secretscan(developer_result)
    # v0.16.0: hallucination-guard reuses the diff-scope path list so the
    # AST walk only visits files the executor just touched. ``cfg.hallucination_guard``
    # is a top-level toggle (default True) — see :class:`AutodevConfig`.
    hallucination_guard_enabled = bool(
        getattr(orch.cfg, "hallucination_guard", True)
    )

    gates: list[tuple[bool, Callable[[], Awaitable[GateResult]]]] = [
        (cfg.syntax_check, lambda: run_syntax_check(cwd, language)),
        (cfg.lint, lambda: run_lint(cwd, language)),
        (cfg.build_check, lambda: run_build_check(cwd, language)),
        (cfg.test_runner, lambda: run_tests(cwd)),
        (cfg.secretscan, lambda: run_secretscan(cwd, paths=secretscan_paths)),
        (
            hallucination_guard_enabled,
            lambda: run_hallucination_guard(cwd, paths=secretscan_paths),
        ),
    ]

    for enabled, gate_fn in gates:
        if not enabled:
            continue
        result: GateResult = await gate_fn()
        if not result.passed:
            return result.details or "QA gate failed"

    # Run plugin QA gates after all built-in gates pass.
    if hasattr(orch, "plugin_registry") and orch.plugin_registry is not None:
        ctx = QAContext(cwd=cwd, task_id=task.id)
        for plugin in orch.plugin_registry.qa_gates.values():
            try:
                plugin_result = await plugin.run(ctx)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.plugin_gate_error",
                    task_id=task.id,
                    plugin=plugin.name,
                    error=str(exc),
                )
                continue
            if not plugin_result.passed:
                return plugin_result.details or f"plugin gate '{plugin.name}' failed"

    return None


async def _record_lessons(
    orch: "Orchestrator",
    task_id: str,
    output_text: str,
    role: str,
) -> None:
    """Scan ``output_text`` for ``LESSON:`` prefixed lines and record each.

    Extraction is lightweight: only lines that start with ``LESSON:``
    (case-insensitive, after stripping whitespace) are recorded.  Each lesson
    is recorded with confidence 0.7 and the agent's role as ``role_source``.

    If ``orch.knowledge`` is None or recording raises, a WARNING is logged and
    execution continues — knowledge errors must never block task completion.
    """
    if not output_text:
        return
    lessons: list[str] = []
    for line in output_text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("LESSON:"):
            lesson_text = stripped[len("LESSON:"):].strip()
            if lesson_text:
                lessons.append(lesson_text)
    if not lessons:
        return
    for lesson_text in lessons:
        try:
            await orch.knowledge.record(lesson_text, role, confidence=0.7)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.knowledge_record_failed",
                task_id=task_id,
                role=role,
                err=str(exc),
            )


__all__ = [
    "ConflictResolution",
    "StuckResolution",
    "TaskEscalatedError",
    "delegate",
    "run_execute_phase",
]
