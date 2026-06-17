"""Universal Blocker Resolver core (ADR-0047, headline of v0.42.0).

When a downstream agent/phase hits a *terminal* blocker — or an *unrecognised*
failure the deterministic recovery ladder does not know about — the orchestrator
used to dead-end at ``blocked: user_decision_required`` or silently degrade. This
module is the chokepoint that replaces that dead-end with a single, bounded,
*executable* recovery decision: a :class:`~state.schemas.ResolutionAction`.

Two-tier design (mirrors :class:`config.schema.ResolverConfig`):

  1. A LADDER-AWARE **deterministic fast-path** (:func:`deterministic_action`)
     for *known* failure classes. Given what recovery has already been tried for
     this blocker, it returns the next sensible un-tried action, ``ask_human``
     when the ladder is exhausted, or ``None`` to defer to the LLM. No model call.
  2. An **LLM resolver** (:func:`_llm_resolve`) for novel/unseen failure classes
     (and, when ``fast_path_only_on_known=False``, for every routed blocker). It
     dispatches the self-contained ``resolver`` specialist role (the same
     ``load_prompt`` path framing uses), forces a structured JSON decision, and
     parses it into a :class:`ResolutionAction`. On ANY failure (dispatch raises,
     parse fails, action token invalid) it returns a safe ``ask_human`` — it
     NEVER recurses on the resolver (B5: resolver-self-failure -> ask_human).

Loop-safety (B5): :func:`resolve_blocker` consults a resume-safe, ledger-tracked
per-blocker cycle budget (:func:`count_prior_cycles`). Once a single blocker has
consumed ``cfg.resolver.max_cycles_per_blocker`` cycles it stops and returns a
bounded ``ask_human`` instead of re-engaging.

Public API (Phase-3 wiring depends on these EXACT signatures):

  * :func:`blocker_key`
  * :func:`count_prior_cycles`
  * :func:`deterministic_action`
  * :func:`resolve_blocker`  — THE chokepoint decision function
  * :func:`consult_knowledge`  — thin async helper for the consult_knowledge action

The CALLER (Phase 3) checks ``cfg.resolver.enabled`` + ``AUTODEV_RESOLVER_DISABLED``
BEFORE calling :func:`resolve_blocker`, then APPLIES the returned action and
appends a ``resolution_outcome`` ledger op. :func:`resolve_blocker` assumes it is
enabled.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from autologging import get_logger
from pydantic import ValidationError

from adapters.types import AgentInvocation
from agents import load_prompt
from orchestrator import failure_classes as fc
from orchestrator.knowledge_lookup import lookup_recent_failures
from state import ledger as ledger_mod
from state.schemas import BlockerContext, ResolutionAction, ResolutionActionType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from orchestrator import Orchestrator


logger = get_logger()


# The role dispatched for the LLM resolver. Registered in ``cfg.agents`` and
# backed by ``src/agents/prompts/resolver.md`` (loaded via ``load_prompt``).
_RESOLVER_ROLE = "resolver"

# Wall-clock cap on the consult_knowledge KB lookup. Generous enough for a
# healthy local read; the lookup itself degrades to ``[]`` on timeout.
_KNOWLEDGE_TIMEOUT_S = 0.2


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def blocker_key(ctx: BlockerContext) -> str:
    """Return a stable per-blocker key: ``"{task_id|-}:{failure_class}"``.

    Used both as the per-blocker cycle-budget key (:func:`count_prior_cycles`)
    and as a payload field on the ledger breadcrumbs so a resume can correlate
    a fresh blocker with prior resolution cycles.
    """
    return f"{ctx.task_id or '-'}:{ctx.failure_class}"


def count_prior_cycles(orch: "Orchestrator", ctx: BlockerContext) -> int:
    """Count prior ``resolution_chosen`` ledger entries for this blocker key.

    Resume-safe per-blocker budget: reads the on-disk ledger via the read API
    (:func:`state.ledger.read_entries`) and counts entries whose payload
    ``blocker_key`` matches :func:`blocker_key`. Never re-invokes anything and
    never raises — a missing/empty ledger yields ``0``.
    """
    key = blocker_key(ctx)
    try:
        entries = ledger_mod.read_entries(orch.cwd)
    except Exception as exc:  # noqa: BLE001 - resume-safe; ledger may be absent/corrupt
        logger.warning("blocker_resolver.count_cycles_read_failed", err=str(exc))
        return 0
    return sum(
        1
        for e in entries
        if e.op == "resolution_chosen" and e.payload.get("blocker_key") == key
    )


def _act(
    action: ResolutionActionType, rationale: str, **params: Any
) -> ResolutionAction:
    """Build a :class:`ResolutionAction` with the given params/rationale."""
    return ResolutionAction(action=action, params=dict(params), rationale=rationale)


def _next_rung(tried: list[str], ladder: list[ResolutionAction]) -> ResolutionAction:
    """Return the first ladder rung whose action has not yet been tried.

    Falls through to the last rung (always ``ask_human`` by construction) when
    every earlier rung has already been attempted for this blocker.
    """
    tried_set = set(tried)
    for rung in ladder:
        if rung.action not in tried_set:
            return rung
    return ladder[-1]


def _budget_dedupe_key(ctx: BlockerContext) -> str:
    """Stable shared-cap key for budget-widening dedupe (WS1-guardrail-double).

    ``GUARDRAIL_EXCEEDED`` has TWO independent budget-widening mechanisms: this
    resolver's ``escalate_budget`` rung and the in-loop
    :class:`~orchestrator.budget_escalation.BudgetEscalationTracker` (keyed on
    ``(task_id, role)``). Without a shared key they widen the SAME guardrail
    budget twice per cycle. The resolver rung carries this key + ``defer_to_tracker``
    so the actual turn/timeout widening is owned by the single tracker — the cap
    is shared, widened once per cycle, not compounded.
    """
    return f"{ctx.task_id or '-'}:{ctx.failing_role or '-'}:{ctx.failure_class}"


def deterministic_action(ctx: BlockerContext) -> ResolutionAction | None:
    """Ladder-aware fast-path map for KNOWN failure classes.

    Given ``ctx.recovery_already_tried`` (the actions already attempted for THIS
    blocker), return the NEXT un-tried sensible action, an ``ask_human`` when the
    ladder is exhausted, ``fall_through`` for the intentional legacy-quarantine
    classes, or ``None`` to defer to the LLM (novel/unrecognised class).

    Policy (one ladder per known class; the final rung is always a terminal
    ``ask_human`` so the ladder cannot run off the end):

      * ``guardrail_exceeded``    -> escalate_budget (defer_to_tracker; shared
        budget_dedupe_key so the budget is widened once per cycle) -> ask_human
      * ``test_diagnosis_*``      -> consult_knowledge -> retry_with_changes -> ask_human
      * ``worker_exception``      -> retry_with_changes -> ask_human
      * ``conflict_*``            -> re_architect -> ask_human
      * ``worktree_apply_failed``      -> repair_environment -> ask_human
      * ``worktree_diff_check_failed`` -> repair_environment -> ask_human
      * ``phase_degraded``             -> repair_environment -> ask_human  (the DOA conversion)
      * ``soft_blocker``          -> consult_knowledge (no_immediate_reescalate;
        min_cycle_gap so the re-enable can't churn in the same cycle) -> ask_human
      * ``dag_invalid`` /
        ``cross_phase_dag_invalid`` -> re_plan -> ask_human
      * ``edit_scope_violation``  -> narrow_scope -> re_plan -> ask_human
      * ``qa_gate_failed`` /
        ``tests_failed``          -> retry_with_changes -> ask_human
      * ``review_rejected`` /
        ``review_malformed``      -> retry_with_changes -> ask_human
      * ``review_escalated``      -> consult_knowledge -> ask_human
      * ``infra_circuit_open``    -> fall_through  (legacy quarantine is intentional)

    Returns ``None`` for any class not in the map — the novel-failure path the
    LLM resolver handles.
    """
    cls = ctx.failure_class
    tried = ctx.recovery_already_tried

    # --- intentional fall-through (legacy quarantine is correct here) ------
    if cls == fc.INFRA_CIRCUIT_OPEN:
        return _act(
            "fall_through",
            rationale=(
                "infra circuit-breaker open: the cross-task auth/rate-limit "
                "quarantine is intentional; defer to the legacy block so the "
                "operator can clear the underlying infra issue and resume."
            ),
        )

    ladder: list[ResolutionAction] | None = None

    if cls == fc.GUARDRAIL_EXCEEDED:
        ladder = [
            _act(
                "escalate_budget",
                rationale=(
                    "guardrail/decision-cost budget exhausted: widen the cap once "
                    "before giving up so a near-complete task can finish."
                ),
                # WS1-guardrail-double-budget-widen: the in-loop
                # ``BudgetEscalationTracker`` already widens the turn/timeout
                # budget for this (task, role) on the re-run this rung triggers.
                # If the resolver ALSO widened independently the same guardrail
                # budget would be bumped TWICE per cycle. ``defer_to_tracker``
                # cedes the actual widening to that single tracker, and
                # ``budget_dedupe_key`` is the shared cap key so both paths
                # coordinate to one widening per cycle.
                defer_to_tracker=True,
                budget_dedupe_key=_budget_dedupe_key(ctx),
            ),
            _act(
                "ask_human",
                rationale=(
                    "guardrail budget exhausted and a single escalation did not "
                    "unblock it; the cap likely masks a deeper problem — ask the "
                    "operator to widen the budget or rescope."
                ),
                question=(
                    "Task hit its guardrail/turn budget even after one escalation. "
                    "Widen the budget, rescope the task, or accept current state?"
                ),
            ),
        ]
    elif cls == fc.OVERSIZED_INPUT:
        # RECOVERY-CONTRACT §7 Step 8 (the A4 root cause): the role (esp.
        # ``critic_t``) hit ``error_max_turns`` because its prompt was
        # OVERSIZED — it burned its turns digesting context bloat. The remedy
        # is to BOUND the input (truncate / decompose / re-dispatch with reduced
        # scope), NOT to widen the turn budget. ``escalate_budget`` is the WRONG
        # direction here, so it is deliberately absent from this ladder; we use
        # ``narrow_scope`` with a ``direction="bound_input"`` so the call site
        # re-dispatches the same task against a smaller prompt rather than the
        # same bloat with more turns.
        ladder = [
            _act(
                "narrow_scope",
                rationale=(
                    "the role exhausted its turn budget on an OVERSIZED prompt "
                    "(context-window bloat): bound the input — truncate / "
                    "decompose / re-dispatch with reduced scope. Granting more "
                    "turns is the wrong direction (it just re-reads the bloat)."
                ),
                direction="bound_input",
            ),
            _act(
                "ask_human",
                rationale=(
                    "the input is still oversized after a bounding pass; it "
                    "cannot be mechanically reduced enough to fit — ask the "
                    "operator to decompose the task or raise the model's context."
                ),
                question=(
                    "A role keeps exhausting its turns on an oversized prompt "
                    "even after bounding the input. Should this task be split "
                    "into smaller units, or does it need a larger-context model?"
                ),
            ),
        ]
    elif cls in (fc.TEST_DIAGNOSIS_HARDFAIL, fc.TEST_DIAGNOSIS_NO_SIGNAL):
        ladder = [
            _act(
                "consult_knowledge",
                rationale=(
                    "test diagnosis is terminal: first check past-failure memory "
                    "for a known fix on this signature before re-attempting."
                ),
            ),
            _act(
                "retry_with_changes",
                rationale=(
                    "no prior knowledge resolved it: retry the implementation with "
                    "the diagnosis context spliced in (budget/turn escalation)."
                ),
            ),
            _act(
                "ask_human",
                rationale=(
                    "tests remain unrunnable/red after a knowledge-informed retry; "
                    "the failure is not mechanically recoverable — ask the operator."
                ),
                question=(
                    "Tests stay red/unrunnable after a knowledge-informed retry. "
                    "Is the test wrong, the fix wrong, or is the environment broken?"
                ),
            ),
        ]
    elif cls == fc.WORKER_EXCEPTION:
        ladder = [
            _act(
                "retry_with_changes",
                rationale=(
                    "worker (developer/test adapter) raised at the code layer: "
                    "retry with a fresh attempt + escalated budget — most crashes "
                    "are transient or context-shaped."
                ),
            ),
            _act(
                "ask_human",
                rationale=(
                    "the worker crashed again on retry; this is not a transient "
                    "fault — ask the operator to inspect the captured stack."
                ),
                question=(
                    "The implementation worker crashed twice. Inspect the captured "
                    "exception — is this an environment fault or a task defect?"
                ),
            ),
        ]
    elif cls in (
        fc.CONFLICT_3WAY_FAILED,
        fc.CONFLICT_ABANDON,
        fc.CONFLICT_REWRITE_CAP_EXCEEDED,
    ):
        ladder = [
            _act(
                "re_architect",
                rationale=(
                    "merge conflict could not be resolved mechanically: rethink the "
                    "task at component altitude so the patches stop colliding."
                ),
            ),
            _act(
                "ask_human",
                rationale=(
                    "re-architecting did not eliminate the merge collision; the "
                    "conflicting changes likely encode a real design tension — "
                    "ask the operator to adjudicate."
                ),
                question=(
                    "Two changes keep colliding even after re-architecting. Which "
                    "side is authoritative, or should they be sequenced?"
                ),
            ),
        ]
    elif cls == fc.WORKTREE_APPLY_FAILED:
        ladder = [
            _act(
                "repair_environment",
                rationale=(
                    "patch could not be applied to the working tree: rebuild/reset "
                    "the worktree to a clean base and re-apply."
                ),
            ),
            _act(
                "ask_human",
                rationale=(
                    "the patch still will not apply after an environment repair; "
                    "the tree state is inconsistent with the diff — ask the operator."
                ),
                question=(
                    "A patch will not apply even after resetting the worktree. The "
                    "base is likely diverged — how should the tree be reconciled?"
                ),
            ),
        ]
    elif cls == fc.WORKTREE_DIFF_CHECK_FAILED:
        # A failed diff-check means git/worktree state is unreadable — treat
        # it as an environment problem (same root cause as apply failures) and
        # walk the same deterministic ladder: repair first, then escalate to a
        # human if the environment still cannot be read after repair.
        ladder = [
            _act(
                "repair_environment",
                rationale=(
                    "worktree diff-check failed: the git state cannot be read; "
                    "rebuild/reset the worktree to restore a readable base."
                ),
            ),
            _act(
                "ask_human",
                rationale=(
                    "the diff-check still fails after an environment repair; "
                    "the worktree is in an irrecoverable state — ask the operator."
                ),
                question=(
                    "A worktree diff-check failed even after resetting the environment. "
                    "The git state appears irrecoverable — how should this be resolved?"
                ),
            ),
        ]
    elif cls == fc.PHASE_DEGRADED:
        # The DOA conversion: a phase that silently degraded becomes an
        # actionable environment repair instead of a no-op.
        ladder = [
            _act(
                "repair_environment",
                rationale=(
                    "a phase degraded (e.g. role-dispatch no-op / missing artifact): "
                    "backfill the missing role/artifact and re-run the phase instead "
                    "of silently continuing degraded."
                ),
            ),
            _act(
                "ask_human",
                rationale=(
                    "the phase is still degraded after an environment repair; the "
                    "missing capability is not auto-recoverable — ask the operator."
                ),
                question=(
                    "A phase keeps degrading even after repairing the environment. "
                    "What capability/artifact is missing?"
                ),
            ),
        ]
    elif cls == fc.SOFT_BLOCKER:
        ladder = [
            _act(
                "consult_knowledge",
                rationale=(
                    "soft-blocker handoff rung reached: consult past-failure memory "
                    "for a known unblock before escalating to a human."
                ),
                # WS1-soft-blocker-single-cycle-churn: a soft-blocker
                # consult_knowledge re-enable resets the task's retry budget and
                # transitions it back to in_progress. If the re-enabled task
                # immediately re-soft-blocks it would re-escalate in the SAME
                # cycle (the per-blocker cycle budget hasn't advanced yet),
                # producing single-cycle churn. ``no_immediate_reescalate`` tells
                # the call site to consume the cycle budget before re-engaging,
                # and ``min_cycle_gap`` is the minimum number of cycles that must
                # pass before this blocker may escalate again.
                no_immediate_reescalate=True,
                min_cycle_gap=1,
            ),
            _act(
                "ask_human",
                rationale=(
                    "no prior knowledge unblocks this soft-block; it genuinely "
                    "needs an operator decision — surface a precise question."
                ),
                question=(
                    "This task soft-blocked and prior runs have no recorded fix. "
                    "What decision is needed to proceed?"
                ),
            ),
        ]
    elif cls in (fc.DAG_INVALID, fc.CROSS_PHASE_DAG_INVALID):
        ladder = [
            _act(
                "re_plan",
                rationale=(
                    "the plan DAG is structurally invalid (cycle / dangling dep): "
                    "re-plan so the dependency graph is well-formed — this is not "
                    "task-local."
                ),
            ),
            _act(
                "ask_human",
                rationale=(
                    "re-planning did not produce a valid DAG; the spec may encode a "
                    "genuine ordering contradiction — ask the operator."
                ),
                question=(
                    "The plan graph stays invalid after a re-plan. Is there a "
                    "circular requirement in the spec that needs resolving?"
                ),
            ),
        ]
    elif cls == fc.EDIT_SCOPE_VIOLATION:
        ladder = [
            _act(
                "narrow_scope",
                rationale=(
                    "a task wrote outside its declared edit scope: narrow the task "
                    "to the in-scope files (scope degradation) before re-planning."
                ),
            ),
            _act(
                "re_plan",
                rationale=(
                    "narrowing scope did not contain the change: the task genuinely "
                    "needs a wider/restructured scope — re-plan with the correct "
                    "edit_scope."
                ),
            ),
            _act(
                "ask_human",
                rationale=(
                    "the work keeps escaping the declared scope after a narrow + "
                    "re-plan; the scope boundary itself may be wrong — ask the "
                    "operator."
                ),
                question=(
                    "Work keeps spilling outside the declared edit scope. Should the "
                    "scope be widened, or is the change touching the wrong module?"
                ),
            ),
        ]
    elif cls in (fc.QA_GATE_FAILED, fc.TESTS_FAILED):
        # Step 5 (Part 2): the developer-side verification failures are
        # RETRY-mappable (not structural). A QA gate / test failure that survived
        # the in-loop retry budget gets ONE knowledge-informed retry through the
        # resolver, then escalates to a human. Mapping them here makes recovery
        # deterministic + testable (was None → LLM fallback).
        ladder = [
            _act(
                "retry_with_changes",
                rationale=(
                    "a QA gate / test run failed past the in-loop retry budget: "
                    "retry the implementation once more with the failure context "
                    "spliced in before giving up."
                ),
            ),
            _act(
                "ask_human",
                rationale=(
                    "the QA gate / tests still fail after a context-informed retry; "
                    "this is not mechanically recoverable — ask the operator."
                ),
                question=(
                    "QA gate / tests keep failing after a retry. Is the test wrong, "
                    "the fix wrong, or the environment broken?"
                ),
            ),
        ]
    elif cls in (fc.REVIEW_REJECTED, fc.REVIEW_MALFORMED):
        # Reviewer-side failures: a rejected or malformed review is also
        # retry-mappable — re-dispatch the developer with the review feedback,
        # then escalate.
        ladder = [
            _act(
                "retry_with_changes",
                rationale=(
                    "the reviewer rejected the change (or returned a malformed "
                    "verdict): retry the implementation with the review feedback "
                    "spliced in before escalating."
                ),
            ),
            _act(
                "ask_human",
                rationale=(
                    "the reviewer still rejects (or keeps returning malformed "
                    "verdicts) after a feedback-informed retry — ask the operator "
                    "to adjudicate the change or the reviewer signal."
                ),
                question=(
                    "The reviewer keeps rejecting / returning malformed verdicts "
                    "after a retry. Is the change wrong, or is the review signal "
                    "unreliable?"
                ),
            ),
        ]
    elif cls == fc.REVIEW_ESCALATED:
        # An explicitly-escalated review wants more context, not another blind
        # retry: consult past-failure knowledge first, then ask the operator.
        ladder = [
            _act(
                "consult_knowledge",
                rationale=(
                    "the reviewer escalated for a decision: consult past-failure "
                    "memory for a known resolution on this signature before "
                    "surfacing it to a human."
                ),
            ),
            _act(
                "ask_human",
                rationale=(
                    "no prior knowledge resolves the escalated review; it genuinely "
                    "needs an operator decision — surface a precise question."
                ),
                question=(
                    "The reviewer escalated this change for a decision and prior "
                    "runs have no recorded resolution. What should happen?"
                ),
            ),
        ]

    if ladder is None:
        return None
    return _next_rung(tried, ladder)


# --------------------------------------------------------------------------
# consult_knowledge — thin async helper (no execute loop needed)
# --------------------------------------------------------------------------


async def consult_knowledge(orch: "Orchestrator", ctx: BlockerContext) -> str:
    """Best-effort, time-bounded past-failure lookup. NEVER raises.

    Wraps :func:`orchestrator.knowledge_lookup.lookup_recent_failures` and folds
    the returned summaries into a single short string the Phase-3 wiring can
    splice into a retry prompt (or log). Returns ``""`` when there is no task id,
    no matching memory, or anything goes wrong — the consult_knowledge action is
    advisory and must not be able to block recovery.
    """
    task_id = ctx.task_id
    if not task_id:
        return ""
    try:
        summaries = await lookup_recent_failures(
            orch,
            task_id,
            timeout_s=_KNOWLEDGE_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - advisory; must never raise
        logger.warning("blocker_resolver.consult_knowledge_failed", err=str(exc))
        return ""
    if not summaries:
        return ""
    return "Prior failures on this task signature:\n- " + "\n- ".join(summaries)


# --------------------------------------------------------------------------
# LLM resolver (self-contained specialist dispatch + forced structured output)
# --------------------------------------------------------------------------


def _ask_human(rationale: str, **params: Any) -> ResolutionAction:
    return _act("ask_human", rationale=rationale, **params)


def _build_context_block(ctx: BlockerContext) -> str:
    """Render the CONTEXT block appended to the resolver prompt.

    Generalises the architect-consult ``ARCHITECT_CONTEXT`` builder: a compact,
    field-labelled dump of everything the resolver needs to reason about the
    blocker without re-reading orchestrator internals.
    """
    available = ctx.available_actions or list(
        # When the call site does not pin a subset, the whole vocabulary is fair
        # game. Keep it explicit in the prompt so the model knows its options.
        [
            "retry_with_changes",
            "split_task",
            "narrow_scope",
            "re_architect",
            "re_plan",
            "reroute",
            "repair_environment",
            "relax_constraint",
            "escalate_budget",
            "escalate_model",
            "soft_pass_with_evidence",
            "consult_knowledge",
            "web_search",
            "ask_human",
            "fall_through",
        ]
    )
    lines = [
        "## CONTEXT",
        f"failure_class: {ctx.failure_class}",
        f"failing_role: {ctx.failing_role or '(unknown)'}",
        f"task_id: {ctx.task_id or '(none)'}",
        f"phase_id: {ctx.phase_id or '(none)'}",
        f"attempt_history: {', '.join(ctx.attempt_history) or '(none)'}",
        f"recovery_already_tried: {', '.join(ctx.recovery_already_tried) or '(none)'}",
        f"available_actions: {', '.join(available)}",
        f"evidence_refs: {', '.join(ctx.evidence_refs) or '(none)'}",
        "",
        "### raw_error",
        (ctx.raw_error or "(no captured error text)")[:4000],
    ]
    return "\n".join(lines)


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Robustly extract the first JSON object from agent text.

    Tries, in order: a fenced ```json``` / ``` block; then the first balanced
    ``{...}`` span found by a brace scan. Returns ``None`` if nothing parses.
    """
    if not text:
        return None

    candidates: list[str] = []
    m = _JSON_FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())

    # Brace-balanced scan for the first top-level object (covers bare JSON and
    # JSON embedded in prose without a fence).
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    candidates.append(text[start : i + 1])
                    break

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _parse_resolution(text: str) -> ResolutionAction | None:
    """Parse agent text into a validated :class:`ResolutionAction`.

    Strips code fences / prose, extracts the JSON object, and validates it via
    ``ResolutionAction.model_validate`` (which enforces the bounded action
    vocabulary). Returns ``None`` on any parse/validate failure so the caller
    can fall back to ``ask_human``.
    """
    obj = _extract_json_object(text)
    if obj is None:
        return None
    try:
        return ResolutionAction.model_validate(obj)
    except ValidationError:
        return None


