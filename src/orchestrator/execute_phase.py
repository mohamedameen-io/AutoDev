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

from adapters.git_utils import _git_rev_parse_head
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


def _indent_block(text: str, prefix: str = "  ") -> str:
    """Indent every line of ``text`` by ``prefix`` for the YAML-ish block.

    Returns the empty string verbatim so the CONFLICT_CONTEXT: block is
    well-formed even when one of the diffs is missing.
    """
    if not text:
        return ""
    return "\n".join(prefix + line for line in text.splitlines())


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
        # Validate every phase's DAG up-front. A bad DAG short-circuits
        # the entire run rather than propagating mid-execute.
        from orchestrator.dag import DagValidationError, validate_phase_dag

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
        try:
            worktree = await worktree_mgr.create_per_task(task.id)
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
            gate_failure = await _run_qa_gates(orch, task)
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
            # change. Apply failures route into the conflict-escalation
            # path (commit 14 wires this in).
            if worktree_mgr is not None and worktree is not None:
                try:
                    await worktree_mgr.apply_patch_to_main(worktree, base_ref="HEAD")
                except WorktreeError as exc:
                    logger.warning(
                        "execute_phase.apply_patch_failed",
                        task_id=task.id,
                        err=str(exc),
                    )
                    task = await orch.plan_manager.update_task_status(
                        task.id,
                        "blocked",
                        meta={
                            "blocked_reason": f"apply_patch_failed: {exc}"
                        },
                    )
                    return task

            # Step 8: complete.
            task = await orch.plan_manager.update_task_status(
                task.id,
                "complete",
                meta={"evidence_bundle": f".autodev/evidence/{task.id}-coder.json"},
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
    """
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
        max_turns = (
            resolve_task_max_turns(task, spec.max_turns)
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


async def _run_qa_gates(orch: "Orchestrator", task: "Task") -> str | None:
    """Run enabled QA gates. Returns the first failure detail string, or None if all pass."""
    from plugins.registry import QAContext

    cfg = orch.cfg.qa_gates
    cwd = orch.cwd
    language = detect_language(cwd)

    gates: list[tuple[bool, Callable[[], Awaitable[GateResult]]]] = [
        (cfg.syntax_check, lambda: run_syntax_check(cwd, language)),
        (cfg.lint, lambda: run_lint(cwd, language)),
        (cfg.build_check, lambda: run_build_check(cwd, language)),
        (cfg.test_runner, lambda: run_tests(cwd)),
        (cfg.secretscan, lambda: run_secretscan(cwd)),
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
    "TaskEscalatedError",
    "delegate",
    "run_execute_phase",
]
