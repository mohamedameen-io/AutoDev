"""Execute-phase loop.

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

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal, cast

from adapters.inline import InlineAdapter
from adapters.inline_types import DelegationPendingSignal
from adapters.types import AgentInvocation, AgentResult
from errors import AutodevError, GuardrailExceededError
from autologging import get_logger
from orchestrator.delegation_envelope import DelegationEnvelope
from orchestrator.inline_state import write_suspend_state
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
from state.schemas import (
    CoderEvidence,
    CriticEvidence,
    ReviewEvidence,
    Task,
    TestEvidence,
)


if TYPE_CHECKING:
    from orchestrator import Orchestrator


logger = get_logger(__name__)


class TaskEscalatedError(AutodevError):
    """Raised (and logged) when a task is escalated to critic_sounding_board.

    The execute loop catches this internally — it is surfaced to the CLI
    only when the user explicitly targets a task that ends up escalated.
    """


async def run_execute_phase(
    orch: "Orchestrator", task_id: str | None = None
) -> list[Task]:
    """Run the execute loop. Returns the list of tasks processed (in order)."""
    processed: list[Task] = []

    if task_id is not None:
        task = await orch.plan_manager.get_task(task_id)
        if task is None:
            raise AutodevError(f"task_id={task_id!r} not found in plan")
        if task.status in ("complete", "skipped"):
            logger.info("execute_phase.skip", task_id=task_id, status=task.status)
            return processed
        processed.append(await _execute_one(orch, task))
        return processed

    # Loop over all pending tasks.
    while True:
        task = await orch.plan_manager.next_pending_task()
        if task is None:
            break
        processed.append(await _execute_one(orch, task))
    return processed


async def _execute_one(orch: "Orchestrator", task: Task) -> Task:
    """Run the developer -> reviewer -> tests loop for one task. Returns the final task."""
    retry_limit = orch.cfg.qa_retry_limit
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

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
    inv = AgentInvocation(
        role=role,
        prompt="\n".join(parts),
        cwd=orch.cwd,
        model=spec.model,
        allowed_tools=list(spec.tools) if spec.tools else None,
        max_turns=spec.max_turns or 1,
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