async def _llm_resolve(orch: "Orchestrator", ctx: BlockerContext) -> ResolutionAction:
    """Dispatch the ``resolver`` specialist role and parse a structured action.

    Self-contained dispatch mirroring ``framing_phase._invoke_framing_role``:
    ``load_prompt(role)`` + ``cfg.agents[role]`` + ``AgentInvocation`` +
    ``orch.adapter.execute``. The model override is ``cfg.resolver.model`` (else
    the agent's own configured model).

    Forced structured output: the resolver prompt instructs the model to emit
    ONLY a JSON object matching :class:`ResolutionAction`. On ANY exception
    (dispatch raises) OR an unparseable/invalid response, returns a safe
    ``ask_human`` — this is the resolver-self-failure -> ask_human safety (B5).
    It NEVER recurses on the resolver.
    """
    try:
        raw_prompt = load_prompt(_RESOLVER_ROLE)
    except Exception as exc:  # noqa: BLE001 - missing prompt must not crash the loop
        logger.warning("blocker_resolver.prompt_load_failed", err=str(exc))
        return _ask_human(
            rationale=(
                "resolver prompt could not be loaded; defaulting to a human "
                "decision (resolver-self-failure safety)."
            ),
            question="The blocker resolver itself failed to initialise. Manual review needed.",
        )

    context_block = _build_context_block(ctx)
    full_prompt = "\n\n---\n".join([raw_prompt.strip(), context_block])

    try:
        agent_cfg = orch.cfg.agents[_RESOLVER_ROLE]
    except Exception as exc:  # noqa: BLE001 - unregistered role must not crash
        logger.warning("blocker_resolver.role_unregistered", err=str(exc))
        return _ask_human(
            rationale=(
                "resolver role is not registered; defaulting to a human decision "
                "(resolver-self-failure safety)."
            ),
            question="The blocker resolver role is unavailable. Manual review needed.",
        )

    model_override = orch.cfg.resolver.model or agent_cfg.model
    inv = AgentInvocation(
        role=_RESOLVER_ROLE,
        prompt=full_prompt,
        cwd=orch.cwd,
        model=model_override,
        max_turns=agent_cfg.max_turns or 1,
    )

    try:
        result = await orch.adapter.execute(inv)
    except Exception as exc:  # noqa: BLE001 - dispatch failure -> ask_human, never recurse
        logger.warning("blocker_resolver.dispatch_failed", err=str(exc))
        return _ask_human(
            rationale=(
                "resolver dispatch raised; defaulting to a human decision "
                "(resolver-self-failure safety)."
            ),
            question="The blocker resolver failed to run. Manual review needed.",
        )

    action = _parse_resolution(result.text or "")
    if action is None:
        logger.warning(
            "blocker_resolver.parse_failed",
            failure_class=ctx.failure_class,
            task_id=ctx.task_id,
        )
        return _ask_human(
            rationale=(
                "resolver returned an unparseable/invalid decision; defaulting to "
                "a human decision (resolver-self-failure safety)."
            ),
            question=(
                "The blocker resolver could not produce a valid action for this "
                "novel failure. Manual review needed."
            ),
        )
    return action


