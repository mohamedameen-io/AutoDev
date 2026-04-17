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
from typing import TYPE_CHECKING

from adapters.inline import InlineAdapter
from adapters.inline_types import DelegationPendingSignal
from adapters.types import AgentInvocation, AgentResult
from errors import AutodevError
from autologging import get_logger
from orchestrator.delegation_envelope import DelegationEnvelope
from orchestrator.inline_state import write_suspend_state
from orchestrator.plan_parser import (
    PlanParseError,
    parse_plan_markdown,
)
from orchestrator.plan_tournament_runner import run_plan_tournament
from state.evidence import write_evidence
from state.paths import ensure_autodev_dir, spec_path
from state.schemas import (
    ExploreEvidence,
    Plan,
    SMEEvidence,
)


if TYPE_CHECKING:
    from orchestrator import Orchestrator


logger = get_logger(__name__)


def _spec_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def run_plan_phase(orch: "Orchestrator", intent: str) -> Plan:
    """Execute the plan phase end-to-end and return the approved plan."""
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

        architect_env = DelegationEnvelope(
            task_id="plan",
            target_agent="architect",
            action="document",
            acceptance=(
                "Return a plan in the canonical autodev markdown format:\n"
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
            },
        )
        architect_result = await _delegate(orch, "architect", architect_env)

        plan_md = architect_result.text
        plan: Plan
        try:
            plan = parse_plan_markdown(plan_md, spec_hash=spec_hash)
        except PlanParseError as exc:
            logger.warning("plan_phase.parse_failed_retrying", err=str(exc))
            retry_env = architect_env.model_copy(
                update={
                    "context": {
                        **architect_env.context,
                        "prior_attempt": plan_md[:2000],
                        "parse_error": str(exc),
                        "hint": "Please use EXACTLY the canonical format.",
                    }
                }
            )
            retry_result = await _delegate(orch, "architect", retry_env)
            plan_md = retry_result.text
            plan = parse_plan_markdown(plan_md, spec_hash=spec_hash)

        if orch.cfg.tournaments.plan.enabled:
            refined_md = await run_plan_tournament(orch, plan_md, intent)
            if refined_md and refined_md != plan_md:
                try:
                    plan = parse_plan_markdown(refined_md, spec_hash=spec_hash)
                    plan_md = refined_md
                    logger.info(
                        "plan_phase.tournament_applied",
                        pre_bytes=len(plan_md),
                        post_bytes=len(refined_md),
                    )
                except PlanParseError as exc:
                    logger.warning(
                        "plan_phase.tournament_refined_unparseable",
                        err=str(exc),
                    )
            else:
                logger.info("plan_phase.tournament_no_change")

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


async def _delegate(
    orch: "Orchestrator",
    role: str,
    envelope: DelegationEnvelope,
) -> AgentResult:
    """Build an :class:`AgentInvocation` from the envelope + registry and call adapter.

    Guardrail hooks are called around the adapter execution:
    - ``pre_invocation`` before the adapter call (may raise GuardrailExceededError)
    - ``post_invocation`` after the adapter call (may raise GuardrailExceededError)
    - ``loop_detector.observe`` after post_invocation

    For :class:`~adapters.inline.InlineAdapter`:
    - If a response file already exists (resume path), collect and return it.
    - Otherwise inject ``task_id`` into ``inv.metadata`` and re-raise
      :class:`DelegationPendingSignal` after writing suspend state.
    """
    spec = orch.registry.get(role)
    if spec is None:
        raise AutodevError(f"role {role!r} not in registry")
    parts: list[str] = [spec.prompt.strip()]
    block = envelope.render_as_task_message()
    parts.append("\n\n---\n")
    parts.append(block)
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
        max_turns=1,
    )

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
        _plan_role_map: dict[str, str] = {
            "explorer": "plan_explorer",
            "domain_expert": "plan_domain_expert",
            "architect": "plan_architect",
        }
        step = _plan_role_map.get(role, role)
        write_suspend_state(
            cwd=orch.cwd,
            session_id=orch.session_id,
            pending_task_id=envelope.task_id,
            pending_role=role,
            delegation_path=sig.delegation_path,
            response_path=orch.adapter.response_path(envelope.task_id, role),  # type: ignore[union-attr]
            orchestrator_step=step,
        )
        raise
    orch.guardrails.post_invocation(envelope.task_id, result)
    if result.success and result.text:
        orch.loop_detector.observe(envelope.task_id, role, result.text)
    return result


__all__ = [
    "PlanParseError",
    "parse_plan_markdown",
    "run_plan_phase",
]
