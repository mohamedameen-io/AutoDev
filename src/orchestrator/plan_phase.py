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
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from adapters.types import AgentInvocation, AgentResult
from errors import AutodevError, TournamentError
from autologging import get_logger
from orchestrator.delegation_envelope import DelegationEnvelope
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
    """v0.26.2 Phase 3: build a retry envelope that carries:

    * the prior architect attempt (truncated to 2000 chars),
    * the stringified exception in ``parse_error`` (back-compat),
    * the typed ``path_error_*`` fields when ``exc`` is a
      :class:`PathValidationError` (Phase 1b),
    * the accumulated ``prior_errors`` list so the architect can see
      "you've now emitted this same path 2 times — fix it",
    * the list of entries previously dropped on this plan-phase run.

    Returns a new :class:`DelegationEnvelope` (does not mutate the input).
    """
    from orchestrator.path_validator import PathValidationError

    ctx_extras: dict[str, Any] = {
        "prior_attempt": prior_plan_md[:2000] if prior_plan_md else "",
        "parse_error": str(exc) if exc is not None else "",
        "prior_errors": [
            {"raw": r, "reason": s, "count": n}
            for (r, s), n in errors_seen.items()
        ],
        "dropped_entries": list(dropped_entries),
        "hint": _retry_hint_text(),
    }
    if isinstance(exc, PathValidationError):
        ctx_extras["path_error_raw"] = exc.raw
        ctx_extras["path_error_reason"] = exc.reason
        ctx_extras["path_error_suggestion"] = exc.suggestion or ""
    return base_env.model_copy(
        update={"context": {**base_env.context, **ctx_extras}}
    )


def _drop_entry_from_plan(
    plan: Plan, bad_path: str
) -> tuple[Plan, bool]:
    """v0.26.2 Phase 3: drop ``bad_path`` from every validator-walked
    scope site, returning ``(new_plan, was_dropped)``.

    Drops from:
      - ``plan.edit_scope``
      - every ``phase.edit_scope`` (when non-None — ``None`` means inherit)
      - every ``task.files``
      - every ``task.extended_scope``

    Does NOT touch ``task.files_new`` — those are the architect's declared
    about-to-be-created files and the v0.24.3 validator already skips them.

    A path matches if it equals an entry OR if it equals the entry with
    a trailing ``/`` trimmed (the validator treats scope entries as
    directory prefixes).

    Uses ``model_copy(update=...)`` at every level so Pydantic field
    validators re-fire on the new objects. Direct in-place mutation would
    NOT re-trigger validation and would leave the new model in a
    potentially-invalid state.
    """
    norm = bad_path.rstrip("/")
    was_dropped = False

    def _filter(entries: list[str]) -> tuple[list[str], bool]:
        kept = [e for e in entries if e.rstrip("/") != norm]
        return kept, len(kept) != len(entries)

    new_plan_edit, dropped_at_plan_level = _filter(list(plan.edit_scope))
    if dropped_at_plan_level:
        was_dropped = True

    new_phases: list[Phase] = []
    for phase in plan.phases:
        new_phase_edit: list[str] | None = phase.edit_scope
        if phase.edit_scope is not None:
            ph_kept, ph_dropped = _filter(list(phase.edit_scope))
            if ph_dropped:
                was_dropped = True
                new_phase_edit = ph_kept

        new_tasks: list[Task] = []
        any_task_mutated = False
        for task in phase.tasks:
            t_files, t_files_dropped = _filter(list(task.files))
            t_ext, t_ext_dropped = _filter(list(task.extended_scope))
            if t_files_dropped or t_ext_dropped:
                was_dropped = True
                any_task_mutated = True
                new_tasks.append(
                    task.model_copy(
                        update={
                            "files": t_files,
                            "extended_scope": t_ext,
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

    if was_dropped:
        new_plan = plan.model_copy(
            update={
                "edit_scope": new_plan_edit,
                "phases": new_phases,
            }
        )
        return new_plan, True
    return plan, False


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
            validate_files_exist(plan, cwd)
            return plan
        except PathValidationError as exc:
            key = (exc.raw, exc.reason)
            # Read-only check: anticipate the next outer-loop increment.
            # If we haven't met the recurrence threshold yet, bubble up
            # so the outer except increments + retries the architect.
            if errors_seen.get(key, 0) + 1 < _DROP_AT_RECURRENCE:
                raise
            # Threshold met — try to drop the bad entry.
            new_plan, was_dropped = _drop_entry_from_plan(plan, exc.raw)
            if not was_dropped:
                # The bad path isn't in any validator-walked site we
                # know how to drop from (e.g. it appears only as a
                # task.files_new entry, which we deliberately preserve).
                raise
            # Hard empty-scope guard: only fires when plan.edit_scope
            # was non-empty before AND would become empty after the
            # drop. Empty plan.edit_scope is the whole-repo sentinel —
            # silently widening here would be the P0 risk this entire
            # phase guards against.
            if plan.edit_scope and not new_plan.edit_scope:
                raise
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
            plan = new_plan
            dropped_entries.append(exc.raw)
            # Loop continues — re-validate the mutated plan. Because
            # the path was just removed from every validator-walked
            # site, the next call CANNOT raise on the same key —
            # guaranteed termination.


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
        architect_spec = orch.registry.get("architect")
        retry_max = (
            (architect_spec.max_turns or 5) + 2 if architect_spec else 7
        )

        for attempt in range(_MAX_ARCHITECT_ATTEMPTS):
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
            architect_result = await _delegate(
                orch, "architect", env, max_turns_override=max_turns_override
            )
            plan_md = architect_result.text
            # Fallback: if architect wrote to a file instead of returning
            # text, try reading the plan from known file locations.
            plan_md = _try_read_plan_from_file(cwd, plan_md)

            try:
                plan = parse_plan_markdown(plan_md, spec_hash=spec_hash)
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
                if isinstance(exc, PathValidationError):
                    key = (exc.raw, exc.reason)
                    errors_seen[key] = errors_seen.get(key, 0) + 1
                logger.warning(
                    "plan_phase.parse_failed_retrying",
                    err=str(exc),
                    archived_to=str(archived),
                    attempt=attempt + 1,
                    max_attempts=_MAX_ARCHITECT_ATTEMPTS,
                )
                if attempt == _MAX_ARCHITECT_ATTEMPTS - 1:
                    # Exhausted retries — surface the original error
                    # so the operator can inspect the archived markdown.
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

    v0.26.0: InlineAdapter's suspend/resume special-cases (response-file
    shortcut on the resume path, ``write_suspend_state`` on the
    ``DelegationPendingSignal`` exit path) were removed. Every adapter
    is now a subprocess adapter.
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
