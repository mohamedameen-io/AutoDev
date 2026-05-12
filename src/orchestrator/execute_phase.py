"""Execute-phase loop and conflict-escalation helpers.

For each pending task (or a specific task when ``task_id`` is given):

  1. Build a :class:`DelegationEnvelope` from the task.
  2. developer -> :class:`CoderEvidence`. Retry on adapter failure up to
     ``qa_retry_limit``; on exhaustion, escalate.
  3. test_engineer -> :class:`TestEvidence`. Any failure retries test_engineer.
  4. auto-gates (syntax/lint/build/run_tests/secretscan). ``TODO(v0.26+)``:
     replace the current always-pass placeholder with real gate execution.
  5. reviewer -> :class:`ReviewEvidence`. NEEDS_CHANGES / REJECTED counts
     as a retry back to developer with the issue list injected as context.
  6. ``TODO(v0.26+)``: wire :class:`ImplementationTournament` into the
     execute-phase FSM. Today's execute path skips it; the impl-tournament
     module itself exists and works via the dedicated CLI surface.
  7. Mark task complete.

On retry exhaustion, call ``critic_sounding_board`` once, flag the task as
escalated, mark it blocked, and stop the loop.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from adapters.git_utils import _git_rev_parse_head, extract_files_from_diff
from adapters.types import AgentInvocation, AgentResult
from errors import AutodevError, GuardrailExceededError, TournamentError
from autologging import get_logger
from orchestrator.delegation_envelope import DelegationEnvelope
from orchestrator.worktree import WorktreeError, WorktreeManager
from state.evidence import write_evidence, write_patch
from qa import (
    GateResult,
    detect_language,
    run_build_check,
    run_lint,
    run_hallucination_guard,
    run_mutation_test,
    run_secretscan,
    run_syntax_check,
    run_tests,
)
from qa.code_size import run_code_size
from state.paths import autodev_root
from state.schemas import (
    CoderEvidence,
    CriticEvidence,
    Phase,
    Plan,
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


# v0.26.1 patch G: architect-consult parser + helper. The directive set
# is distinct from the critic's STUCK RECOVERY MODE ("refine" / "pivot"
# / "soft-blocker"). The architect emits ONE of three outcomes:
#
# * ``refine-tasks`` — bullet list of sub-tasks. Orchestrator injects
#   them via the existing ``parse_corrective_direction`` pipeline.
# * ``infrastructure`` — environment / tooling failure; orchestrator
#   flags ``escalated_infra`` and falls through to SOFT_BLOCKER.
# * ``continue`` — the developer was on the right track; reset the
#   retry budget once and let it try again.

ArchitectAction = Literal[
    "architect-refine",
    "architect-infra",
    "architect-continue",
]


@dataclass
class ArchitectResolution:
    """Parsed result from ``architect_b`` in CONSULT MODE.

    Attributes:
        action: One of ``architect-refine`` / ``architect-infra`` /
            ``architect-continue``. Defaults to ``architect-infra`` —
            the least-trusting fallback if the parser cannot extract a
            directive (treats an unparseable response as an environment
            problem and routes to human action via the SOFT_BLOCKER
            follow-up).
        guidance: Free-form text from the architect. For
            ``architect-refine`` this is the bullet list passed to
            :func:`parse_corrective_direction`. For ``architect-infra``
            it's the one-line diagnosis surfaced as ``blocked_reason``.
            For ``architect-continue`` it's the approval line surfaced
            in the next developer's ``last_issues`` context.
    """

    action: ArchitectAction = "architect-infra"
    guidance: str = ""


# Regex matching the trailing ``RESOLUTION: <action>`` directive on its
# own line. The architect emits ``refine-tasks`` / ``infrastructure`` /
# ``continue`` (without the ``architect-`` prefix); the parser maps
# them onto the typed ``ArchitectAction`` values.
_ARCHITECT_DIRECTIVE_RE = re.compile(
    r"^RESOLUTION:\s*(refine-tasks|infrastructure|continue)\s*$",
    re.MULTILINE,
)
_ARCHITECT_ACTION_MAP: dict[str, ArchitectAction] = {
    "refine-tasks": "architect-refine",
    "infrastructure": "architect-infra",
    "continue": "architect-continue",
}


def _parse_architect_resolution(architect_response: str) -> ArchitectResolution:
    """Extract an :class:`ArchitectResolution` from the architect's text.

    Looks for a ``RESOLUTION: <action>`` line, anchored to its own line.
    On parse failure (no directive, unrecognised action, etc.) returns
    ``ArchitectResolution(action="architect-infra", guidance=<raw response>)``
    — the most conservative fallback that routes to human action rather
    than silently retrying.

    The guidance captured is everything FOLLOWING the matched directive
    line. The architect_b_consult.md prompt format places the
    actionable content (corrective sub-task bullets / one-line
    diagnosis / one-line approval) AFTER the directive — distinct from
    the critic's STUCK RECOVERY MODE format where the analysis lives
    BEFORE the directive. The full response is preserved as guidance on
    parse failure so operators can inspect what the architect said.
    """
    if not architect_response:
        return ArchitectResolution(action="architect-infra", guidance="")

    matches = list(_ARCHITECT_DIRECTIVE_RE.finditer(architect_response))
    if not matches:
        return ArchitectResolution(
            action="architect-infra",
            guidance=architect_response.strip(),
        )
    last = matches[-1]
    action_raw = last.group(1)
    action = _ARCHITECT_ACTION_MAP.get(action_raw)
    if action is None:
        return ArchitectResolution(
            action="architect-infra",
            guidance=architect_response.strip(),
        )

    # Guidance lives AFTER the directive line (architect prompt format).
    guidance = architect_response[last.end():].strip()
    return ArchitectResolution(action=action, guidance=guidance)


async def _escalate_stuck_to_architect(
    orch: "Orchestrator",
    task: Task,
    *,
    stuck_state: object,
    ladder_step: str,
    recent_evidence: str = "",
    prior_attempts: list[str] | None = None,
    typed_errors: list[str] | None = None,
    web_search_summary: str = "",
    reviewer_feedback: str = "",
) -> ArchitectResolution:
    """v0.26.1 patch G: invoke ``architect_b`` in CONSULT MODE.

    Mirrors :func:`_escalate_stuck_to_critic` but targets the architect
    (the agent that designed the plan in the first place) and uses a
    structurally different directive set (``refine-tasks`` /
    ``infrastructure`` / ``continue``). The consult-mode prompt is
    injected via ``extra_context`` since the existing :func:`delegate`
    flow does not support per-call prompt swapping.

    Args:
        orch: Orchestrator instance.
        task: Failing task whose autonomous budget is exhausted.
        stuck_state: :class:`StuckState` for the task — counters surface
            in the ARCHITECT_CONTEXT block.
        ladder_step: Always ``"ARCHITECT_CONSULT"`` for the canonical
            entry point. Stamped into the context block for forensics.
        recent_evidence: Freshest excerpt of failing-test / adapter
            output. Empty string allowed.
        prior_attempts: Optional list of one-line summaries of the
            most recent coder attempts.
        typed_errors: Optional list of typed error signatures
            (``qa_gate_encoding_error`` / ``error_max_turns`` / etc.)
            extracted from prior worker exceptions or adapter results.
        web_search_summary: Optional rendered summary of WEB_SEARCH rung
            results from the prior escalations. Empty when none.
        reviewer_feedback: Most recent reviewer output if any.

    Returns the parsed :class:`ArchitectResolution`. On any delegate /
    parse failure the result falls back to
    ``ArchitectResolution(action="architect-infra", guidance=<raw>)``,
    which causes the caller to route to SOFT_BLOCKER with the typed
    flag — the conservative "we asked, and we did not get a usable
    answer" path.
    """
    # Load the consult-mode prompt once; failure to read it is a
    # build-time bug (the file ships in the package) but we degrade
    # safely if a downstream operator has removed it.
    try:
        from agents import load_prompt as _load_prompt

        consult_prompt = _load_prompt("architect_b_consult")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.architect_consult_prompt_load_failed",
            err=str(exc),
        )
        consult_prompt = ""

    # Indented YAML-ish prior_attempts block; empty when no attempts.
    if prior_attempts:
        attempts_block = "\n".join(f"  - {a}" for a in prior_attempts)
    else:
        attempts_block = "  - (no prior attempts recorded)"

    typed_errors_block = (
        "\n".join(f"  - {e}" for e in typed_errors) if typed_errors else "  - (none)"
    )

    discard_count = int(getattr(stuck_state, "discard_count", 0))
    pivot_count = int(getattr(stuck_state, "pivot_count", 0))
    search_count = int(getattr(stuck_state, "search_count", 0))
    architect_count = int(getattr(stuck_state, "architect_count", 0))
    last_event = str(getattr(stuck_state, "last_event", "") or "")

    # Surface the original task definition verbatim so the architect
    # does not need to re-derive intent from the failing diffs.
    task_definition_block = (
        f"  title: {task.title}\n"
        f"  description: |\n{_indent_block(task.description, prefix='    ')}\n"
        f"  files: {list(task.files)}\n"
        f"  acceptance:\n"
        + (
            "\n".join(f"    - {ac.description}" for ac in task.acceptance)
            if task.acceptance
            else "    - (none declared)"
        )
        + "\n"
    )

    architect_context = (
        (consult_prompt + "\n\n---\n\n" if consult_prompt else "")
        + "ARCHITECT_CONTEXT:\n"
        f"failing_task_id: {task.id}\n"
        f"discard_count: {discard_count}\n"
        f"pivot_count: {pivot_count}\n"
        f"search_count: {search_count}\n"
        f"architect_count: {architect_count}\n"
        f"last_event: {last_event}\n"
        f"ladder_step: {ladder_step}\n"
        f"task_definition:\n{task_definition_block}"
        f"developer_attempts:\n{attempts_block}\n"
        f"typed_errors:\n{typed_errors_block}\n"
        "reviewer_feedback: |\n"
        f"{_indent_block(reviewer_feedback)}\n"
        "web_search_summary: |\n"
        f"{_indent_block(web_search_summary)}\n"
        "recent_evidence: |\n"
        f"{_indent_block(recent_evidence)}\n"
    )

    env = DelegationEnvelope(
        task_id=task.id,
        target_agent="architect_b",
        action="consult",
        acceptance=(
            "End your response with exactly one RESOLUTION: directive "
            "(refine-tasks / infrastructure / continue)."
        ),
        context={
            "task_id": task.id,
            "architect_consult_marker": True,
            "ladder_step": ladder_step,
        },
    )
    try:
        result = await delegate(
            orch,
            "architect_b",
            env,
            extra_context=architect_context,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.architect_consult_delegate_failed",
            task_id=task.id,
            err=str(exc),
        )
        return ArchitectResolution(
            action="architect-infra",
            guidance=f"architect_b delegate raised: {exc}",
        )

    return _parse_architect_resolution(result.text or "")


async def _dispatch_architect_consult(
    orch: "Orchestrator",
    task: Task,
    *,
    stuck_state: object,
    reason: str,
    prior_attempts: list[str] | None,
    web_context_block: str,
) -> Task | None:
    """v0.26.1 patch G: drive the ARCHITECT_CONSULT rung end-to-end.

    Increments the per-task ``architect_count`` (so the next ladder step
    routes to SOFT_BLOCKER), invokes :func:`_escalate_stuck_to_architect`,
    applies the parsed :class:`ArchitectResolution`, and returns the
    updated :class:`Task` (or ``None`` if the dispatch must fall through
    to the legacy retry path — e.g. the delegate raised before the
    architect could weigh in).

    The three return paths:

    * ``architect-refine`` — corrective sub-tasks injected via
      :meth:`PlanManager.append_corrective_tasks`. The failing task is
      transitioned to ``skipped`` with ``metadata.reason =
      "architect_consult_refine_replacement"``. Task is returned.
    * ``architect-infra`` — task marked ``escalated`` + ``blocked``
      (mirrors the soft-blocker path). The ``blocked_reason`` carries
      the architect's one-line diagnosis with an ``architect_consult:``
      prefix and an ``infrastructure`` flag in the metadata.
    * ``architect-continue`` — retry budget reset; task transitioned to
      ``in_progress`` so the outer loop picks it up again. Resolution's
      guidance is appended to the next developer ``last_issues``.
    """
    from orchestrator.corrective_parser import parse_corrective_direction

    # 1) Bump the counter under lock + emit the audit ledger op so the
    # ladder's one-shot accounting holds across crash / replay.
    await orch.plan_manager.increment_architect_consult(task.id)

    # 2) Invoke the architect in consult mode.
    arch_resolution = await _escalate_stuck_to_architect(
        orch,
        task,
        stuck_state=stuck_state,
        ladder_step="ARCHITECT_CONSULT",
        recent_evidence=(
            web_context_block + reason if web_context_block else reason
        ),
        prior_attempts=prior_attempts,
    )

    # 3) Always log the architect_consult ledger op, regardless of the
    # action taken — operators want a single grep target for "did the
    # consult fire on this task?".
    try:
        await orch.plan_manager.ledger_append(
            op="architect_consult",
            payload={
                "task_id": task.id,
                "reason": reason,
                "action": arch_resolution.action,
                "architect_response_excerpt": (arch_resolution.guidance or "")[:500],
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.ledger_append_failed",
            op="architect_consult",
            err=str(exc),
        )

    # 4) Branch on the resolution action.
    if arch_resolution.action == "architect-refine":
        # Inject corrective sub-tasks via the existing pipeline.
        phase_id = task.phase_id
        try:
            plan = await orch.plan_manager.load()
            base_task_count = 0
            if plan is not None:
                phase = next((p for p in plan.phases if p.id == phase_id), None)
                if phase is not None:
                    base_task_count = len(phase.tasks)
            corrective_tasks = parse_corrective_direction(
                arch_resolution.guidance,
                phase_id=phase_id,
                base_task_count=base_task_count,
                phase_complexity=task.complexity,
            )
            if corrective_tasks:
                await orch.plan_manager.append_corrective_tasks(
                    phase_id, corrective_tasks
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.architect_refine_inject_failed",
                task_id=task.id,
                err=str(exc),
            )
        # Mark the failing task as ``skipped`` so the executor moves on
        # to the corrective sub-tasks. (``superseded`` is not a valid
        # TaskStatus — we use ``skipped`` with a typed metadata reason
        # per the v0.26.1 plan's risk mitigation.)
        try:
            return await orch.plan_manager.update_task_status(
                task.id,
                "skipped",
                meta={
                    "blocked_reason": (
                        f"architect_consult: refined into corrective sub-tasks "
                        f"(see phase {phase_id})"
                    ),
                    "architect_consult_action": "refine",
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.architect_refine_skip_failed",
                task_id=task.id,
                err=str(exc),
            )
            return task

    if arch_resolution.action == "architect-infra":
        # Mark escalated + blocked with the typed infra flag. Mirrors
        # the SOFT_BLOCKER outcome path so downstream operators see a
        # consistent shape, with the ``escalated_infra`` marker in
        # metadata distinguishing the architect-diagnosed case from the
        # plain soft-blocker.
        await orch.plan_manager.mark_escalated(task.id)
        diagnosis = arch_resolution.guidance or "architect diagnosed infrastructure failure"
        return await orch.plan_manager.update_task_status(
            task.id,
            "blocked",
            meta={
                "blocked_reason": f"architect_consult: infrastructure: {diagnosis}",
                "architect_consult_action": "infrastructure",
                "escalated_infra": True,
            },
        )

    if arch_resolution.action == "architect-continue":
        # Reset the developer's retry budget once and put the task back
        # into ``in_progress`` so the outer loop picks it up. The
        # ``update_task_status`` meta carries the retry reset; the
        # caller's ``last_issues`` is appended on the next iteration.
        target_status = "in_progress" if task.status != "in_progress" else task.status
        await orch.plan_manager.update_task_status(
            task.id,
            target_status,
            meta={
                "retry_count": 0,
                "architect_consult_action": "continue",
            },
        )
        return await orch.plan_manager.get_task(task.id) or task

    # Defensive default — should be unreachable given the parser's
    # exhaustive fallback. Treat as infrastructure so the operator sees
    # the architect's raw output and can act.
    return await orch.plan_manager.update_task_status(
        task.id,
        "blocked",
        meta={
            "blocked_reason": (
                f"architect_consult: unparseable response — {(arch_resolution.guidance or '')[:200]}"
            ),
            "architect_consult_action": "unparseable",
        },
    )


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
    # v0.25.1 Bug #2: persistent integration. Commit per task so the next
    # task's per-task worktree (created at HEAD) sees prior tasks' changes.
    commit_msg = f"autodev: task {task.id} ({task.title})"
    while True:
        try:
            await worktree_mgr.apply_patch_to_main(
                worktree, base_ref="HEAD", commit_message=commit_msg
            )
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
                        worktree,
                        base_ref="HEAD",
                        three_way=True,
                        commit_message=commit_msg,
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
    # v0.26.0: the v0.25.4 InlineAdapter+tournaments preflight check was
    # removed alongside InlineAdapter itself — no inline adapter exists,
    # so no mismatch is possible.
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

    # v0.22.2 B3: reconcile evidence-vs-ledger BEFORE the reaper so any
    # orphan evidence (success=true on disk but no coded ledger op) is
    # promoted to coded first — preventing the reaper from reverting a
    # task whose work actually completed (D-3 finding from the
    # 2026-05-09 Unity stall).
    try:
        await orch.plan_manager.reconcile_evidence_vs_ledger()
    except Exception as exc:  # noqa: BLE001 — log + continue
        logger.warning(
            "execute_phase.reconcile_evidence_failed", err=str(exc)
        )

    # v0.22.2 B1: reap orphan in-flight tasks before any dispatch. An
    # interrupted run leaves tasks frozen in non-terminal-non-pending
    # states (``coded``, ``in_progress``, ``reviewed``, etc.) — the
    # dispatcher's pending-only filter cannot pick them up. The reaper
    # reverts orphans to ``pending`` so they re-dispatch fresh. Idempotent
    # (no-op when there are no orphans). After B3 reconciliation runs
    # first, only genuine orphans (no evidence) reach this path.
    try:
        reaped = await orch.plan_manager.reap_orphans()
        if reaped:
            logger.info(
                "execute_phase.reaped_orphans",
                count=len(reaped),
                task_ids=reaped,
            )
    except Exception as exc:  # noqa: BLE001 — log + continue; do not block run
        logger.warning("execute_phase.reap_orphans_failed", err=str(exc))

    # v0.11.0: DAG-aware worker pool over all phases.
    plan = await orch.plan_manager.load()
    if plan is None:
        return processed

    # Resolve worker-pool cap once per run.
    parallelism = _resolve_execute_parallelism(orch)

    # Build a worktree manager rooted under the autodev root. Skip
    # worktree-isolation when the repo is not git-initialized — tests
    # commonly use bare tmp dirs and the legacy serial path applies.
    #
    # v0.21.0 A1: when ``cfg.worktree_pool_enabled`` is True, substitute
    # a :class:`WorktreePool` for the :class:`WorktreeManager`. The pool
    # implements the same ``create_per_task`` / ``remove_per_task`` /
    # ``get_diff_vs_base`` / ``apply_patch_to_main`` surface so the
    # worker (``_execute_one``) is unaware of the substitution. Cold-
    # start happens here so the upfront cost is amortized over the
    # entire phase.
    worktree_mgr: WorktreeManager | None = None
    if _is_git_repo(orch.cwd):
        if getattr(orch.cfg, "worktree_pool_enabled", False):
            from orchestrator.worktree_pool import WorktreePool

            pool = WorktreePool(
                main_repo=orch.cwd,
                pool_dir=autodev_root(orch.cwd) / "execute_worktrees_pool",
                size=parallelism,
            )
            try:
                await pool.cold_start()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.worktree_pool_cold_start_failed",
                    err=str(exc),
                )
            # Duck-type as WorktreeManager (it exposes the same surface).
            worktree_mgr = pool  # type: ignore[assignment]
        else:
            # v0.22.1 A3 + v0.23.0 C1: huge-repo mode resolution.
            # ``"auto"`` keys off ``RepoCapacity.is_huge``; ``"on"``/``"off"``
            # are operator overrides. When huge mode is on we extend the
            # worktree-add timeout (was the bug from 2026-05-09 Unity:
            # 60 s ceiling killed 80-180 s full checkouts).
            _is_huge = bool(
                getattr(getattr(orch, "_repo_capacity", None), "is_huge", False)
            )
            _huge_mode_cfg = getattr(
                orch.cfg, "worktree_huge_repo_mode", "auto"
            )
            if _huge_mode_cfg == "on":
                _wm_huge_mode = True
            elif _huge_mode_cfg == "off":
                _wm_huge_mode = False
            else:  # "auto"
                _wm_huge_mode = _is_huge
            _huge_create_timeout_s = float(
                getattr(orch.cfg, "worktree_huge_create_timeout_s", 600)
            )
            worktree_mgr = WorktreeManager(
                main_repo=orch.cwd,
                tournament_dir=autodev_root(orch.cwd) / "execute_worktrees",
                huge_mode=_wm_huge_mode,
                huge_create_timeout_s=_huge_create_timeout_s,
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

        # v0.21.0 B1: when cross-phase parallelism is enabled, ``Task.
        # depends_on`` may legitimately reference tasks in EARLIER phases.
        # The per-phase DAG validator rejects those as "undefined
        # references", so under the cross-phase flag we run a relaxed
        # plan-level DAG check that accepts cross-phase deps as long as
        # every dep resolves SOMEWHERE in the plan.
        if getattr(orch.cfg, "cross_phase_parallelism_enabled", False):
            try:
                _validate_cross_phase_dag(plan)
            except DagValidationError as exc:
                logger.warning(
                    "execute_phase.cross_phase_dag_invalid", err=str(exc)
                )
                for phase in plan.phases:
                    for t in phase.tasks:
                        if t.status == "pending":
                            try:
                                await orch.plan_manager.update_task_status(
                                    t.id,
                                    "blocked",
                                    meta={
                                        "blocked_reason": (
                                            f"cross_phase_dag_invalid: {exc}"
                                        )
                                    },
                                )
                            except Exception:  # noqa: BLE001
                                pass
                return processed
        else:
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

        # v0.21.0 B1: cross-phase parallelism. When enabled, run a
        # single dispatcher across all phases — tasks from phase N+1
        # may begin executing while phase N's tail is in-flight,
        # provided their deps are terminal AND their files don't
        # conflict. Phase-review still fires per-phase as each phase
        # observes all-terminal, using ``end_checkpoint_commit`` for
        # diff isolation.
        if getattr(orch.cfg, "cross_phase_parallelism_enabled", False):
            for phase in plan.phases:
                await _maybe_record_phase_entry(orch, phase.id)
            cross_processed = await _execute_cross_phase_dag(
                orch, worktree_mgr, parallelism
            )
            processed.extend(cross_processed)
        else:
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
            stuck_task_ids: list[str] = []
            plan = await orch.plan_manager.load()
            if plan is not None:
                phase_obj = next(
                    (p for p in plan.phases if p.id == phase_id), None
                )
                if phase_obj is not None:
                    phase_has_pending = any(
                        t.status == "pending" for t in phase_obj.tasks
                    )
                    # v0.22.2 B2: collect non-terminal non-pending task IDs for
                    # the PhaseStuckError surface.
                    stuck_task_ids = [
                        t.id
                        for t in phase_obj.tasks
                        if t.status not in _TERMINAL_TASK_STATUSES
                        and t.status != "pending"
                    ]
            if not phase_has_pending:
                # v0.22.2 B2: if non-terminal non-pending tasks exist, this is
                # a wedged FSM (typical cause: an interrupted run that left
                # tasks in ``coded``/``in_progress``). Pre-B2 this returned
                # silently and the dispatcher reported success. Now we raise
                # PhaseStuckError so the operator sees the stuck tasks. If
                # everything is genuinely terminal, the legacy clean return
                # still applies.
                if stuck_task_ids:
                    from errors import PhaseStuckError

                    raise PhaseStuckError(phase_id, stuck_task_ids)
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


def _validate_cross_phase_dag(plan: "Plan") -> None:
    """v0.21.0 B1: relaxed DAG validation accepting cross-phase deps.

    Mirrors :func:`orchestrator.dag.validate_phase_dag` but operates at
    plan-level: every ``Task.depends_on`` id must resolve somewhere in
    the plan (not just within its own phase). Cycle detection runs
    across the unified graph too — a backward dep from phase 1 to
    phase 2 would still count as a cycle.

    Raises :class:`orchestrator.dag.DagValidationError` on:

    * an undefined dep (no task with that id anywhere in the plan)
    * any cycle in the unified DAG
    """
    from orchestrator.dag import DagValidationError

    by_id: dict[str, "Task"] = {}
    for phase in plan.phases:
        for t in phase.tasks:
            by_id[t.id] = t

    # Pass 1: undefined references.
    for tid, t in by_id.items():
        for dep in t.depends_on:
            if dep not in by_id:
                raise DagValidationError(
                    f"task {tid!r} depends_on undefined task {dep!r} "
                    "(plan-level)"
                )

    # Pass 2: cycle detection across the unified graph.
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in by_id}

    def dfs(node: str, path: list[str]) -> None:
        color[node] = GRAY
        path.append(node)
        for dep in by_id[node].depends_on:
            if color[dep] == GRAY:
                start = path.index(dep)
                cycle = " -> ".join(path[start:] + [dep])
                raise DagValidationError(
                    f"cycle detected (plan-level): {cycle}"
                )
            if color[dep] == WHITE:
                dfs(dep, path)
        path.pop()
        color[node] = BLACK

    for tid in by_id:
        if color[tid] == WHITE:
            dfs(tid, [])


async def _execute_cross_phase_dag(
    orch: "Orchestrator",
    worktree_mgr: WorktreeManager | None,
    parallelism: int,
) -> list[Task]:
    """v0.21.0 B1: cross-phase parallel dispatcher.

    Runs a single worker pool over ALL phases of the plan, allowing tasks
    from phase N+1 to begin executing while phase N's tail is still
    in-flight, subject to:

    * dependencies in :attr:`Task.depends_on` are all in a terminal state
      (the existing :meth:`PlanManager.next_pending_tasks` already enforces
      this and is unchanged), AND
    * the new task's files don't intersect any in-flight task's files
      (also enforced by :meth:`next_pending_tasks` via ``exclude_files``).

    Phase-review fires per-phase as each phase observes all-terminal,
    using :func:`_maybe_run_phase_review` and :func:`_maybe_record_phase_checkpoint`
    so the diff range honors the captured ``end_checkpoint_commit``
    rather than live HEAD (preserves correctness when phase N+1 is
    landing commits during phase N's review).

    Termination: every task across every phase is in a terminal state
    AND no workers are in-flight. Then any phases without phase-review
    completion are walked once more so corrective tasks (if any) get
    a chance to run.
    """
    in_flight: dict[str, asyncio.Task[Task]] = {}
    in_flight_phase_id: dict[str, str] = {}  # task_id → phase_id
    processed: list[Task] = []

    # v0.21.0 B2: speculative-execution bookkeeping. Maps speculative
    # task_id → parent task_id so the rollback path can find the
    # parent on failure. Capped at 1 active speculative task per
    # phase per the v0.21.0 plan.
    speculative_parents: dict[str, str] = {}
    speculative_phase: set[str] = set()  # phases with active speculative
    speculative_enabled = getattr(
        orch.cfg, "speculative_execution_enabled", False
    )

    async def _all_terminal_across_plan() -> bool:
        plan = await orch.plan_manager.load()
        if plan is None:
            return True
        for phase in plan.phases:
            for t in phase.tasks:
                if t.status not in _TERMINAL_TASK_STATUSES:
                    return False
        return True

    while True:
        if not in_flight and await _all_terminal_across_plan():
            # Fire phase-reviews for every phase that's now terminal.
            plan = await orch.plan_manager.load()
            if plan is not None:
                for phase in plan.phases:
                    await _maybe_run_phase_review(orch, phase.id)
            return processed

        # Spawn workers up to parallelism cap. The dispatcher honors
        # the existing file-overlap and depends-on guards, but does NOT
        # restrict by phase_id (the cross-phase contract).
        if len(in_flight) < parallelism:
            slots = parallelism - len(in_flight)
            excluded = await orch.plan_manager.in_flight_files()
            tasks_to_run = await orch.plan_manager.next_pending_tasks(
                limit=slots, exclude_files=excluded
            )
            for t in tasks_to_run:
                if t.id in in_flight:
                    continue
                await orch.plan_manager.mark_in_flight(t.id)
                in_flight[t.id] = asyncio.create_task(
                    _execute_one_worker(orch, t, worktree_mgr)
                )
                in_flight_phase_id[t.id] = t.phase_id

            # v0.21.0 B2: after dispatching pending tasks, opportunistically
            # speculate ONE child task per phase whose parent is in-flight.
            # Conditions are fully enforced by ``speculable_candidate``;
            # we just gate at "max 1 speculative per phase" here.
            if speculative_enabled and len(in_flight) < parallelism:
                # Pick one in-flight task that doesn't already have a
                # speculative child running in its phase.
                for parent_id in list(in_flight.keys()):
                    parent_phase = in_flight_phase_id.get(parent_id, "")
                    if parent_phase in speculative_phase:
                        continue
                    candidate = await orch.plan_manager.speculable_candidate(
                        parent_id
                    )
                    if candidate is None:
                        continue
                    if candidate.id in in_flight:
                        continue
                    await orch.plan_manager.mark_in_flight(candidate.id)
                    await orch.plan_manager.ledger_append(
                        op="speculative_started",
                        payload={
                            "task_id": candidate.id,
                            "parent_task_id": parent_id,
                        },
                    )
                    in_flight[candidate.id] = asyncio.create_task(
                        _execute_one_worker(orch, candidate, worktree_mgr)
                    )
                    in_flight_phase_id[candidate.id] = candidate.phase_id
                    speculative_parents[candidate.id] = parent_id
                    speculative_phase.add(candidate.phase_id)
                    logger.info(
                        "execute_phase.speculative_started",
                        task_id=candidate.id,
                        parent_task_id=parent_id,
                    )
                    break  # max 1 speculative per polling round

        if not in_flight:
            # Nothing dispatchable but not all terminal — likely waiting
            # on a different worker; brief sleep then re-poll.
            if await _all_terminal_across_plan():
                continue
            await asyncio.sleep(0.05)
            continue

        # Wait for any worker to finish.
        done, _pending = await asyncio.wait(
            list(in_flight.values()), return_when=asyncio.FIRST_COMPLETED
        )
        finished_phases: set[str] = set()
        # v0.21.0 B2: track failed parents so we can roll back any
        # speculative children that depended on them in this round.
        failed_parents: set[str] = set()
        for d in done:
            finished_id = next(
                (tid for tid, h in in_flight.items() if h is d), None
            )
            if finished_id is None:
                continue
            finished_phase_id = in_flight_phase_id.pop(finished_id, "")
            del in_flight[finished_id]
            await orch.plan_manager.clear_in_flight(finished_id)
            # If this was a speculative task, pop its bookkeeping.
            if finished_id in speculative_parents:
                speculative_parents.pop(finished_id, None)
                speculative_phase.discard(finished_phase_id)
            try:
                completed_task = d.result()
                processed.append(completed_task)
                # If this task was non-speculative AND it failed (status
                # blocked) AND a speculative child was tracked, mark
                # the parent as failed so the rollback runs after the
                # main loop body.
                if (
                    completed_task.status == "blocked"
                    and finished_id not in speculative_parents.values()
                ):
                    pass  # caught by the speculative_parents check below
                if completed_task.status == "blocked":
                    failed_parents.add(finished_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.cross_phase_worker_unhandled_exception",
                    task_id=finished_id,
                    err=str(exc),
                )
                failed_parents.add(finished_id)
                if finished_phase_id:
                    try:
                        await orch.plan_manager.mark_blocked_descendants(
                            finished_phase_id, finished_id, str(exc)
                        )
                    except Exception as exc2:  # noqa: BLE001
                        logger.warning(
                            "execute_phase.cross_phase_cascade_block_failed",
                            phase_id=finished_phase_id,
                            task_id=finished_id,
                            err=str(exc2),
                        )
            if finished_phase_id:
                finished_phases.add(finished_phase_id)

        # v0.21.0 B2: roll back any speculative children whose parents
        # just failed. Walk a copy of speculative_parents so we can
        # mutate the dict mid-iteration.
        if speculative_enabled and failed_parents:
            from orchestrator.speculative import rollback_speculative_task

            for spec_id, parent_id in list(speculative_parents.items()):
                if parent_id not in failed_parents:
                    continue
                # Look up the speculative task.
                plan = await orch.plan_manager.load()
                if plan is None:
                    continue
                spec_task = next(
                    (
                        t
                        for ph in plan.phases
                        for t in ph.tasks
                        if t.id == spec_id
                    ),
                    None,
                )
                if spec_task is None:
                    continue
                # If the speculative task is still in flight here, we
                # can't roll back yet — wait until next round.
                if spec_id in in_flight:
                    continue
                try:
                    await rollback_speculative_task(
                        orch,
                        spec_task,
                        parent_task_id=parent_id,
                        reason="parent_blocked",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.speculative_rollback_failed",
                        task_id=spec_id,
                        err=str(exc),
                    )
                speculative_parents.pop(spec_id, None)
                # Phase may now be free for another speculative attempt.
                phase_id_of_spec = next(
                    (
                        ph.id
                        for ph in plan.phases
                        for t in ph.tasks
                        if t.id == spec_id
                    ),
                    None,
                )
                if phase_id_of_spec is not None:
                    speculative_phase.discard(phase_id_of_spec)

        # After draining, fire phase-review for any phase that just
        # observed all-terminal. ``_maybe_run_phase_review`` is
        # idempotent + race-safe (in_flight count check).
        for phid in finished_phases:
            await _maybe_run_phase_review(orch, phid)


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

    v0.26.0: previously, :class:`DelegationPendingSignal` (raised by
    the inline adapter to suspend a run pending an external response)
    was re-raised here so the caller could persist suspend state. With
    InlineAdapter gone the special-case is gone; every exception is
    routed through the cascade-block path.

    v0.26.1 patch E: exceptions are classified by ``isinstance`` so the
    ``blocked_reason`` prefix carries typed semantics (encoding error vs
    IO error vs timeout vs generic). The full traceback is persisted to
    ``.autodev/debug/worker-exception-<task>-<ts>.txt`` for operator
    diagnosis.
    """
    try:
        return await _execute_one(orch, task, worktree_mgr)
    except Exception as exc:  # noqa: BLE001
        import asyncio
        import traceback

        from state.paths import debug_dir

        if isinstance(exc, UnicodeDecodeError):
            prefix = "qa_gate_encoding_error"
        elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            prefix = "qa_gate_timeout"
        elif isinstance(exc, OSError):
            prefix = "qa_gate_io_error"
        else:
            prefix = "worker_exception"
        blocked_reason = f"{prefix}: {exc}"

        # Persist traceback for operator diagnosis. Failure to write is
        # logged but never re-raised — the block path is already a
        # degraded-mode return.
        try:
            dbg = debug_dir(orch.cwd)
            dbg.mkdir(parents=True, exist_ok=True)
            ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            safe_id = task.id.replace("/", "_").replace(" ", "_")
            tb_path = dbg / f"worker-exception-{safe_id}-{ts}.txt"
            tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            tb_path.write_text(tb_text, encoding="utf-8", errors="replace")
        except Exception as exc_tb:  # noqa: BLE001
            logger.warning(
                "execute_phase.worker_traceback_write_failed",
                task_id=task.id,
                err=str(exc_tb),
            )

        logger.warning(
            "execute_phase.worker_caught_exception",
            task_id=task.id,
            prefix=prefix,
            err=str(exc),
        )
        try:
            blocked = await orch.plan_manager.update_task_status(
                task.id,
                "blocked",
                meta={"blocked_reason": blocked_reason},
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


async def _maybe_record_phase_checkpoint(
    orch: "Orchestrator", phase_id: str
) -> None:
    """v0.21.0 B1: capture ``Phase.end_checkpoint_commit`` once at phase
    completion.

    Idempotent: phases already carrying an end_checkpoint_commit are
    skipped. This SHA is the ``tip_commit`` for the phase-review
    tournament's diff range — captured at the moment ALL tasks in the
    phase reach a terminal state. Critically, this happens BEFORE the
    next phase's tasks start landing commits (which is possible under
    cross-phase parallelism), so the diff window is phase-isolated.

    Reads the latest plan; if every task in the phase is terminal AND
    end_checkpoint_commit is unset, captures HEAD and persists.
    """
    plan = await orch.plan_manager.load()
    if plan is None:
        return
    phase = next((p for p in plan.phases if p.id == phase_id), None)
    if phase is None or phase.end_checkpoint_commit is not None:
        return
    if not _all_phase_tasks_terminal(phase):
        return
    sha = _git_rev_parse_head(orch.cwd)
    if sha is None:
        logger.info(
            "execute_phase.end_checkpoint_unavailable", phase_id=phase_id
        )
        return
    try:
        await orch.plan_manager.update_phase_meta(
            phase_id, end_checkpoint_commit=sha
        )
        logger.info(
            "execute_phase.end_checkpoint_captured",
            phase_id=phase_id,
            sha=sha[:12],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.end_checkpoint_capture_failed",
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

    # v0.21.0 B1: capture end_checkpoint_commit at the moment we observe
    # all tasks terminal — BEFORE any concurrent next-phase tasks land
    # commits. Idempotent: re-entry with end_checkpoint already set is a
    # no-op. The captured SHA is read by the phase-review runner as
    # tip_commit so the diff range is phase-isolated.
    await _maybe_record_phase_checkpoint(orch, phase_id)

    # Re-load to pick up the captured checkpoint (used downstream).
    plan = await orch.plan_manager.load()
    if plan is None:
        return
    phase = next((p for p in plan.phases if p.id == phase_id), None)
    if phase is None:
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
    # v0.21.0 B1: prefer the captured end-checkpoint SHA (set in
    # ``_maybe_record_phase_checkpoint``) so the diff range is phase-
    # isolated even when cross-phase parallelism is active. Falls back
    # to live HEAD when the checkpoint isn't recorded (e.g. legacy
    # plans, pre-v0.21.0 phases that never executed under the new
    # capture path).
    tip_commit = phase.end_checkpoint_commit or _git_rev_parse_head(orch.cwd) or ""
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
        # v0.23.0 C1: sparse-by-default on huge repos. The legacy
        # ``worktree_sparse_checkout_enabled`` flag still works as an
        # explicit operator opt-in for non-huge repos. On huge repos
        # (when ``worktree_huge_repo_mode`` resolves on) sparse becomes
        # the default unless explicitly disabled with ``"off"``.
        _sparse_enabled = bool(orch.cfg.worktree_sparse_checkout_enabled)
        _huge_mode_cfg_for_sparse = getattr(
            orch.cfg, "worktree_huge_repo_mode", "auto"
        )
        _is_huge_for_sparse = bool(
            getattr(getattr(orch, "_repo_capacity", None), "is_huge", False)
        )
        if _huge_mode_cfg_for_sparse == "on" or (
            _huge_mode_cfg_for_sparse == "auto" and _is_huge_for_sparse
        ):
            _sparse_enabled = True
        sparse_paths: list[str] | None = None
        if _sparse_enabled:
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
                # v0.22.2 B3: emit a pre-flight marker BEFORE the developer
                # dispatch so resume can detect "evidence written but
                # ``coded`` op missing" (a process crash between
                # ``write_evidence`` at line 1771 and
                # ``update_task_status("coded")`` at line 1818). Audit-only
                # — does NOT mutate plan state.
                try:
                    from state.ledger import append_entry as _append_entry
                    from state.lockfile import plan_lock as _plan_lock

                    async with _plan_lock(
                        orch.cwd,
                        timeout_s=getattr(
                            orch.plan_manager, "_lock_timeout_s", 30.0
                        ),
                    ):
                        await _append_entry(
                            orch.cwd,
                            op="attempt_started",
                            payload={
                                "task_id": task.id,
                                "attempt_n": task.retry_count,
                                "started_at": _dt.datetime.now(
                                    _dt.timezone.utc
                                ).isoformat(),
                                "session_id": getattr(
                                    orch.plan_manager, "_session_id", ""
                                ),
                            },
                            session_id=getattr(
                                orch.plan_manager, "_session_id", ""
                            ),
                        )
                except Exception as exc:  # noqa: BLE001 — best-effort marker
                    logger.debug(
                        "execute_phase.attempt_started_emit_failed",
                        task_id=task.id,
                        err=str(exc),
                    )
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
                # v0.20.0 C3: dynamic scope expansion on missing-file
                # error. Inspect the adapter output for paths that look
                # like sparse-checkout misses; if any are covered by the
                # task's extended_scope / phase / plan edit_scope, widen
                # the worktree and retry once. This is a one-shot
                # repair — repeat misses fall through to normal retry.
                if (
                    worktree is not None
                    and worktree_mgr is not None
                    and not task.metadata.get("dynamic_scope_expansion_used")
                ):
                    expanded = await _maybe_expand_sparse_for_missing(
                        orch=orch,
                        task=task,
                        worktree=worktree,
                        worktree_mgr=worktree_mgr,
                        adapter_text=(
                            (developer_result.text or "")
                            + "\n"
                            + (developer_result.error or "")
                        ),
                    )
                    if expanded:
                        # Mark + retry once.
                        task.metadata["dynamic_scope_expansion_used"] = True
                        last_issues = [
                            developer_result.error or "missing-file; sparse-expanded"
                        ]
                        continue
                task = await _try_retry_or_escalate(
                    orch,
                    task,
                    retry_limit,
                    reason=_build_adapter_failure_reason(developer_result),
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
                    run_multi_branch_impl_tournament,
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
                # v0.21.0 A2: opt into the multi-branch impl-tournament fan-
                # out when ``cfg.tournaments.impl.num_branches > 1`` OR the
                # operator pinned a non-None ``branches`` list. Keeps the
                # single-branch path byte-identical when neither is set.
                _impl_cfg = orch.cfg.tournaments.impl
                _impl_branch_configs = (
                    list(_impl_cfg.branches) if _impl_cfg.branches else None
                )
                _impl_n_branches = (
                    len(_impl_branch_configs)
                    if _impl_branch_configs is not None
                    else _impl_cfg.num_branches
                )
                try:
                    if _impl_n_branches > 1:
                        try:
                            await run_multi_branch_impl_tournament(
                                orch,
                                task,
                                _initial_bundle,
                                n_branches=_impl_n_branches,
                                branch_configs=_impl_branch_configs,
                            )
                        except TournamentError as _mb_exc:
                            # Survivor floor or other fan-out failure —
                            # fall back to the single-branch path so the
                            # task still gets tournament refinement.
                            logger.warning(
                                "execute_phase.multi_branch_impl_fallback",
                                task_id=task.id,
                                err=str(_mb_exc),
                            )
                            await run_impl_tournament(
                                orch, task, _initial_bundle
                            )
                    else:
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


async def _maybe_expand_sparse_for_missing(
    orch: "Orchestrator",
    task: Task,
    worktree: Path,
    worktree_mgr: WorktreeManager,
    adapter_text: str,
) -> bool:
    """v0.20.0 C3: parse missing-file errors and dynamically widen sparse-checkout.

    Returns True iff at least one path was admitted via
    :meth:`WorktreeManager.expand_sparse_paths`. Returns False when:

    * no missing-file paths are detected in ``adapter_text``;
    * none of the detected paths fall under the task's
      ``extended_scope`` / phase ``edit_scope`` / plan ``edit_scope`` —
      we never widen beyond the architect-declared envelope.
    """
    from orchestrator.dag import is_in_scope
    from orchestrator.worktree import detect_missing_paths

    missing = detect_missing_paths(adapter_text)
    if not missing:
        return False
    # Resolve the broadest legitimate scope: extended_scope ∪ phase
    # edit_scope ∪ plan edit_scope. If a missing path is covered by
    # any of these, admit it.
    scope: list[str] = list(task.extended_scope or [])
    if orch.plan_manager is not None:
        try:
            plan = await orch.plan_manager.load()
        except Exception:  # noqa: BLE001
            plan = None
        if plan is not None:
            scope.extend(plan.edit_scope or [])
            for ph in plan.phases:
                if ph.id == task.phase_id and ph.edit_scope is not None:
                    scope.extend(ph.edit_scope)
                    break
    if not scope:
        # No scope to honor → skip dynamic expansion (safety: never
        # blindly admit arbitrary missing paths).
        return False
    to_admit: list[str] = []
    for path in missing:
        if is_in_scope(path, scope):
            # Admit the parent directory (sparse-checkout takes prefixes;
            # adding the file directly works under cone-mode but adding
            # the parent is more idempotent / future-friendly).
            parent = "/".join(path.split("/")[:-1])
            if parent and parent not in to_admit:
                to_admit.append(parent)
            elif not parent and path not in to_admit:
                to_admit.append(path)
    if not to_admit:
        return False
    try:
        await worktree_mgr.expand_sparse_paths(worktree, to_admit)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.dynamic_scope_expand_failed",
            task_id=task.id,
            err=str(exc),
        )
        return False
    logger.info(
        "execute_phase.dynamic_scope_expanded",
        task_id=task.id,
        admitted=to_admit,
    )
    if orch.plan_manager is not None:
        try:
            await orch.plan_manager.ledger_append(
                op="sparse_checkout_expanded",
                payload={
                    "task_id": task.id,
                    "missing_paths": missing,
                    "admitted_prefixes": to_admit,
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return True


async def _enforce_retry_backoff(
    last_retry_at: str | None,
    min_interval_s: float,
    *,
    now: Callable[[], "datetime"] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> float:
    """v0.25.1 Bug #4: pause until the retry interval has elapsed.

    Returns the number of seconds actually slept (``0.0`` when no wait
    was required). ``now`` and ``sleep`` are injectable so tests can
    drive the helper deterministically.

    The guard is a no-op when:

    * ``last_retry_at`` is ``None`` (first retry of the task);
    * ``min_interval_s <= 0`` (operator opted out);
    * the elapsed time since ``last_retry_at`` already exceeds the
      interval (legitimate slow retry, e.g. resume after a long pause).

    Otherwise the helper sleeps for ``min_interval_s - elapsed`` so the
    next dispatch lands at the earliest permissible instant. This stops
    the resume-loop pathology where a task wedged at ``retry_count=N``
    burns through ``N+1, N+2, …`` within milliseconds (sub-second
    sequences observed in the unity run).
    """
    import asyncio
    from datetime import datetime, timezone

    if last_retry_at is None or min_interval_s <= 0:
        return 0.0
    try:
        last_dt = datetime.fromisoformat(last_retry_at)
    except (ValueError, TypeError):
        # Malformed timestamp on disk — fail open rather than block
        # forever. Subsequent mark_task_retry will overwrite with a
        # well-formed value.
        return 0.0
    now_fn = now if now is not None else lambda: datetime.now(timezone.utc)
    sleep_fn = sleep if sleep is not None else asyncio.sleep
    elapsed = (now_fn() - last_dt).total_seconds()
    if elapsed >= min_interval_s:
        return 0.0
    wait = min_interval_s - elapsed
    await sleep_fn(wait)
    return wait


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

    # v0.25.1 Bug #4: enforce the configured minimum retry interval
    # BEFORE any state mutation. Stops the resume-loop pathology where
    # a task wedged at retry_count=N burns through N+1, N+2, ... within
    # milliseconds. Idempotent — re-entering with a fresh ``last_retry_at``
    # naturally short-circuits via the elapsed-time check.
    min_interval = getattr(orch.cfg, "qa_retry_min_interval_s", 30.0)
    waited = await _enforce_retry_backoff(task.last_retry_at, min_interval)
    if waited > 0:
        logger.info(
            "execute_phase.retry_backoff_enforced",
            task_id=task.id,
            waited_s=waited,
            min_interval_s=min_interval,
            last_retry_at=task.last_retry_at,
        )

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

        # v0.26.1 patch G: ARCHITECT_CONSULT rung — re-delegate to
        # architect_b in consult mode. Branches before the regular
        # critic dispatch because the response format + action set is
        # distinct (refine-tasks / infrastructure / continue).
        if step == "ARCHITECT_CONSULT":
            arch_resolution = await _dispatch_architect_consult(
                orch,
                task,
                stuck_state=stuck_state,
                reason=reason,
                prior_attempts=prior_attempts,
                web_context_block=web_context_block,
            )
            if arch_resolution is not None:
                return arch_resolution

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

    v0.26.0: InlineAdapter's suspend/resume special-cases (response-file
    shortcut on the resume path, ``write_suspend_state`` on the
    ``DelegationPendingSignal`` exit path) were removed. Every adapter
    is now a subprocess adapter and the dispatch is a straight call.

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

        # v0.20.0 A1: optional LLM-augmentation. When ``cfg.prm.strategy``
        # is ``"rules+ml"`` AND the orchestrator carries an
        # ``llm_trajectory_classifier`` attribute, run the classifier and
        # merge its patterns with the rule-based ones. Rules win on dedup;
        # graceful degradation on any error (already implemented inside
        # :class:`LLMTrajectoryClassifier`).
        prm_strategy = getattr(
            getattr(orch.cfg, "prm", None), "strategy", "rules"
        )
        ml_clf = getattr(orch, "llm_trajectory_classifier", None)
        if prm_strategy == "rules+ml" and ml_clf is not None:
            try:
                events_for_clf = trajectory_store.events_for(envelope.task_id)
                ml_patterns = await ml_clf.classify(events_for_clf)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.prm_ml_classify_failed",
                    task_id=envelope.task_id,
                    err=str(exc),
                )
                ml_patterns = []
            if ml_patterns:
                from orchestrator.prm import merge_patterns

                patterns = merge_patterns(patterns, ml_patterns)
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
        # v0.20.0 D1: thread per-bucket huge-repo multiplier overrides
        # through to the resolver. Default ``None`` → resolver uses
        # baked-in per-bucket curves (simple 3.0×, medium 2.0×, complex 1.5×).
        # ``getattr`` on the cfg attribute is defensive — orchestrator stubs
        # in some unit tests omit ``task_overrides`` entirely.
        _task_overrides_cfg = getattr(orch.cfg, "task_overrides", None)
        huge_mult_overrides = (
            getattr(_task_overrides_cfg, "huge_repo_multipliers", None)
            if _task_overrides_cfg is not None
            else None
        )
        max_turns = (
            resolve_task_max_turns(
                task,
                spec.max_turns,
                capacity=repo_capacity,
                huge_repo_multipliers=huge_mult_overrides,
            )
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

    orch.guardrails.pre_invocation(envelope.task_id, inv)
    import time as _time

    _t0 = _time.time()
    result = await orch.adapter.execute(inv)
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


def _build_adapter_failure_reason(
    developer_result: AgentResult | None,
) -> str:
    """v0.26.1 patch D: surface ``developer_result.error`` + ``subtype`` in
    the escalation reason so the repetition_loop / stuck-recovery ladder
    sees semantic variation across genuinely-different failures.

    Prior to this patch every coder-adapter failure produced the
    identical string ``"coder adapter failure"`` regardless of the
    underlying cause (``error_max_turns`` vs ``error_max_tokens`` vs
    parser failure vs subprocess crash), so the repetition_loop course-
    correction misfired by matching on the cosmetic symptom.

    The returned reason starts with the legacy literal so any
    case-insensitive matcher still recognises adapter-class failures,
    appends the typed subtype, and the first 200 chars of the adapter's
    own error message.
    """
    base = "coder adapter failure"
    if developer_result is None:
        return f"{base} (unknown)"
    subtype = developer_result.subtype or "unknown"
    raw_error = developer_result.error or "adapter failure"
    truncated = raw_error[:200]
    return f"{base} ({subtype}): {truncated}"


def _files_changed_for_secretscan(
    developer_result: AgentResult | None,
) -> list[Path]:
    """Extract repo-relative paths from a developer ``AgentResult`` for the
    v0.13.0 diff-scoped secretscan.

    v0.26.1 patch C — contract simplified to always return a list:

    * ``[]`` when the result is missing or has no diff text.
    * ``list[Path]`` of repo-relative paths in first-seen, deduped order
      when a parseable diff is present.

    Callers do not need to special-case the empty case; passing
    ``paths=[]`` through to ``run_secretscan`` / ``run_hallucination_guard``
    /``run_mutation_test`` / ``run_code_size`` produces a clean "scan
    nothing, pass the gate" outcome.

    v0.27.0 (audit §6) — caller of last resort: when a non-empty diff
    body has no parseable ``+++ b/`` headers (garbage payload, truncated
    stream, etc.) this helper now raises :class:`errors.DiffParseError`
    so :func:`_run_qa_gates` can fail-closed against tasks declared as
    ``produces_diff=True`` rather than silently scan zero files.
    """
    if developer_result is None or not developer_result.diff:
        return []
    return [
        Path(p)
        for p in extract_files_from_diff(developer_result.diff, strict=True)
    ]


def _surface_warning(task: "Task", gate_name: str, result: GateResult) -> None:
    """Record a warn/info-severity gate result on the task without halting.

    v0.22.0: warn-severity GateResults (passed=True, severity="warn") and
    info-severity findings (passed=False, severity="info") are surfaced
    via the task's ``metadata["qa_warnings"]`` list so downstream
    consumers (evidence ledger, status command, knowledge seeding) can
    pick them up without a separate channel. The list is appended in
    gate-evaluation order; each entry is a small dict with the gate
    name, severity, details snippet, and structured metrics carrier.
    """
    if task.metadata is None:  # pragma: no cover — Task default is dict
        task.metadata = {}
    warnings = task.metadata.setdefault("qa_warnings", [])
    warnings.append(
        {
            "gate": gate_name,
            "severity": result.severity,
            "details": result.details,
            "metrics": result.metrics,
        }
    )


def _run_secretscan_with_cfg(
    cwd: Path, secretscan_paths: list[Path] | None, cfg: object
) -> Awaitable[GateResult]:
    """v0.23.0 C2: bridge that only forwards new C2 kwargs when set.

    Existing tests stub :func:`run_secretscan` with the v0.19.0 signature
    (no C2 kwargs). When the operator hasn't opted into the new fields,
    we omit them from the call so those mocks keep working. Only when
    the cfg explicitly carries any new C2 setting do we thread them
    through (and any test that exercises the C2 surface will mock
    accordingly).
    """
    extra: dict[str, object] = {}
    ignore = getattr(cfg, "secretscan_ignore_paths", None)
    if ignore:
        extra["ignore_paths"] = ignore
    eth = getattr(cfg, "secretscan_entropy_threshold", None)
    if eth is not None:
        extra["entropy_threshold_override"] = eth
    mlen = getattr(cfg, "secretscan_min_entropy_length", None)
    if mlen is not None:
        extra["min_entropy_length"] = mlen
    return run_secretscan(
        cwd,
        paths=secretscan_paths,
        per_extension_thresholds=cfg.secretscan_per_extension_thresholds,
        baseline_enabled=cfg.secretscan_baseline_enabled,
        **extra,
    )


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

    v0.22.0: respects :class:`GateResult` severity. ``passed=False`` with
    the default ``severity="block"`` halts as before (byte-identical to
    v0.21.0). ``passed=True, severity="warn"`` and ``passed=False,
    severity="info"`` are surfaced via :func:`_surface_warning` and the
    gate dispatch continues. Existing gates that don't set ``severity``
    inherit the "block" default — pre-v0.22.0 behavior is preserved.
    """
    from errors import DiffParseError
    from plugins.registry import QAContext

    cfg = orch.cfg.qa_gates
    cwd = orch.cwd
    language = detect_language(cwd)

    # v0.27.0 (audit §6): fail-closed when a diff-producing task ships a
    # malformed diff body. Investigation tasks (``produces_diff=False``)
    # legitimately have no diff — treat their unparseable diff as the
    # legacy ``paths=[]`` no-op so the secretscan / hallucination_guard
    # gates skip cleanly. For everyone else, the gate-set returns a
    # blocking failure detail string before any gate runs.
    try:
        secretscan_paths = _files_changed_for_secretscan(developer_result)
    except DiffParseError as exc:
        if getattr(task, "produces_diff", True) is False:
            secretscan_paths = []
        else:
            return (
                "secretscan: developer diff unparseable, refusing to silently "
                f"skip diff-scoped QA gates ({exc})"
            )
    # v0.16.0: hallucination-guard reuses the diff-scope path list so the
    # AST walk only visits files the executor just touched. ``cfg.hallucination_guard``
    # is a top-level toggle (default True) — see :class:`AutodevConfig`.
    hallucination_guard_enabled = bool(
        getattr(orch.cfg, "hallucination_guard", True)
    )

    # v0.22.1 A2: auto-skip secretscan on huge repos to avoid the
    # false-positive avalanche observed on Unity (27K-50K FPs from asset
    # GUIDs). Operators can force-enable via
    # ``cfg.secretscan_force_run_on_huge_repo``.
    repo_capacity = getattr(orch, "_repo_capacity", None)
    secretscan_huge_skip = bool(
        repo_capacity is not None
        and getattr(repo_capacity, "is_huge", False)
        and getattr(cfg, "secretscan_auto_skip_huge_repo", True)
        and not getattr(cfg, "secretscan_force_run_on_huge_repo", False)
    )
    secretscan_enabled = cfg.secretscan and not secretscan_huge_skip
    if secretscan_huge_skip and cfg.secretscan:
        # Surface the skip so operators see it in the orchestrator log
        # without having to dig into config. Mirrors the pattern of
        # :func:`_surface_warning` used elsewhere for soft gate events.
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "qa.secretscan.auto_skipped_huge_repo file_count=%s total_bytes=%s; "
            "set cfg.qa_gates.secretscan_force_run_on_huge_repo=True to override",
            getattr(repo_capacity, "file_count", "?"),
            getattr(repo_capacity, "total_bytes", "?"),
        )

    # v0.22.0: gates are ``(name, enabled, callable)`` triples so the
    # warn-surface helper can attribute findings to a gate.
    gates: list[tuple[str, bool, Callable[[], Awaitable[GateResult]]]] = [
        ("syntax_check", cfg.syntax_check, lambda: run_syntax_check(cwd, language)),
        ("lint", cfg.lint, lambda: run_lint(cwd, language)),
        ("build_check", cfg.build_check, lambda: run_build_check(cwd, language)),
        ("test_runner", cfg.test_runner, lambda: run_tests(cwd)),
        (
            "secretscan",
            secretscan_enabled,
            lambda: _run_secretscan_with_cfg(
                cwd,
                secretscan_paths,
                cfg,
            ),
        ),
        (
            "hallucination_guard",
            hallucination_guard_enabled,
            lambda: run_hallucination_guard(
                cwd,
                paths=secretscan_paths,
                extra_skip_dirs=getattr(
                    cfg, "hallucination_guard_skip_dirs", None
                ),
            ),
        ),
        (
            "mutation_test",
            cfg.mutation_test_enabled,
            lambda: run_mutation_test(
                cwd,
                paths=secretscan_paths,
                kill_rate_threshold=cfg.mutation_test_threshold,
            ),
        ),
        (
            "code_size",
            getattr(cfg, "code_size", False),
            lambda: run_code_size(
                cwd,
                paths=secretscan_paths,
                thresholds=getattr(cfg, "code_size_thresholds", None),
                baseline_enabled=getattr(
                    cfg, "code_size_baseline_enabled", False
                ),
            ),
        ),
    ]

    for name, enabled, gate_fn in gates:
        if not enabled:
            continue
        result: GateResult = await gate_fn()
        # v0.22.0 severity dispatch:
        #   * passed=False AND severity=="block" (legacy default) → halt.
        #   * passed=True AND severity=="warn" → surface as warning, continue.
        #   * passed=False AND severity=="info" → surface as info, continue.
        #   * other combos (passed=True silent, etc.) → no-op.
        severity = getattr(result, "severity", "block")
        if not result.passed and severity == "block":
            return result.details or "QA gate failed"
        if (result.passed and severity == "warn") or (
            not result.passed and severity == "info"
        ):
            _surface_warning(task, name, result)
            logger.info(
                "execute_phase.qa_gate_warning",
                task_id=task.id,
                gate=name,
                severity=severity,
                details=(result.details or "")[:200],
            )

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
            severity = getattr(plugin_result, "severity", "block")
            if not plugin_result.passed and severity == "block":
                return plugin_result.details or f"plugin gate '{plugin.name}' failed"
            if (plugin_result.passed and severity == "warn") or (
                not plugin_result.passed and severity == "info"
            ):
                _surface_warning(task, f"plugin:{plugin.name}", plugin_result)

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
