"""WS6 — ``escalate_model`` actually switches the next dispatch's model.

Pre-WS6 the resolver's ``escalate_model`` action (documented everywhere as
"sonnet -> opus", schema comment "recovery Tier-5", resolver prompt
``{"to": "opus"}``) routed identically to ``retry_with_changes`` /
``escalate_budget`` via ``_resolver_retry`` and NEVER changed which model the
next attempt dispatched on — a documented no-op.

WS6 threads the action's ``params["to"]`` into a validated, per-task
``model_override`` (stamped onto ``Task.metadata`` by ``_resolver_retry``) that
``delegate`` — the single dispatch chokepoint every role flows through — honours
on the next developer attempt. The override is capped to fire ONCE per blocker
(mirroring ``escalate_budget``'s once-per-cycle coordination): a second
consecutive ``escalate_model`` for the SAME blocker does NOT re-widen. An
unknown / missing ``to`` degrades to a plain retry (never dispatches a bogus
model).

Coverage:
  * ``delegate`` honours ``task.metadata["model_override"]`` (with an
    override-absent control) — the dispatch seam.
  * ``_apply_resolution`` on ``escalate_model{"to": "opus"}`` stamps the
    override onto ``Task.metadata`` (and it round-trips a reload).
  * end-to-end: after ``_apply_resolution`` chooses ``escalate_model``, the NEXT
    ``delegate`` for the task dispatches on the escalated model.
  * the once-per-blocker cap: a second consecutive choice for the same blocker
    does NOT pass a fresh ``model_override`` into ``_resolver_retry``.
  * an unrecognized ``to`` degrades to a plain retry (no override stamped).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from orchestrator.delegation_envelope import DelegationEnvelope
from state.plan_manager import PlanManager
from state.schemas import (
    AcceptanceCriterion,
    BlockerContext,
    Phase,
    Plan,
    ResolutionAction,
    Task,
)

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-ws6",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t1",
                        description="d1",
                        complexity="simple",
                    ),
                ],
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="simple",
    )


async def _build_orch(repo: Path, *, session: str) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.phase_review.enabled = False
    registry = build_registry(cfg)
    adapter = StubAdapter({"developer": ok("ok")})
    pm = PlanManager(repo, session_id=f"{session}-init")
    await pm.init_plan(_mk_plan())
    return Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id=f"{session}-exec",
    )


def _mk_envelope(task_id: str = "1.1") -> DelegationEnvelope:
    return DelegationEnvelope(
        task_id=task_id,
        target_agent="developer",
        action="implement",
    )


def _escalate_model_action(to: object = "opus") -> ResolutionAction:
    return ResolutionAction(
        action="escalate_model",
        params={"to": to},
        rationale="this task is hard; move to a stronger model",
    )


def _ctx(*, tried: list[str] | None = None) -> BlockerContext:
    return BlockerContext(
        failure_class="worker_exception",
        task_id="1.1",
        phase_id="1",
        failing_role="developer",
        recovery_already_tried=list(tried or []),
    )


# ---------------------------------------------------------------------------
# delegate() dispatch seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_honours_model_override_from_task_metadata(
    tmp_path: Path,
) -> None:
    """``delegate`` uses ``task.metadata["model_override"]`` in the dispatched
    ``AgentInvocation`` instead of the per-role spec model."""
    orch = await _build_orch(tmp_path, session="delegate-override")
    adapter = orch.adapter  # type: ignore[assignment]
    spec_model = orch.registry.get("developer").model  # type: ignore[union-attr]

    # Control: no override => spec model.
    plain = Task(id="1.1", phase_id="1", title="t", description="d", complexity="simple")
    await ep.delegate(orch, "developer", _mk_envelope(), task=plain)
    assert adapter.calls[-1].model == spec_model  # type: ignore[attr-defined]

    # Override => escalated model on the dispatched invocation.
    escalated = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        complexity="simple",
        metadata={"model_override": "opus"},
    )
    await ep.delegate(orch, "developer", _mk_envelope(), task=escalated)
    assert adapter.calls[-1].model == "opus"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# _apply_resolution() maps escalate_model onto a real override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_resolution_escalate_model_stamps_override(
    tmp_path: Path,
) -> None:
    """``_apply_resolution`` on ``escalate_model{"to": "opus"}`` re-enables the
    task AND stamps ``model_override="opus"`` onto ``Task.metadata`` (round-trips
    a cold reload)."""
    orch = await _build_orch(tmp_path, session="apply-stamp")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    recovered = await ep._apply_resolution(
        orch, task, _ctx(), _escalate_model_action("opus")
    )
    assert recovered is not None
    assert recovered.metadata.get("model_override") == "opus"

    # Cold reload — the override persists on the Task model, not just in-memory.
    pm2 = PlanManager(tmp_path, session_id="apply-stamp-reload")
    reloaded = await pm2.load()
    assert reloaded is not None
    assert reloaded.phases[0].tasks[0].metadata.get("model_override") == "opus"


@pytest.mark.asyncio
async def test_escalate_model_then_next_dispatch_uses_opus(tmp_path: Path) -> None:
    """END-TO-END: after the resolver chooses ``escalate_model``, the NEXT
    ``delegate`` for the task dispatches on the escalated model."""
    orch = await _build_orch(tmp_path, session="e2e-nextdispatch")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    recovered = await ep._apply_resolution(
        orch, task, _ctx(), _escalate_model_action("opus")
    )
    assert recovered is not None

    await ep.delegate(orch, "developer", _mk_envelope(), task=recovered)
    assert orch.adapter.calls[-1].model == "opus"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# once-per-blocker cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_model_capped_once_per_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second consecutive ``escalate_model`` for the SAME blocker must NOT
    re-fire: the first choice passes ``model_override`` into ``_resolver_retry``;
    the second (with ``escalate_model`` already in ``recovery_already_tried``)
    passes NO fresh override.

    Unit-level cap check with a hand-set ``recovery_already_tried`` — the REAL
    ledger round-trip (that the string ``"escalate_model"`` actually survives the
    ``_record_chosen`` write + ``_prior_resolution_actions`` read) is locked in by
    ``test_escalate_model_cap_real_ledger_round_trip`` below."""
    orch = await _build_orch(tmp_path, session="cap")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    captured: list[dict[str, object]] = []
    orig = ep._resolver_retry

    async def _spy(orch_, task_, **kw):  # noqa: ANN001, ANN202
        captured.append(dict(kw))
        return await orig(orch_, task_, **kw)

    monkeypatch.setattr(ep, "_resolver_retry", _spy)

    # First choice for this blocker — nothing tried yet.
    first = await ep._apply_resolution(
        orch, task, _ctx(tried=[]), _escalate_model_action("opus")
    )
    assert first is not None
    # Second consecutive choice for the SAME blocker.
    await ep._apply_resolution(
        orch,
        first,
        _ctx(tried=["escalate_model"]),
        _escalate_model_action("opus"),
    )

    assert len(captured) == 2, f"expected two _resolver_retry calls, got {captured}"
    assert captured[0].get("model_override") == "opus", (
        f"first escalate_model must widen the model: {captured[0]}"
    )
    assert captured[1].get("model_override") is None, (
        f"second consecutive escalate_model for the same blocker must NOT "
        f"re-widen: {captured[1]}"
    )