# --------------------------------------------------------------------------
# resolve_blocker — THE chokepoint decision function
# --------------------------------------------------------------------------


async def resolve_blocker(orch: "Orchestrator", ctx: BlockerContext) -> ResolutionAction:
    """Choose a bounded recovery action for a terminal blocker.

    Contract (the caller has already checked ``cfg.resolver.enabled`` +
    ``AUTODEV_RESOLVER_DISABLED`` — this function assumes it is enabled):

      1. Append a ``blocker_escalated`` ledger op (forensics).
      2. Loop-safety (B5): if :func:`count_prior_cycles` ``>=
         cfg.resolver.max_cycles_per_blocker`` choose ``ask_human``
         (per-blocker resolution budget exhausted), record, return.
      3. If ``cfg.resolver.fast_path_only_on_known`` and the class is known,
         try :func:`deterministic_action`.
      4. If still undecided, call :func:`_llm_resolve` (which itself can never
         raise — it returns ``ask_human`` on any self-failure).
      5. Append a ``resolution_chosen`` ledger op and return the action.

    The CALLER then APPLIES the action and appends ``resolution_outcome``.
    """
    cfg = orch.cfg.resolver
    key = blocker_key(ctx)

    # 1. blocker_escalated breadcrumb (best-effort — never block recovery).
    await _safe_ledger_append(
        orch,
        "blocker_escalated",
        {
            "task_id": ctx.task_id,
            "phase_id": ctx.phase_id,
            "failure_class": ctx.failure_class,
            "failing_role": ctx.failing_role,
            "raw_error_excerpt": (ctx.raw_error or "")[:500],
            "recovery_already_tried": list(ctx.recovery_already_tried),
            "blocker_key": key,
        },
    )

    # 2. Loop-safety: per-blocker resolution budget.
    prior = count_prior_cycles(orch, ctx)
    if prior >= cfg.max_cycles_per_blocker:
        budget_action = _ask_human(
            rationale="per-blocker resolution budget exhausted",
            question=(
                "This blocker has consumed its resolution budget without "
                "recovering. Manual review needed."
            ),
        )
        await _record_chosen(orch, ctx, budget_action, key)
        return budget_action

    # 3. Deterministic fast-path for known classes.
    action: ResolutionAction | None = None
    if cfg.fast_path_only_on_known and fc.is_known(ctx.failure_class):
        action = deterministic_action(ctx)

    # 4. LLM resolver for novel classes (or when fast-path is disabled).
    #    _llm_resolve never raises — it self-fails to ask_human.
    if action is None:
        action = await _llm_resolve(orch, ctx)

    # 5. resolution_chosen breadcrumb + return.
    await _record_chosen(orch, ctx, action, key)
    return action


