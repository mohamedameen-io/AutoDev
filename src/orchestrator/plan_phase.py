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
from pathlib import Path
from typing import TYPE_CHECKING

from adapters.inline import InlineAdapter
from adapters.inline_types import DelegationPendingSignal
from adapters.types import AgentInvocation, AgentResult
from errors import AutodevError, TournamentError
from autologging import get_logger
from orchestrator.delegation_envelope import DelegationEnvelope
from orchestrator.inline_state import write_suspend_state
from orchestrator.file_existence_validator import validate_files_exist
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
from state.paths import autodev_root, ensure_autodev_dir, spec_path
from state.schemas import (
    ExploreEvidence,
    Plan,
    SMEEvidence,
)
from tournament.effort import resolve_role_effort
from tournament.state import (
    TournamentArtifactStore,
    latest_incumbent_md_across_branches,
)


if TYPE_CHECKING:
    from orchestrator import Orchestrator


logger = get_logger(__name__)


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

        # v0.25.0: query the file/symbol index for candidate paths/symbols
        # relevant to the spec text. The architect's prompt has a CANDIDATE
        # FILES section instructing it to prefer these paths over inventing
        # new ones; missing/disabled index degrades to an empty string and
        # the architect prompt's "PREFER paths from this list" sentence
        # becomes a no-op (no broken behavior).
        candidate_digest_str = ""
        if orch.cfg.index_enabled:
            db_path = orch.cwd / orch.cfg.index_path
            if db_path.exists():
                try:
                    from state.file_index import IndexQuery

                    digest = IndexQuery(db_path).get_candidates_for_spec(
                        spec_text=intent, limit=20
                    )
                    candidate_digest_str = digest.render(max_chars=2500)
                except Exception as exc:  # noqa: BLE001 - never block plan
                    logger.warning(
                        "plan_phase.index_query_failed", err=str(exc)
                    )

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
            },
        )
        architect_result = await _delegate(orch, "architect", architect_env)

        plan_md = architect_result.text
        # Fallback: if architect wrote to a file instead of returning text,
        # try reading the plan from known file locations.
        plan_md = _try_read_plan_from_file(cwd, plan_md)
        plan: Plan
        # v0.22.4 B4: also catch path-validator errors at parse time so
        # the architect can self-correct malformed paths (backticks,
        # parentheticals, trailing punctuation) on retry. The legacy
        # PlanParseError path remains identical.
        from pydantic import ValidationError as _PydValidationError

        from orchestrator.path_validator import PathValidationError

        try:
            plan = parse_plan_markdown(plan_md, spec_hash=spec_hash)
            # v0.24.3: enforce that every architect-emitted file path
            # actually exists on disk (modulo the ``[new]`` opt-out).
            # Any miss raises ``PathValidationError`` with
            # ``reason="missing_on_disk"`` which flows into the same
            # architect-retry envelope as the v0.22.4 path-shape errors.
            validate_files_exist(plan, cwd)
        except (
            PlanParseError,
            _PydValidationError,
            PathValidationError,
        ) as exc:
            # Build a structured architect-retry envelope: the architect
            # sees both the raw error AND a hint listing format rules so
            # the second pass produces well-formed output.
            extra_hint = (
                "Paths must be plain repo-relative strings — no surrounding "
                "backticks, quotes, parentheticals, or trailing punctuation. "
                "List one path per line in EDIT_SCOPE blocks, comma-separated "
                "in `Files:` lines."
            )
            # v0.24.3: when the failure was a missing-on-disk path,
            # append a missing-file paragraph that documents the ``[new]``
            # opt-out and embeds the closest tracked-file suggestion.
            if getattr(exc, "reason", None) == "missing_on_disk":
                suggestion = getattr(exc, "suggestion", None)
                extra_hint += (
                    "\n\nYour previous plan listed files that do not exist "
                    "on disk. Either: (a) correct the path — closest "
                    f"tracked match: {suggestion!r}, or (b) if you intend "
                    "to CREATE this file in the task, prefix the path "
                    "with [new] in the Files: line, e.g.:\n"
                    "    Files: src/foo.cpp, [new] src/foo_test.cpp\n"
                    "Do NOT smash directory paths and source code "
                    "together — a path like `src/qa/cpp_symbols.py// "
                    "...code...` is malformed and will be rejected."
                )
            logger.warning("plan_phase.parse_failed_retrying", err=str(exc))
            retry_env = architect_env.model_copy(
                update={
                    "context": {
                        **architect_env.context,
                        "prior_attempt": plan_md[:2000],
                        "parse_error": str(exc),
                        "hint": (
                            "Please use EXACTLY the canonical format. "
                            "Return the plan as your text response, do NOT write to files. "
                            f"{extra_hint}"
                        ),
                    }
                }
            )
            architect_spec = orch.registry.get("architect")
            retry_max = (architect_spec.max_turns or 5) + 2 if architect_spec else 7
            retry_result = await _delegate(
                orch, "architect", retry_env, max_turns_override=retry_max
            )
            plan_md = retry_result.text
            plan_md = _try_read_plan_from_file(cwd, plan_md)
            plan = parse_plan_markdown(plan_md, spec_hash=spec_hash)
            # v0.24.3: enforced on the retry parse too — the architect's
            # second pass must satisfy on-disk existence as well.
            validate_files_exist(plan, cwd)

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
    *,
    max_turns_override: int | None = None,
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
            response_path=orch.adapter.response_path(envelope.task_id, role),  # type: ignore[attr-defined]
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