@pytest.mark.asyncio
async def test_escalate_model_cap_real_ledger_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent-breakage guard for the cross-module cap contract.

    The once-per-blocker cap depends on the bare string ``"escalate_model"`` (in
    ``_apply_escalate_model``) matching ``ResolutionAction.action`` as PERSISTED by
    ``blocker_resolver._record_chosen`` and RE-READ by ``_prior_resolution_actions``
    — a coupling with no type enforcement that a rename could silently sever while
    every hand-stubbed cap test above still passed. This drives the REAL path: the
    first ``escalate_model`` is recorded via the production ``_record_chosen``
    writer, the SECOND ``BlockerContext`` is built from the production
    ``_prior_resolution_actions`` reader (not a hand-set list), and we assert the
    first applied a fresh override while the second did not.
    """
    from orchestrator import blocker_resolver as br
    from orchestrator import failure_classes as fcls
    from state.schemas import BlockerContext as _BC

    orch = await _build_orch(tmp_path, session="cap-real")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    fclass = fcls.WORKER_EXCEPTION
    action = _escalate_model_action("opus")

    captured: list[dict[str, object]] = []
    orig = ep._resolver_retry

    async def _spy(orch_, task_, **kw):  # noqa: ANN001, ANN202
        captured.append(dict(kw))
        return await orig(orch_, task_, **kw)

    monkeypatch.setattr(ep, "_resolver_retry", _spy)

    # --- Cycle 1: recovery_already_tried comes from the REAL ledger (empty). ---
    tried1 = ep._prior_resolution_actions(orch, task.id, fclass)
    assert "escalate_model" not in tried1
    ctx1 = _BC(
        failure_class=fclass,
        task_id=task.id,
        phase_id=task.phase_id,
        failing_role="developer",
        recovery_already_tried=tried1,
    )
    # Write the resolution_chosen op EXACTLY as ``resolve_blocker`` does in prod.
    await br._record_chosen(orch, ctx1, action, br.blocker_key(ctx1))
    first = await ep._apply_resolution(orch, task, ctx1, action)
    assert first is not None

    # --- Cycle 2: recovery_already_tried is re-derived from the REAL ledger. ---
    tried2 = ep._prior_resolution_actions(orch, task.id, fclass)
    assert "escalate_model" in tried2, (
        "the REAL _record_chosen write did not survive the REAL "
        "_prior_resolution_actions read — the cap's cross-module action-string "
        f"contract is broken (tried2={tried2})"
    )
    ctx2 = _BC(
        failure_class=fclass,
        task_id=task.id,
        phase_id=task.phase_id,
        failing_role="developer",
        recovery_already_tried=tried2,
    )
    await ep._apply_resolution(orch, first, ctx2, action)

    assert len(captured) == 2, f"expected two _resolver_retry calls, got {captured}"
    assert captured[0].get("model_override") == "opus", (
        f"first escalate_model must apply the override: {captured[0]}"
    )
    assert captured[1].get("model_override") is None, (
        "second escalate_model — blocker-scoped via the REAL ledger round-trip — "
        f"must NOT re-fire the override: {captured[1]}"
    )


@pytest.mark.asyncio
async def test_escalate_model_unknown_target_degrades_to_plain_retry(
    tmp_path: Path,
) -> None:
    """An unrecognized ``to`` (not a known model alias) must degrade to a plain
    retry — the task is re-enabled but NO ``model_override`` is stamped (never a
    bogus model on the wire)."""
    orch = await _build_orch(tmp_path, session="unknown-target")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    recovered = await ep._apply_resolution(
        orch, task, _ctx(), _escalate_model_action("totally-not-a-model")
    )
    # Still recovers (plain retry), but does not stamp an override.
    assert recovered is not None
    assert recovered.status == "in_progress"
    assert "model_override" not in recovered.metadata

    # And the next dispatch uses the unchanged spec model.
    spec_model = orch.registry.get("developer").model  # type: ignore[union-attr]
    await ep.delegate(orch, "developer", _mk_envelope(), task=recovered)
    assert orch.adapter.calls[-1].model == spec_model  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_escalate_model_rejects_de_escalation_to_weakest(
    tmp_path: Path,
) -> None:
    """``escalate_model`` must move UP the strength ladder: a ``{"to": "haiku"}``
    (the weakest alias) is a de-escalation despite the action name, so it degrades
    to a plain retry with NO override rather than DOWN-shifting the developer.

    Also pins the pure resolver: opus/sonnet resolve; haiku and unknowns are
    rejected (``None``)."""
    # Pure-function contract for the strength floor.
    assert ep._resolve_escalate_model_target("opus") == "opus"
    assert ep._resolve_escalate_model_target("sonnet") == "sonnet"
    assert ep._resolve_escalate_model_target("haiku") is None
    assert ep._resolve_escalate_model_target("claude-opus-4-8") == "opus"
    assert ep._resolve_escalate_model_target(None) is None

    # Behaviour through the apply path: haiku target does not stamp an override.
    orch = await _build_orch(tmp_path, session="no-deescalate")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    recovered = await ep._apply_resolution(
        orch, task, _ctx(), _escalate_model_action("haiku")
    )
    assert recovered is not None
    assert recovered.status == "in_progress"
    assert "model_override" not in recovered.metadata

    spec_model = orch.registry.get("developer").model  # type: ignore[union-attr]
    await ep.delegate(orch, "developer", _mk_envelope(), task=recovered)
    assert orch.adapter.calls[-1].model == spec_model  # type: ignore[attr-defined]