# --------------------------------------------------------------------------
# Ledger helpers (best-effort; audit must never block recovery)
# --------------------------------------------------------------------------


async def _safe_ledger_append(
    orch: "Orchestrator", op: str, payload: dict[str, Any]
) -> None:
    try:
        await orch.plan_manager.ledger_append(op, payload)
    except Exception as exc:  # noqa: BLE001 - audit-only; must never block recovery
        logger.warning("blocker_resolver.ledger_append_failed", op=op, err=str(exc))


async def _record_chosen(
    orch: "Orchestrator",
    ctx: BlockerContext,
    action: ResolutionAction,
    key: str,
) -> None:
    await _safe_ledger_append(
        orch,
        "resolution_chosen",
        {
            "blocker_key": key,
            "task_id": ctx.task_id,
            "failure_class": ctx.failure_class,
            "action": action.action,
            "rationale": (action.rationale or "")[:500],
            "params": action.params,
        },
    )


async def record_phase_degrade(
    orch: "Orchestrator", phase_name: str, exc: BaseException
) -> None:
    """Convert a silent phase degrade into an EXPLICIT, recorded resolver
    decision (ADR-0047 B1).

    The intake / diagnosis / framing phases fail-safe by degrading to a
    pass-through outcome (they must never block planning). Pre-v0.42 that
    degrade vanished into a ``*.degraded`` warning log — the exact silent
    failure the Run-4 benchmark caught (intake/diagnosis were dead-on-arrival
    and nobody noticed). This routes the degrade through the resolver so it
    lands in the ledger (``blocker_escalated`` + ``resolution_chosen`` +
    ``resolution_outcome``) as an explicit, auditable decision.

    Observability-only: the phase STILL returns its degraded outcome — the
    resolver does not re-dispatch the phase here (that would risk the
    never-block contract). Gated on ``cfg.resolver.enabled`` +
    ``AUTODEV_RESOLVER_DISABLED``; best-effort, never raises.
    """
    import os

    try:
        rcfg = getattr(orch.cfg, "resolver", None)
        if rcfg is None or not getattr(rcfg, "enabled", False):
            return
        if os.environ.get("AUTODEV_RESOLVER_DISABLED", "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            return
        ctx = BlockerContext(
            failure_class=fc.PHASE_DEGRADED,
            raw_error=str(exc)[:2000],
            failing_role=phase_name,
            task_id=None,
            phase_id=phase_name,
        )
        # Records blocker_escalated + resolution_chosen (the resolver's
        # recommendation, e.g. repair_environment). We do NOT apply it here.
        await resolve_blocker(orch, ctx)
        await _safe_ledger_append(
            orch,
            "resolution_outcome",
            {
                "task_id": None,
                "action": "observed",
                "outcome": "phase_degraded",
                "reason": f"{phase_name} degraded: {str(exc)[:200]}",
            },
        )
    except Exception:  # noqa: BLE001 - observability must never break the phase
        pass


__all__ = [
    "blocker_key",
    "count_prior_cycles",
    "deterministic_action",
    "resolve_blocker",
    "consult_knowledge",
    "record_phase_degrade",
]
